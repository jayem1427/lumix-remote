#!/usr/bin/env python3
"""Browser GUI for the Lumix remote.

    python app.py            # serve http://localhost:8754 and open it
    python app.py --demo     # simulated camera and gimbal, for trying the UI without them
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import os
import subprocess
import sys

from gimbal import GimbalLink
from lumix import DEFAULT_IP, CameraError, CamFile, LiveView, Lumix, Reply, discover, download, fetch
from tracker import FakeGimbal, Tracker

INDEX = Path(__file__).with_name("index.html")
DEFAULT_DEST = Path.home() / "Pictures" / "G7"
DEMO_ASSETS = Path(__file__).with_name("assets") / "demo"


class FakeLumix(Lumix):
    """Answers cam.cgi like a camera would, without a network."""

    def __init__(self, ip: str = DEFAULT_IP):
        super().__init__(ip)
        self.rec_since: float | None = None

    def cgi(self, mode: str, check: bool = True, **params: str) -> Reply:
        time.sleep(0.03)
        value = params.get("value", "")
        if mode == "camcmd" and value == "video_recstart":
            self.rec_since = time.time()
        elif mode == "camcmd" and value == "video_recstop":
            self.rec_since = None
        if mode == "camctrl" and params.get("type") == "focus":
            return Reply("ok,512,1024")
        state = {
            "batt": "3/3",
            "cammode": "rec",
            "rec": "on" if self.rec_since else "off",
            "video_remaincapacity": "5420",
            "sd_memory": "set",
        }
        body = "".join(f"<{k}>{v}</{k}>" for k, v in state.items())
        return Reply(f"<camrply><result>ok</result><state>{body}</state></camrply>")

    def files(self) -> list[CamFile]:
        out = []
        for i in range(18):
            video = i % 5 == 0
            name = f"P10{i:05d}.{'MP4' if video else 'JPG'}"
            size = 48_000_000 if video else 6_200_000
            res = [{"url": f"demo://DO{name}", "protocol": "video/mp4" if video else "image/jpeg", "size": size},
                   {"url": f"demo://DT{name}", "protocol": "image/jpeg:DLNA.ORG_PN=JPEG_TN", "size": 0}]
            out.append(CamFile(str(i), name, "object.item.videoItem" if video else "object.item.imageItem",
                               f"2026-11-0{1 + i % 5}T1{i % 10}:2{i % 6}:00", res))
        return out

    @staticmethod
    def thumb(fid: str) -> bytes | None:
        p = DEMO_ASSETS / f"thumb{int(fid) % 6}.jpg"
        return p.read_bytes() if p.exists() else None


class FakeLiveView(LiveView):
    """Replays a still frame at 10 fps instead of listening for the camera's UDP stream."""

    def start(self) -> None:
        if self._sock is not None or not (DEMO_ASSETS / "frame.jpg").exists():
            return
        self._sock = True  # marks running; there is no real socket
        self._stop.clear()
        frame = (DEMO_ASSETS / "frame.jpg").read_bytes()

        def loop():
            while not self._stop.wait(0.1):
                with self._cond:
                    self.frame, self.seq = frame, self.seq + 1
                    self._cond.notify_all()

        threading.Thread(target=loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._sock = None


class App:
    def __init__(self, cam: Lumix, demo: bool, dest: Path = DEFAULT_DEST):
        self.cam, self.demo, self.dest = cam, demo, dest
        self.live = FakeLiveView(cam) if demo else LiveView(cam)
        self.connected = False
        self.mode = "rec"
        self.lock = threading.Lock()
        self.files: dict[str, CamFile] = {}
        self.thumbs: dict[str, bytes] = {}
        self.thumb_lock = threading.Lock()
        self.job: dict = {"running": False}
        self.gimbal = FakeGimbal() if demo else GimbalLink()
        self.manual_until = 0.0  # manual gimbal moves pause tracking until then
        self.tracker = Tracker(self.live, self.gimbal, lambda: self.manual_until)

    # --- gimbal and tracking -----------------------------------------------------------------
    def gimbal_move(self, a: dict) -> dict:
        speeds = {k: float(a.get(k) or 0) for k in ("yaw", "pitch", "roll")}
        if any(speeds.values()):
            self.manual_until = time.monotonic() + 0.5
        self.gimbal.move(**speeds)
        return {"gimbal": self.gimbal.status()}

    def track(self, action: str, a: dict) -> dict:
        t = self.tracker
        t.configure(**{k: a.get(k) for k in ("rot", "mirror", "tilt", "gain", "speed")})
        if action == "start":
            t.start()
        elif action == "stop":
            t.stop()
        elif action == "enable":
            if a.get("on") and self.gimbal.state != "on":
                raise RuntimeError("connect the gimbal first")
            if a.get("on"):
                t.start()
            t.enable(bool(a.get("on")))
        elif action == "lock":
            if t.lock_at(float(a["x"]), float(a["y"])) is None:
                raise ValueError("no person at that point")
        elif action == "unlock":
            t.unlock()
        elif action == "recalibrate":
            t.recalibrate()
        elif action != "config":
            raise ValueError(f"unknown tracking action {action!r}")
        return {"track": t.status()}

    # --- files -------------------------------------------------------------------------------
    def _local(self, f: CamFile) -> bool:
        p = self.dest / f.filename
        return p.exists() and (not f.original or not f.original["size"] or p.stat().st_size == f.original["size"])

    def open_files(self) -> dict:
        """Switch to play mode (live view stops) and list the card."""
        with self.lock:
            self.live.stop()
            self.cam.play_mode()
            self.mode = "play"
            time.sleep(0.8)
            listing = self.cam.files()
        self.files = {f.id: f for f in listing}
        items = [dict(f.to_json(), local=self._local(f)) for f in listing]
        items.sort(key=lambda f: (f["date"], f["name"]), reverse=True)
        return {"files": items, "dest": str(self.dest)}

    def close_files(self) -> dict:
        if self.job.get("running"):
            raise CameraError("a download is still running")
        with self.lock:
            self.cam.rec_mode()
            self.mode = "rec"
            live_error = None
            try:
                self.live.start()
            except (CameraError, OSError) as e:
                live_error = str(e)
        return {"live_error": live_error}

    def thumb(self, fid: str) -> bytes | None:
        if fid in self.thumbs:
            return self.thumbs[fid]
        f = self.files.get(fid)
        r = f and (f.thumb or f.preview)
        if self.demo:
            return self.cam.thumb(fid)
        if not r:
            return None
        with self.thumb_lock:  # one request at a time; the camera's server is slow under load
            if fid not in self.thumbs:
                self.thumbs[fid] = fetch(r["url"])
        return self.thumbs[fid]

    def start_download(self, ids: list[str]) -> dict:
        if self.job.get("running"):
            raise CameraError("a download is already running")
        picked = [self.files[i] for i in ids if i in self.files and self.files[i].original]
        if not picked:
            raise ValueError("no downloadable files selected")
        if not self.demo:
            self.dest.mkdir(parents=True, exist_ok=True)
        self.job = {
            "running": True, "total": len(picked), "index": 0, "name": "",
            "total_bytes": sum(f.original["size"] for f in picked), "done_bytes": 0,
            "saved": [], "skipped": [], "errors": [], "dest": str(self.dest),
        }
        threading.Thread(target=self._download_all, args=(picked,), daemon=True).start()
        return self.job

    def _download_all(self, picked: list[CamFile]) -> None:
        job = self.job
        try:
            for n, f in enumerate(picked, 1):
                job.update(index=n, name=f.filename)
                base, size = job["done_bytes"], f.original["size"]
                if self._local(f):
                    job["skipped"].append(f.id)
                elif self.demo:
                    for k in range(1, 11):
                        time.sleep(0.05)
                        job["done_bytes"] = base + size * k // 10
                    job["saved"].append(f.id)
                else:
                    try:
                        download(f.original["url"], self.dest / f.filename,
                                 lambda done: job.__setitem__("done_bytes", base + done))
                        job["saved"].append(f.id)
                    except CameraError as e:
                        job["errors"].append({"id": f.id, "name": f.filename, "error": str(e)})
                job["done_bytes"] = base + size
        finally:
            job["running"] = False

    def reveal(self) -> None:
        """Open the download folder in the system file manager."""
        self.dest.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(self.dest)  # noqa: S606
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(self.dest)])

    def connect(self, ip: str) -> dict:
        with self.lock:
            self.live.stop()
            self.cam.ip = ip
            info = self.cam.connect()
            self.connected = True
            self.mode = "rec"
            live_error = None
            try:
                self.live.start()
            except (CameraError, OSError) as e:
                live_error = str(e)
            return {"access": info["access"], "state": info["state"], "live_error": live_error}

    def run(self, action: str, a: dict) -> dict | None:
        fast = bool(a.get("fast"))
        c = self.cam
        if action in ("zoom_in", "zoom_out"):
            c.zoom(action[5:], fast)
        elif action == "zoom_stop":
            c.zoom_stop()
        elif action in ("focus_near", "focus_far"):
            reply = c.focus(action[6:], fast)
            return {"values": reply.values}
        elif action == "touch_af":
            c.touch_af(float(a["x"]), float(a["y"]))
        elif action in ("rec_start", "rec_stop"):
            c.record(action == "rec_start")
        elif action == "capture":
            c.capture()
        else:
            raise ValueError(f"unknown action {action!r}")
        return None


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _json(self, obj: dict, status: int = 200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/":
                body = INDEX.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/info":
                self._json({"demo": app.demo, "ip": app.cam.ip, "connected": app.connected, "mode": app.mode,
                            "gimbal": app.gimbal.status(), "track": app.tracker.status()})
            elif path == "/api/gimbal":
                self._json(app.gimbal.status())
            elif path == "/api/track":
                self._json(dict(app.tracker.status(), gimbal=app.gimbal.status()))
            elif path == "/api/discover":
                if app.demo:
                    cams = [{"ip": DEFAULT_IP, "name": "G7-DEMO", "batt": "3/3"}]
                else:
                    cams = discover()
                self._json({"cameras": cams})
            elif path == "/api/files/job":
                self._json(app.job)
            elif path == "/api/files/thumb":
                fid = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("id", [""])[0]
                try:
                    data = app.thumb(fid)
                except CameraError:
                    data = None
                if not data:
                    return self.send_error(404)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/state":
                if not app.connected:
                    return self._json({"connected": False})
                try:
                    self._json({"connected": True, "ok": True, "state": app.cam.state()})
                except CameraError as e:
                    self._json({"connected": True, "ok": False, "error": str(e)})
            elif path == "/stream":
                self._stream()
            else:
                self.send_error(404)

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            t = time.perf_counter()
            try:
                a = self._body()
                if path == "/api/connect":
                    out = app.connect(a.get("ip") or DEFAULT_IP)
                elif path == "/api/cmd":
                    if not app.connected:
                        raise CameraError("not connected")
                    if app.mode != "rec":
                        raise CameraError("camera is in play mode; switch back to Shoot")
                    out = app.run(a.get("action", ""), a)
                elif path == "/api/gimbal/connect":
                    app.gimbal.connect()
                    out = {"gimbal": app.gimbal.status()}
                elif path == "/api/gimbal/move":
                    out = app.gimbal_move(a)
                elif path == "/api/gimbal/stop":
                    app.gimbal.stop()
                    out = {"gimbal": app.gimbal.status()}
                elif path.startswith("/api/track/"):
                    out = app.track(path.rsplit("/", 1)[1], a)
                elif path.startswith("/api/files/") and not app.connected:
                    raise CameraError("not connected")
                elif path == "/api/files/open":
                    out = app.open_files()
                elif path == "/api/files/close":
                    out = app.close_files()
                elif path == "/api/files/download":
                    out = app.start_download(list(a.get("ids") or []))
                elif path == "/api/files/reveal":
                    out = app.reveal()
                else:
                    return self.send_error(404)
                self._json({"ok": True, "ms": round(1000 * (time.perf_counter() - t)), **(out or {})})
            except (CameraError, ValueError, KeyError, OSError, RuntimeError, TimeoutError) as e:
                self._json({"ok": False, "ms": round(1000 * (time.perf_counter() - t)), "error": str(e)})

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seq = -1
            try:
                while True:
                    seq, frame = app.live.wait_frame(seq, 2.0)
                    if frame is None:
                        continue
                    head = b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(frame)
                    self.wfile.write(head + frame + b"\r\n")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    return Handler


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ip", default=DEFAULT_IP, help="camera address")
    p.add_argument("--port", type=int, default=8754)
    p.add_argument("--demo", action="store_true", help="simulate the camera")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--dest", type=Path, default=DEFAULT_DEST, help=f"download folder (default {DEFAULT_DEST})")
    args = p.parse_args()

    cam = FakeLumix(args.ip) if args.demo else Lumix(args.ip)
    app = App(cam, args.demo, args.dest)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app))
    server.daemon_threads = True
    url = f"http://localhost:{args.port}"
    print(f"Lumix remote on {url}{'  (demo)' if args.demo else ''}  - Ctrl+C to quit")
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.tracker.stop()
        app.gimbal.close()
        app.live.stop()


if __name__ == "__main__":
    main()
