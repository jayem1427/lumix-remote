#!/usr/bin/env python3
"""Lumix Wi-Fi remote: cam.cgi client, live view receiver and a CLI smoke test.

Speaks the HTTP API the Panasonic Image App uses. Standard library only.

    python lumix.py test                       # run the smoke test against the camera
    python lumix.py raw mode=camcmd value=tele-fast
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

DEFAULT_IP = "192.168.54.1"
STREAM_PORT = 49199
DEVICE_ID = "4D454930-0100-1000-8001-0A1B2C3D4E5F"
DEVICE_NAME = "G7Remote"

# cam.cgi word that drives focus toward each end. Swap the values if Near/Far feel reversed.
FOCUS_WORD = {"near": "wide", "far": "tele"}

# Bypass any system proxy: the camera is a local access point with no internet.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CameraError(RuntimeError):
    pass


class Reply:
    """A parsed cam.cgi reply. Most are XML; a few (focus) are comma-separated text."""

    def __init__(self, raw: str):
        self.raw = raw.strip()
        self.fields: dict[str, str] = {}
        self.values: list[str] = []
        if self.raw.startswith("<"):
            root = ET.fromstring(self.raw)
            self.result = (root.findtext("result") or "").strip()
            state = root.find("state")
            if state is not None:
                self.fields = {c.tag: (c.text or "").strip() for c in state}
        else:
            parts = self.raw.split(",")
            self.result, self.values = parts[0], parts[1:]

    @property
    def ok(self) -> bool:
        return self.result.startswith("ok")


class Lumix:
    def __init__(self, ip: str = DEFAULT_IP, timeout: float = 3.0):
        self.ip = ip
        self.timeout = timeout

    def cgi(self, mode: str, check: bool = True, **params: str) -> Reply:
        query = urllib.parse.urlencode({"mode": mode, **params}, safe="/")
        url = f"http://{self.ip}/cam.cgi?{query}"
        try:
            with _opener.open(url, timeout=self.timeout) as r:
                reply = Reply(r.read().decode("utf-8", "replace"))
        except (OSError, ET.ParseError) as e:
            raise CameraError(f"no reply from {self.ip}: {e}") from e
        if check and not reply.ok:
            raise CameraError(f"{query} -> {reply.result or reply.raw[:80]!r}")
        return reply

    # --- session -------------------------------------------------------------------------
    def connect(self, approve_timeout: float = 45.0, on_wait=None) -> dict:
        """Request access, wait for approval on the camera, then switch it to record mode."""
        deadline = time.monotonic() + approve_timeout
        while True:
            acc = self.cgi("accctrl", check=False, type="req_acc", value=DEVICE_ID, value2=DEVICE_NAME)
            # "ok_under_research" means the camera is showing an approval prompt.
            if acc.result != "ok_under_research":
                break
            if time.monotonic() > deadline:
                raise CameraError(f"no approval on the camera within {approve_timeout:.0f}s")
            if on_wait:
                on_wait()
            time.sleep(1.0)
        try:
            state = self.state()
        except CameraError as e:
            raise CameraError(f"{e} (access request said {acc.result!r})") from e
        self.cgi("camcmd", value="recmode")
        return {"access": acc.result, "state": state}

    def state(self) -> dict:
        return self.cgi("getstate").fields

    # --- lens ----------------------------------------------------------------------------
    def zoom(self, direction: str, fast: bool = False) -> Reply:
        word = {"in": "tele", "out": "wide"}[direction]
        return self.cgi("camcmd", value=f"{word}-{'fast' if fast else 'normal'}")

    def zoom_stop(self) -> Reply:
        return self.cgi("camcmd", value="zoomstop")

    def focus(self, direction: str, fast: bool = False) -> Reply:
        word = FOCUS_WORD[direction]
        return self.cgi("camctrl", type="focus", value=f"{word}-{'fast' if fast else 'normal'}")

    def touch_af(self, x: float, y: float) -> Reply:
        """Focus at a point; x and y are 0..1 across the frame."""
        px, py = (min(1000, max(0, round(v * 1000))) for v in (x, y))
        return self.cgi("camctrl", type="touch", value=f"{px}/{py}", value2="on")

    # --- shutter -------------------------------------------------------------------------
    def record(self, start: bool) -> Reply:
        return self.cgi("camcmd", value="video_recstart" if start else "video_recstop")

    def capture(self) -> Reply:
        reply = self.cgi("camcmd", value="capture")
        self.cgi("camcmd", check=False, value="capture_cancel")
        return reply

    # --- files ---------------------------------------------------------------------------
    def play_mode(self) -> None:
        self.cgi("camcmd", value="playmode")

    def rec_mode(self) -> None:
        self.cgi("camcmd", value="recmode")

    def files(self) -> list[CamFile]:
        """List the card's contents. The camera must be in play mode."""
        return MediaServer(self.ip).list()


# --- file server (UPnP ContentDirectory, available in play mode) ---------------------------
DIDL = {
    "didl": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/",
}
BROWSE = """<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body><u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1" xmlns:pana="urn:schemas-panasonic-com:pana">
<ObjectID>{oid}</ObjectID><BrowseFlag>BrowseDirectChildren</BrowseFlag><Filter>*</Filter>
<StartingIndex>{start}</StartingIndex><RequestedCount>{count}</RequestedCount><SortCriteria></SortCriteria>
<pana:X_FromCP>LumixLink2.0PC</pana:X_FromCP>
</u:Browse></s:Body></s:Envelope>"""


class CamFile:
    def __init__(self, oid: str, title: str, upnp_class: str, date: str, res: list[dict]):
        self.id, self.title, self.date, self.res = oid, title, date, res
        mime = " ".join(r["protocol"] for r in res).lower()
        self.kind = "video" if "video" in upnp_class or "video/" in mime else "photo"
        self.original = self._pick(lambda r: "/DO" in r["url"]) or (max(res, key=lambda r: r["size"]) if res else None)
        self.thumb = self._pick(lambda r: "/DT" in r["url"] or "JPEG_TN" in r["protocol"])
        self.preview = self._pick(lambda r: "/DL" in r["url"] or "JPEG_LRG" in r["protocol"])

    def _pick(self, test):
        return next((r for r in self.res if test(r)), None)

    @property
    def filename(self) -> str:
        url = self.original["url"] if self.original else ""
        name = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
        # Original URLs look like /DO1000123.JPG; the camera's own name is the part after the prefix.
        return name[2:] if name[:2] in ("DO", "DT", "DL") else (name or self.title)

    def to_json(self) -> dict:
        return {
            "id": self.id, "title": self.title, "name": self.filename, "kind": self.kind, "date": self.date,
            "size": self.original["size"] if self.original else 0, "has_thumb": bool(self.thumb or self.preview),
        }


class MediaServer:
    def __init__(self, ip: str):
        self.ip = ip
        self._control: str | None = None

    def control_url(self) -> str:
        if self._control:
            return self._control
        loc = self._ssdp_location()
        if loc:
            desc = ET.fromstring(_opener.open(loc, timeout=5).read())
            for svc in desc.iter("{urn:schemas-upnp-org:device-1-0}service"):
                if "ContentDirectory" in (svc.findtext("{urn:schemas-upnp-org:device-1-0}serviceType") or ""):
                    path = svc.findtext("{urn:schemas-upnp-org:device-1-0}controlURL") or ""
                    self._control = urllib.parse.urljoin(loc, path)
                    return self._control
        self._control = f"http://{self.ip}:60606/Server0/CDS_control"
        return self._control

    def _ssdp_location(self) -> str | None:
        msg = (
            "M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
            "ST: urn:schemas-upnp-org:service:ContentDirectory:1\r\n\r\n"
        ).encode()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2.5)
            for dest in ((self.ip, 1900), ("239.255.255.250", 1900)):
                try:
                    s.sendto(msg, dest)
                except OSError:
                    pass
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                try:
                    data, addr = s.recvfrom(4096)
                except (socket.timeout, OSError):
                    break
                if addr[0] != self.ip:
                    continue
                for line in data.decode("utf-8", "replace").splitlines():
                    if line.lower().startswith("location:"):
                        return line.split(":", 1)[1].strip()
        return None

    def browse(self, oid: str, start: int, count: int) -> tuple[ET.Element, int]:
        body = BROWSE.format(oid=oid, start=start, count=count).encode()
        req = urllib.request.Request(self.control_url(), data=body, headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"',
        })
        try:
            raw = _opener.open(req, timeout=10).read()
        except OSError as e:
            raise CameraError(f"file listing failed at {self.control_url()}: {e}") from e
        env = ET.fromstring(raw)
        result = next((el.text for el in env.iter() if el.tag.endswith("Result")), "") or ""
        total = next((el.text for el in env.iter() if el.tag.endswith("TotalMatches")), "0") or "0"
        return ET.fromstring(result or "<DIDL-Lite/>"), int(total)

    def list(self, oid: str = "0", depth: int = 0) -> list[CamFile]:
        out: list[CamFile] = []
        start, page = 0, 50
        while True:
            didl, total = self.browse(oid, start, page)
            children = list(didl)
            for el in children:
                if el.tag.endswith("container") and depth < 3:
                    out += self.list(el.get("id", ""), depth + 1)
                elif el.tag.endswith("item"):
                    out.append(self._item(el))
            start += len(children)
            if not children or start >= total:
                return out

    @staticmethod
    def _item(el: ET.Element) -> CamFile:
        res = [{
            "url": (r.text or "").strip(),
            "protocol": r.get("protocolInfo", ""),
            "size": int(r.get("size") or 0),
        } for r in el.findall("didl:res", DIDL)]
        return CamFile(
            el.get("id", ""),
            el.findtext("dc:title", "", DIDL),
            el.findtext("upnp:class", "", DIDL),
            el.findtext("dc:date", "", DIDL),
            res,
        )


def fetch(url: str, timeout: float = 10.0) -> bytes:
    try:
        with _opener.open(url, timeout=timeout) as r:
            return r.read()
    except OSError as e:
        raise CameraError(f"fetch failed for {url}: {e}") from e


def download(url: str, dest_path, progress=None, chunk: int = 256 * 1024) -> int:
    """Stream url to dest_path via a .part file; progress(bytes_done) is called per chunk."""
    import os
    tmp = f"{dest_path}.part"
    done = 0
    try:
        with _opener.open(url, timeout=20) as r, open(tmp, "wb") as f:
            while True:
                buf = r.read(chunk)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if progress:
                    progress(done)
    except OSError as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise CameraError(f"download failed for {url}: {e}") from e
    os.replace(tmp, dest_path)
    return done


class LiveView:
    """Receives the camera's UDP live view and keeps the latest JPEG frame."""

    def __init__(self, cam: Lumix, port: int = STREAM_PORT):
        self.cam, self.port = cam, port
        self.frame: bytes | None = None
        self.seq = 0
        self.last_frame_at = 0.0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._sock: socket.socket | None = None

    @property
    def running(self) -> bool:
        return self._sock is not None

    def start(self) -> None:
        if self.running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.port))
        sock.settimeout(1.0)
        self._sock = sock
        self._stop.clear()
        try:
            self.cam.cgi("startstream", value=str(self.port))
        except CameraError:
            self.stop()
            raise
        threading.Thread(target=self._recv, daemon=True).start()
        threading.Thread(target=self._keepalive, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self.cam.cgi("stopstream", check=False)
            except CameraError:
                pass
            self._sock.close()
            self._sock = None

    def wait_frame(self, last_seq: int, timeout: float = 2.0) -> tuple[int, bytes | None]:
        """Block until a frame newer than last_seq arrives; returns (seq, frame or None)."""
        with self._cond:
            self._cond.wait_for(lambda: self.seq != last_seq, timeout)
            if self.seq == last_seq:
                return last_seq, None
            return self.seq, self.frame

    def _recv(self) -> None:
        while not self._stop.is_set() and self._sock:
            try:
                data, _ = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            start = data.find(b"\xff\xd8")  # JPEG starts after a variable-length header
            if start < 0:
                continue
            with self._cond:
                self.frame = data[start:]
                self.seq += 1
                self.last_frame_at = time.monotonic()
                self._cond.notify_all()

    def _keepalive(self) -> None:
        # The camera drops the stream unless it is polled; restart it if frames stall.
        while not self._stop.wait(2.0):
            try:
                self.cam.cgi("getstate", check=False)
                if time.monotonic() - self.last_frame_at > 4.0:
                    self.cam.cgi("startstream", check=False, value=str(self.port))
            except CameraError:
                pass


# --- CLI -----------------------------------------------------------------------------------
def _step(name: str, fn):
    t = time.perf_counter()
    try:
        out = fn()
        print(f"  PASS  {name:<28} {1000 * (time.perf_counter() - t):6.0f} ms  {out if out else ''}")
        return True
    except Exception as e:  # noqa: BLE001 - report every failure and keep going
        print(f"  FAIL  {name:<28} {e}")
        return False


def smoke_test(cam: Lumix, record: bool) -> int:
    print(f"Lumix smoke test -> {cam.ip}\n")
    prompted = []

    def waiting():
        if not prompted:
            print(f"  ....  approve \"{DEVICE_NAME}\" on the camera screen")
            prompted.append(True)

    if not _step("connect", lambda: cam.connect(on_wait=waiting)["access"]):
        print("\nIf the camera is unreachable, join its Wi-Fi (Wi-Fi > New Connection > Remote Shooting & View).")
        return 1
    _step("getstate", lambda: ", ".join(f"{k}={v}" for k, v in cam.state().items()))

    def zoom(direction):
        cam.zoom(direction)
        time.sleep(1.0)
        cam.zoom_stop()

    if not _step("zoom in 1s", lambda: zoom("in")):
        print("        zoom needs a power zoom lens (e.g. 14-42 PZ, 45-175 PZ)")
    _step("zoom out 1s", lambda: zoom("out"))

    def nudge(direction):
        reply = None
        for _ in range(5):
            reply = cam.focus(direction)
            time.sleep(0.1)
        return ",".join(reply.values) if reply and reply.values else None

    _step("focus near x5", lambda: nudge("near"))
    _step("focus far x5", lambda: nudge("far"))
    _step("touch AF center", lambda: cam.touch_af(0.5, 0.5) and None)

    def grab():
        live = LiveView(cam)
        live.start()
        try:
            _, frame = live.wait_frame(0, timeout=4.0)
            if frame is None:
                raise CameraError("no frames in 4s (Windows firewall may block UDP 49199)")
            with open("liveview.jpg", "wb") as f:
                f.write(frame)
            return f"saved liveview.jpg ({len(frame)} bytes)"
        finally:
            live.stop()

    _step("live view frame", grab)

    if record:
        def clip():
            cam.record(True)
            time.sleep(3.0)
            cam.record(False)

        _step("record 3s clip", clip)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ip", default=DEFAULT_IP)
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("test", help="run the smoke test")
    t.add_argument("--record", action="store_true", help="also record a 3 s clip")
    r = sub.add_parser("raw", help="send one cam.cgi request, e.g. mode=camcmd value=zoomstop")
    r.add_argument("params", nargs="+")
    sub.add_parser("files", help="list the card's files (switches the camera to play mode)")
    args = p.parse_args()

    cam = Lumix(args.ip)
    if args.cmd == "test":
        return smoke_test(cam, args.record)
    if args.cmd == "files":
        cam.connect()
        cam.play_mode()
        time.sleep(1.0)
        ms = MediaServer(cam.ip)
        print("control:", ms.control_url())
        for f in ms.list():
            print(f"{f.kind:5} {f.filename:16} {f.date:20} {f.original['size'] if f.original else 0:>10}")
            for res in f.res:
                print(f"        {res['size']:>10}  {res['protocol'][:60]:60}  {res['url']}")
        cam.rec_mode()
        return 0
    params = dict(kv.split("=", 1) for kv in args.params)
    print(cam.cgi(params.pop("mode"), check=False, **params).raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
