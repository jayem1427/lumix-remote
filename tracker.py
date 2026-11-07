"""Person tracking: find people in the live view with YOLO and steer the gimbal to keep one framed.

Needs `pip install ultralytics` (pulls in PyTorch). The first run downloads the small yolo11n model.
Coordinates are normalised 0..1 in the camera's sensor frame; the GUI maps them through its rotate and
mirror settings, and so does this module when it turns framing error into pan and tilt.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

CAL_FILE = Path.home() / ".g7remote.json"
MODEL = str(Path(__file__).with_name("yolo11n.pt"))
CONF = 0.2  # detection threshold; live view is small, so people score lower than in photos
COAST_S = 0.6  # keep steering toward the last box this long when a frame finds nobody


def to_display(x: float, y: float, rot: int, mirror: bool) -> tuple[float, float]:
    """Sensor point -> point as the GUI shows it (inverse of index.html's toSensor)."""
    if rot == 90:
        x, y = 1 - y, x
    elif rot == 180:
        x, y = 1 - x, 1 - y
    elif rot == 270:
        x, y = y, 1 - x
    return (1 - x if mirror else x), y


class Axis:
    """One steering axis: proportional-derivative control plus a check that learns which way is which.

    Panning toward the subject should shrink its framing error. If, while we command a speed, the error
    keeps moving the same way as the command, the gimbal's sign is the other way round and we flip it.
    """

    def __init__(self, sign: int = 1, calibrated: bool = False):
        self.sign, self.calibrated = sign, calibrated
        self.evidence, self.samples = 0.0, 0
        self.prev_err: float | None = None
        self.prev_cmd = 0.0
        self.d = 0.0

    def reset(self) -> None:
        self.prev_err, self.prev_cmd, self.d = None, 0.0, 0.0

    def step(self, err: float, dt: float, gain: float, max_speed: float, dead: float) -> float:
        if self.prev_err is not None and dt > 0:
            delta = err - self.prev_err
            self.d = 0.6 * self.d + 0.4 * delta / dt
            if not self.calibrated and abs(self.prev_cmd) > 0.08:
                self.evidence += self.prev_cmd * delta
                self.samples += 1
                if self.samples >= 6 and self.evidence > 0.04:
                    self.sign, self.evidence, self.samples = -self.sign, 0.0, 0
                elif self.samples >= 10 and self.evidence < -0.04:
                    self.calibrated = True
        self.prev_err = err
        mag = abs(err)
        if mag < dead:
            u = 0.0
        else:
            e = (mag - dead) * (1 if err > 0 else -1)
            u = gain * e + 0.12 * gain * self.d
        u = max(-1.0, min(1.0, u))
        self.prev_cmd = u
        return self.sign * u * max_speed


class Tracker:
    def __init__(self, live, gimbal, manual_until=lambda: 0.0):
        self.live, self.gimbal, self.manual_until = live, gimbal, manual_until
        self.model = None
        self.error = ""
        self.enabled = False
        self.locked: int | None = None
        self.target: dict | None = None
        self.people: list[dict] = []
        self.fps = 0.0
        self.cfg = {"rot": 0, "mirror": False, "tilt": False, "gain": 3.0, "speed": 900}
        cal = self._load_cal()
        self.pan = Axis(cal.get("pan", 1), "pan" in cal)
        self.tilt = Axis(cal.get("tilt", 1), "tilt" in cal)
        self._last_seen = 0.0
        self._last_box: list[float] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # --- state ---------------------------------------------------------------------------------
    @staticmethod
    def _load_cal() -> dict:
        try:
            return json.loads(CAL_FILE.read_text()).get("track_sign", {})
        except (OSError, ValueError):
            return {}

    def _save_cal(self) -> None:
        cal = {k: a.sign for k, a in (("pan", self.pan), ("tilt", self.tilt)) if a.calibrated}
        try:
            data = json.loads(CAL_FILE.read_text()) if CAL_FILE.exists() else {}
        except (OSError, ValueError):
            data = {}
        if data.get("track_sign") != cal:
            data["track_sign"] = cal
            CAL_FILE.write_text(json.dumps(data, indent=2))

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self._thread is not None, "enabled": self.enabled, "error": self.error,
                "locked": self.locked, "target": self.target, "people": self.people, "fps": round(self.fps, 1),
                "calibrated": {"pan": self.pan.calibrated, "tilt": self.tilt.calibrated},
                "sign": {"pan": self.pan.sign, "tilt": self.tilt.sign},
                "cfg": self.cfg,
            }

    def configure(self, **cfg) -> None:
        for k in ("rot", "mirror", "tilt", "gain", "speed"):
            if k in cfg and cfg[k] is not None:
                self.cfg[k] = type(self.cfg[k])(cfg[k])
        if self.cfg["rot"] not in (0, 90, 180, 270):
            raise ValueError(f"rot must be 0, 90, 180 or 270, not {self.cfg['rot']}")

    def recalibrate(self) -> None:
        self.pan, self.tilt = Axis(), Axis()
        self._save_cal()

    # --- control -------------------------------------------------------------------------------
    def start(self) -> None:
        """Start detecting (boxes appear in the GUI); steering is separate, see enable()."""
        if self._thread:
            return
        self.error = ""
        if self.model is None:
            try:
                from ultralytics import YOLO
            except ImportError as e:
                self.error = "person tracking needs: pip install ultralytics"
                raise RuntimeError(self.error) from e
            self.model = YOLO(MODEL)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="tracker")
        self._thread.start()

    def stop(self) -> None:
        self.enable(False)
        self._stop.set()
        if self._thread:
            self._thread.join(3)
        self._thread = None
        with self._lock:
            self.people, self.target = [], None

    def enable(self, on: bool) -> None:
        self.enabled = on
        self.pan.reset()
        self.tilt.reset()
        if not on:
            self._halt()

    def _halt(self) -> None:
        if self.gimbal.state == "on" and any(self.gimbal.speed) and time.monotonic() > self.manual_until():
            try:
                self.gimbal.stop()
            except RuntimeError:
                pass

    # --- loop ----------------------------------------------------------------------------------
    def _loop(self) -> None:
        import cv2
        import numpy as np

        seq, t_prev = -1, time.monotonic()
        while not self._stop.is_set():
            seq, jpeg = self.live.wait_frame(seq, 1.0)
            if jpeg is None:
                if self.enabled:
                    self._halt()
                continue
            frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue
            h, w = frame.shape[:2]
            res = self.model.track(frame, persist=True, classes=[0], imgsz=640, conf=CONF, verbose=False,
                                   tracker="bytetrack.yaml")[0]
            people = []
            if res.boxes is not None and res.boxes.id is not None:
                for (x1, y1, x2, y2), tid, c in zip(res.boxes.xyxy.tolist(), res.boxes.id.int().tolist(),
                                                    res.boxes.conf.tolist()):
                    people.append({"id": tid, "conf": round(c, 2),
                                   "box": [round(x1 / w, 4), round(y1 / h, 4), round(x2 / w, 4), round(y2 / h, 4)]})
            now = time.monotonic()
            dt, t_prev = now - t_prev, now
            self.fps = 0.8 * self.fps + 0.2 * (1 / dt if dt > 0 else 0)
            target = self._pick(people, now)
            with self._lock:
                self.people, self.target = people, target
            if self.enabled:
                self._steer(target, dt, now)

    def _pick(self, people: list[dict], now: float) -> dict | None:
        """Follow whoever is in frame: the same person while they are visible, else the nearest to
        where the last one was, else the biggest. A short gap in detections keeps the last box."""
        if not people:
            if self._last_box and now - self._last_seen < COAST_S:
                return {"id": self.locked, "box": self._last_box, "coast": True}
            self.locked = None
            return None

        def area(p):
            b = p["box"]
            return (b[2] - b[0]) * (b[3] - b[1])

        pick = next((p for p in people if p["id"] == self.locked), None)
        if pick is None and self._last_box and now - self._last_seen < 1.5:
            lb = self._last_box
            pick = min(people, key=lambda p: abs(p["box"][0] + p["box"][2] - lb[0] - lb[2])
                       + abs(p["box"][1] + p["box"][3] - lb[1] - lb[3]))
        if pick is None:
            pick = max(people, key=area)
        self.locked, self._last_box, self._last_seen = pick["id"], pick["box"], now
        return pick

    def _steer(self, target: dict | None, dt: float, now: float) -> None:
        if self.gimbal.state != "on" or now < self.manual_until():
            self.pan.reset()
            self.tilt.reset()
            return
        if target is None:
            self.pan.reset()
            self.tilt.reset()
            self._halt()
            return
        b = target["box"]
        # Aim at the upper body (a third of the way down the box) and keep it a little above centre.
        cx, cy = to_display((b[0] + b[2]) / 2, b[1] + 0.33 * (b[3] - b[1]), self.cfg["rot"], self.cfg["mirror"])
        g, top = self.cfg["gain"], self.cfg["speed"]
        before = (self.pan.calibrated, self.tilt.calibrated)
        if target.get("coast"):
            g *= 0.5  # the box is stale; ease off rather than overshoot
        yaw = self.pan.step(cx - 0.5, dt, g, top, 0.025)
        pitch = self.tilt.step(cy - 0.42, dt, g, top * 0.6, 0.05) if self.cfg["tilt"] else 0.0
        try:
            self.gimbal.move(yaw=yaw, pitch=pitch)
        except RuntimeError as e:
            self.error = str(e)
        if (self.pan.calibrated, self.tilt.calibrated) != before:
            self._save_cal()


class FakeGimbal:
    """Stands in for GimbalLink in demo mode."""

    def __init__(self):
        self.state, self.name, self.error, self.speed = "off", "", "", (0, 0, 0)

    def status(self) -> dict:
        return {"state": self.state, "name": self.name, "error": self.error, "moving": any(self.speed)}

    def connect(self) -> None:
        time.sleep(0.6)
        self.state, self.name = "on", "DJI RS3 MINI-DEMO"

    def move(self, yaw: float = 0, pitch: float = 0, roll: float = 0) -> None:
        self.speed = (int(yaw), int(roll), int(pitch))

    def stop(self) -> None:
        self.speed = (0, 0, 0)

    def close(self) -> None:
        self.state = "off"
