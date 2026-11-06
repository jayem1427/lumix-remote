# lumix-remote

Control a Panasonic Lumix G7 over Wi-Fi from a browser: live view, power zoom, manual focus,
touch AF, record, photo, and copying files off the card.

- **No installs.** Python standard library only: one server script, one camera module, one HTML page.
- **Runs anywhere Python runs.** Windows, macOS and Linux; any browser on the same machine.
- **Built for rigs.** Hold-to-zoom and hold-to-focus controls, keyboard shortcuts, and view rotation for
  cameras mounted sideways or upside down on a gimbal.

It speaks the same HTTP API as the Panasonic Image App, so other Wi-Fi Lumix bodies of the same era
(GH4, G80/G85, GX85 and similar) will probably work. Only the G7 has been tested.

---

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Using the GUI](#using-the-gui)
- [Transferring files](#transferring-files)
- [Command line](#command-line)
- [How it works](#how-it-works)
- [cam.cgi reference](#camcgi-reference)
- [Using it from your own code](#using-it-from-your-own-code)
- [Troubleshooting](#troubleshooting)
- [Status](#status)
- [Roadmap](#roadmap)

## Requirements

| | |
|---|---|
| Python | 3.9 or newer |
| Camera | Lumix with Wi-Fi (tested on G7, firmware 2.8) |
| Zoom | A **power zoom** lens, e.g. Lumix 14-42 PZ or 45-175 PZ. A manual zoom ring can't be driven. |
| Focus | Any focus-by-wire Micro Four Thirds lens, with the camera's focus mode set to **MF** |
| Firewall | Incoming UDP on port **49199** allowed for Python (live view) |

## Quick start

1. **Start the camera's access point.** On the G7: **Wi-Fi > New Connection > Remote Shooting & View**.
2. **Join it** from your computer. The network is named after the camera, e.g. `G7-107EB7`.
3. **Run the smoke test.** The first time, the camera asks whether to allow **G7Remote**; choose Yes.

   ```
   python lumix.py test
   ```

   Each step prints PASS or FAIL with its timing:

   ```
   PASS  connect                         694 ms  ok
   PASS  getstate                         14 ms  batt=2/3, cammode=rec, ...
   PASS  zoom in 1s                     1282 ms
   PASS  zoom out 1s                    1025 ms
   ...
   PASS  touch AF center                  32 ms
   PASS  live view frame                1592 ms  saved liveview.jpg (22344 bytes)
   ```

4. **Start the GUI.** It opens http://localhost:8754 in your browser. Press **Connect**.

   ```
   python app.py
   ```

To try the interface without a camera, run `python app.py --demo`. It simulates the camera's replies
and a card full of files.

## Using the GUI

The **Shoot** tab is live view plus controls. Buttons act while held, and so do their keys, so a zoom
or focus move stops the moment you let go (or the window loses focus).

| Action | Control | Key |
|---|---|---|
| Zoom tele / wide | hold **T** / **W** | hold **E** / **Q** |
| Focus near / far | hold **Near** / **Far** | hold **A** / **D** |
| Autofocus at center | **AF center** | **F** |
| Autofocus at a point | click the live view | |
| Fast zoom and focus | **Speed: Fast** | hold **Shift** |
| Start / stop recording | red button | **R** |
| Take a photo | **Photo** | **P** |
| Rotate view 90 deg | **Rotate** | **V** (Shift+V turns back) |
| Mirror view | **Mirror** | **M** |
| Thirds grid | | **G** |

The header shows battery, recording time left on the card, and the link state. The footer shows the
last command, the camera's reply and the round-trip time, which is handy when something misbehaves.

**Rotate and Mirror** change only what you see, never the recording. Click-to-focus is mapped back to
the sensor, so it still lands where you click. Both settings are remembered by the browser.

## Transferring files

The **Files** tab switches the camera to playback, which pauses live view, and lists the card.

- Click tiles to select, Shift-click for a range. **Select new** picks everything not yet downloaded.
- Filter by **All / Photos / Videos**.
- **Download** saves to `~/Pictures/G7` (on Windows, `C:\Users\<you>\Pictures\G7`) with a progress bar.
  Files already there at the same size are marked **SAVED** and skipped, so repeat downloads are cheap.
- **Open folder** opens the download folder in your file manager.
- Switching back to **Shoot** returns the camera to record mode and restarts live view.

Choose another folder with `python app.py --dest D:\footage`.

Wi-Fi transfer is much slower than a card reader, so it suits photos and short clips better than an
hour of footage.

## Command line

```
python lumix.py test [--record]                 # smoke test; --record also shoots a 3 s clip
python lumix.py files                           # list every file and resource URL on the card
python lumix.py raw mode=camcmd value=zoomstop  # send any cam.cgi request, print the raw reply
python lumix.py --ip 192.168.54.1 ...           # camera address (that's the default)

python app.py [--ip IP] [--port 8754] [--dest DIR] [--demo] [--no-browser]
```

## How it works

```
 Browser (index.html)          app.py (localhost:8754)                 Camera (192.168.54.1)
 ---------------------         -----------------------                 ---------------------
 POST /api/cmd  ------------>  Lumix.zoom/focus/...   -- HTTP GET -->  cam.cgi           :80
 GET  /stream   <-- MJPEG ---  LiveView               <-- UDP JPEG --  live view    -> :49199
 POST /api/files/* -------->   MediaServer.list       -- SOAP ------>  UPnP ContentDirectory
                               download()             <-- HTTP ------  original files
```

The camera runs its own Wi-Fi access point and serves three things:

1. **`cam.cgi` (HTTP, port 80).** Every control is a GET request such as
   `/cam.cgi?mode=camcmd&value=tele-fast`. Replies are XML (`<camrply><result>ok</result>...`), except
   focus, which replies with comma-separated text (`ok,512,1024`).
2. **Live view (UDP).** After `mode=startstream&value=49199` the camera sends a stream of UDP packets
   to the requesting host, each holding a small header and then one JPEG frame. The stream stops unless
   it is polled, so `LiveView` calls `getstate` every 2 s and restarts the stream if frames stall.
   `app.py` re-serves the frames to the browser as MJPEG.
3. **File server (UPnP, playback mode only).** After `mode=camcmd&value=playmode` the camera exposes a
   UPnP ContentDirectory. `MediaServer` finds it through SSDP discovery (falling back to
   `:60606/Server0/CDS_control`) and pages through it with SOAP `Browse` calls. Each file comes with
   several resources: the original, a large preview and a thumbnail.

**Connecting.** The client first sends `mode=accctrl&type=req_acc` with a device ID and the name
`G7Remote`. If the camera answers `ok_under_research`, it is showing an approval prompt and the client
keeps asking until you accept. Then it confirms with `getstate` and switches to record mode.

**Safety.** Hold controls send a stop on release, on pointer cancel, on window blur and when the tab is
hidden. Focus repeats only after the previous request has returned, so slow replies don't pile up.

## cam.cgi reference

The requests this project uses. All are `http://192.168.54.1/cam.cgi?` plus the query.

| Purpose | Query |
|---|---|
| Request access | `mode=accctrl&type=req_acc&value=<uuid>&value2=<name>` |
| Status | `mode=getstate` |
| Record / playback mode | `mode=camcmd&value=recmode` / `playmode` |
| Zoom | `mode=camcmd&value=tele-fast` / `tele-normal` / `wide-normal` / `wide-fast` |
| Stop zoom | `mode=camcmd&value=zoomstop` |
| Focus step | `mode=camctrl&type=focus&value=wide-normal` (near), `tele-normal` (far); `-fast` for big steps |
| Touch AF | `mode=camctrl&type=touch&value=<x>/<y>&value2=on` (x, y in 0..1000) |
| Record | `mode=camcmd&value=video_recstart` / `video_recstop` |
| Photo | `mode=camcmd&value=capture`, then `capture_cancel` |
| Live view | `mode=startstream&value=<udp port>` / `mode=stopstream` |

`getstate` fields seen on the G7 include `batt` (`2/3`), `cammode`, `rec` (`on`/`off`),
`remaincapacity` (photos left), `video_remaincapacity` (seconds left), `sd_memory`, `temperature`,
`lens` and `version`.

## Using it from your own code

`lumix.py` is importable:

```python
from lumix import Lumix, LiveView

cam = Lumix("192.168.54.1")
cam.connect()                    # waits for approval on the camera if needed
print(cam.state()["batt"])

cam.zoom("in", fast=True)
cam.zoom_stop()
cam.focus("near")
cam.touch_af(0.5, 0.5)
cam.record(True)

live = LiveView(cam)
live.start()
seq, jpeg = live.wait_frame(0, timeout=3)
live.stop()
```

Failures raise `CameraError` with the request and the camera's reply, e.g.
`mode=camctrl&type=focus&value=wide-normal -> 'err_reject'`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `err_reject` on connect | The camera hasn't approved this device. Accept the prompt on the camera; the client waits up to 45 s. |
| `no reply from 192.168.54.1` | The computer isn't on the camera's Wi-Fi, or the camera has left remote mode. Rejoin the network. |
| Connect works, no live view | The firewall is blocking UDP 49199. Allow Python on private networks. |
| Focus says `err_reject` | Set the focus mode lever to **MF**. |
| Zoom says `needs a power zoom lens` | The lens has a manual zoom ring. |
| Near and Far are reversed | Swap the two values of `FOCUS_WORD` at the top of `lumix.py`. |
| Phone app disconnects | The camera takes one Wi-Fi client at a time. Disconnect the other device first. |
| Page shows "Connect to the camera" and nothing works | You opened `index.html` straight from disk. Run `python app.py` and use http://localhost:8754. |
| `Address already in use` | Another copy of `app.py` is running. Close it, or pass `--port`. |

## Status

| Feature | G7, tested |
|---|---|
| Connect and approval prompt | yes |
| Status readout | yes |
| Power zoom | yes |
| Touch AF | yes |
| Live view | yes |
| Manual focus | not yet (needs MF mode) |
| Record and photo | not yet |
| File listing and download | not yet (works in `--demo`) |

## Roadmap

- **Gimbal bridge.** Run `lumix.py` on a Raspberry Pi Zero 2 W mounted on a DJI RS 3 Mini. A rotary
  encoder drives zoom and focus. A later step would map the gimbal's own camera-control button
  (half-press AF, press record, hold photo) by having the Pi stand in for a supported camera on the
  gimbal's USB-C camera-control port.
- Serve the GUI from the bridge so a phone can be the monitor.

## Disclaimer

An independent project, not affiliated with or endorsed by Panasonic or DJI. Lumix is a trademark of
Panasonic. The API was learned from how the camera answers its own app; use at your own risk.
