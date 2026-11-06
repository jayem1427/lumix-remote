#!/usr/bin/env python3
"""DJI RS 3 Mini over Bluetooth LE: DUML frame codec, live telemetry and speed control.

The gimbal speaks DJI's DUML framing on GATT service fff0: write commands to fff5 (write without
response), receive pushed frames on fff4 (notify). Speed commands lapse, so they are resent at 10 Hz;
if this script stops, the gimbal coasts to a halt.

    python gimbal.py selftest              # codec checks, no Bluetooth needed
    python gimbal.py scan                  # find the gimbal
    python gimbal.py listen [seconds]      # print decoded frames the gimbal pushes
    python gimbal.py move yaw 100 1.0      # yaw at speed 100 for 1 s (units assumed 0.1 deg/s)
    python gimbal.py raw 04 0c c8000000000080 [--type 00]

Needs `pip install bleak` for anything that touches the gimbal. Close the Ronin app first: the gimbal
holds one connection at a time.
Protocol references: github.com/aronesztergomi/DJI-Ronin-Bluetooth-Connector (MIT),
github.com/o-gs/dji-firmware-tools. CRCs here were checked against frames captured from an RS 3 Mini.
"""
from __future__ import annotations

import argparse
import asyncio
import struct
import sys
from dataclasses import dataclass

SERVICE = "0000fff0-0000-1000-8000-00805f9b34fb"
NOTIFY = "0000fff4-0000-1000-8000-00805f9b34fb"
WRITE = "0000fff5-0000-1000-8000-00805f9b34fb"
NAME_HINT = "RS"  # advertised as e.g. "DJI RS3 MINI-061EWU"

APP, GIMBAL = 0x02, 0x04
SET_GIMBAL = 0x04
CMD_SPEED = 0x0C  # ControlGimbalSpeed
# Payload: three little-endian i16 speeds then a control byte. Yaw in slot 0 is confirmed on an
# RS 3 Mini (cmd_type 00); roll/pitch slots and the 0.1 deg/s unit are still ASSUMED.
AXES = {"yaw": 0, "roll": 1, "pitch": 2}
SPEED_CTRL = 0x80
SEND_HZ = 10


# --- codec ---------------------------------------------------------------------------------
def crc8(data: bytes, seed: int = 0x77) -> int:
    crc = seed
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc


def crc16(data: bytes, seed: int = 0x3692) -> int:
    """DJI CRC16: reflected CCITT polynomial (0x8408), seed 0x3692, stored little-endian."""
    crc = seed
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc


@dataclass
class Frame:
    sender: int
    receiver: int
    seq: int
    cmd_type: int
    cmd_set: int
    cmd_id: int
    payload: bytes

    def encode(self) -> bytes:
        length = 13 + len(self.payload)
        head = bytes([0x55, length & 0xFF, ((length >> 8) & 0x03) | (1 << 2)])
        body = head + bytes([crc8(head), self.sender, self.receiver]) + struct.pack("<H", self.seq) \
            + bytes([self.cmd_type, self.cmd_set, self.cmd_id]) + self.payload
        return body + struct.pack("<H", crc16(body))

    def __str__(self) -> str:
        return (f"{self.sender:02x}->{self.receiver:02x} seq={self.seq:<5} type={self.cmd_type:02x} "
                f"set={self.cmd_set:02x} id={self.cmd_id:02x} {self.payload.hex()}")


def decode(buf: bytes) -> list[Frame]:
    """Split one notification into frames; frames with a bad CRC are dropped."""
    out, i = [], 0
    while i + 13 <= len(buf) and buf[i] == 0x55:
        n = buf[i + 1] | ((buf[i + 2] & 0x03) << 8)
        f = buf[i:i + n]
        if n < 13 or len(f) < n:
            break
        if crc8(f[:3]) == f[3] and crc16(f[:-2]) == struct.unpack("<H", f[-2:])[0]:
            seq = struct.unpack("<H", f[6:8])[0]
            out.append(Frame(f[4], f[5], seq, f[8], f[9], f[10], f[11:-2]))
        i += n
    return out


def speed_payload(yaw: int = 0, roll: int = 0, pitch: int = 0) -> bytes:
    vals = [0, 0, 0]
    for axis, v in (("yaw", yaw), ("roll", roll), ("pitch", pitch)):
        vals[AXES[axis]] = max(-32768, min(32767, int(v)))
    return struct.pack("<hhh", *vals) + bytes([SPEED_CTRL])


# --- connection ----------------------------------------------------------------------------
class Gimbal:
    def __init__(self, cmd_type: int = 0x00):
        self.cmd_type = cmd_type
        self._seq = 0
        self._client = None
        self._speed = (0, 0, 0)
        self._streaming: asyncio.Task | None = None
        self.on_frame = None  # callback(Frame)

    async def connect(self, timeout: float = 20.0) -> str:
        from bleak import BleakClient, BleakScanner

        dev = await BleakScanner.find_device_by_filter(
            lambda d, a: (a.local_name or d.name or "").upper().startswith("DJI " + NAME_HINT), timeout=timeout)
        if not dev:
            raise RuntimeError("gimbal not found; is it on, and is the Ronin app closed?")
        self._client = BleakClient(dev, timeout=timeout)
        await self._client.connect()
        await self._client.start_notify(NOTIFY, self._notified)
        return dev.name or dev.address

    def _notified(self, _, data: bytearray) -> None:
        if self.on_frame:
            for f in decode(bytes(data)):
                self.on_frame(f)

    async def send(self, cmd_set: int, cmd_id: int, payload: bytes = b"") -> bytes:
        self._seq = (self._seq + 1) & 0xFFFF
        frame = Frame(APP, GIMBAL, self._seq, self.cmd_type, cmd_set, cmd_id, payload).encode()
        await self._client.write_gatt_char(WRITE, frame, response=False)
        return frame

    async def set_speed(self, yaw: int = 0, roll: int = 0, pitch: int = 0) -> None:
        """Set a continuous speed; it is resent at 10 Hz until changed or stop() is called."""
        self._speed = (yaw, roll, pitch)
        if self._streaming is None:
            self._streaming = asyncio.create_task(self._stream())

    async def _stream(self) -> None:
        while True:
            await self.send(SET_GIMBAL, CMD_SPEED, speed_payload(*self._speed))
            await asyncio.sleep(1 / SEND_HZ)

    async def stop(self) -> None:
        if self._streaming:
            self._streaming.cancel()
            self._streaming = None
        self._speed = (0, 0, 0)
        if self._client and self._client.is_connected:
            for _ in range(3):
                await self.send(SET_GIMBAL, CMD_SPEED, speed_payload())
                await asyncio.sleep(0.05)

    async def close(self) -> None:
        try:
            await self.stop()
        finally:
            if self._client:
                await self._client.disconnect()


# --- CLI -----------------------------------------------------------------------------------
def selftest() -> int:
    # Frames captured from a DJI RS 3 Mini (gimbal -> app pushes).
    captured = [
        "551a04b104020000001c010301000100020001ff030001005b10",
        "551204c70402ba01000427000000000025d3",
    ]
    ok = True
    for h in captured:
        frames = decode(bytes.fromhex(h))
        good = len(frames) == 1 and frames[0].encode().hex() == h
        print(f"  {'PASS' if good else 'FAIL'}  decode/re-encode {h[:24]}...")
        ok &= good
    f = Frame(APP, GIMBAL, 1, 0, SET_GIMBAL, CMD_SPEED, speed_payload(yaw=200))
    again = decode(f.encode())
    good = len(again) == 1 and again[0].payload == bytes.fromhex("c8000000000080")
    print(f"  {'PASS' if good else 'FAIL'}  speed frame round-trip {f.encode().hex()}")
    ok &= good
    return 0 if ok else 1


async def run(args) -> int:
    if args.cmd == "scan":
        from bleak import BleakScanner

        found = await BleakScanner.discover(timeout=8, return_adv=True)
        for dev, adv in found.values():
            name = adv.local_name or dev.name or ""
            if name.upper().startswith("DJI"):
                print(f"{dev.address}  {name}  rssi={adv.rssi}")
        return 0

    g = Gimbal(cmd_type=int(args.type, 16))
    print("connected:", await g.connect())
    g.on_frame = (lambda f: print("  <-", f)) if args.cmd in ("listen", "raw") or args.verbose else None
    try:
        if args.cmd == "listen":
            await asyncio.sleep(args.seconds)
        elif args.cmd == "move":
            kw = {args.axis: args.speed}
            print(f"  {args.axis} speed {args.speed} for {args.seconds}s")
            await g.set_speed(**kw)
            await asyncio.sleep(args.seconds)
        elif args.cmd == "raw":
            sent = await g.send(int(args.set, 16), int(args.id, 16), bytes.fromhex(args.payload))
            print("  ->", sent.hex())
            await asyncio.sleep(2)
    finally:
        await g.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--type", default="00", help="DUML cmd_type byte in hex (default 00)")
    p.add_argument("-v", "--verbose", action="store_true", help="print pushed frames during move")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    sub.add_parser("scan")
    s = sub.add_parser("listen")
    s.add_argument("seconds", nargs="?", type=float, default=5)
    s = sub.add_parser("move")
    s.add_argument("axis", choices=list(AXES))
    s.add_argument("speed", type=int)
    s.add_argument("seconds", type=float)
    s = sub.add_parser("raw")
    s.add_argument("set")
    s.add_argument("id")
    s.add_argument("payload", nargs="?", default="")
    args = p.parse_args()
    if args.cmd == "selftest":
        return selftest()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
