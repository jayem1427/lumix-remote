# lumix-remote

Control a Panasonic Lumix G7 over Wi-Fi from a browser: live view, power zoom, manual focus,
touch AF, record, photo, and copying files off the card. Pure Python standard library, no installs.

It speaks the same HTTP API as the Panasonic Image App (`cam.cgi` on the camera's access point),
so other Lumix bodies from the same era will likely work too, but only the G7 has been tested.

## Requirements

- Python 3.9 or newer (Windows, macOS or Linux)
- A Lumix camera with Wi-Fi
- For zoom: a power zoom lens (e.g. 14-42 PZ, 45-175 PZ)
- For manual focus: the camera's focus mode set to **MF**

## Quick start

1. On the camera: **Wi-Fi > New Connection > Remote Shooting & View**.
2. Join the camera's network (`G7-xxxxxx`) from your computer.
3. Run the smoke test. The first time, approve **G7Remote** on the camera screen.

   ```
   python lumix.py test
   ```

4. Start the GUI. It opens at http://localhost:8754.

   ```
   python app.py
   ```

Try the UI without a camera with `python app.py --demo`.

## Controls

| Action | Key |
|---|---|
| Zoom tele / wide (hold) | E / Q |
| Focus near / far (hold) | A / D |
| AF at center | F |
| AF at a point | click the live view |
| Record / photo | R / P |
| Fast speed | hold Shift |
| Rotate view 90 deg (Shift: back) | V |
| Mirror view | M |
| Thirds grid | G |

## Files

The **Files** tab switches the camera to playback (live view pauses), lists the card, and downloads
what you select to `~/Pictures/G7`. Already-saved files are marked and skipped. Change the folder with
`python app.py --dest D:\footage`. `python lumix.py files` prints the raw listing from the camera.

## Command line

```
python lumix.py test [--record]                 # smoke test; --record also shoots a 3 s clip
python lumix.py files                           # list the card
python lumix.py raw mode=camcmd value=zoomstop  # send any cam.cgi request
python app.py [--ip 192.168.54.1] [--port 8754] [--dest DIR] [--demo] [--no-browser]
```

## Troubleshooting

- **`err_reject` on connect**: approve the connection prompt on the camera; the script waits up to 45 s.
- **No live view**: allow Python through the firewall for incoming UDP on port 49199.
- **Focus `err_reject`**: switch the camera to MF.
- **Near and Far reversed**: swap the values in `FOCUS_WORD` at the top of `lumix.py`.
- **Only one client**: the camera accepts one Wi-Fi client at a time, so disconnect the phone app first.

## Layout

- `lumix.py`: camera client (`cam.cgi`), UDP live view receiver, UPnP file listing, CLI
- `app.py`: local web server and a simulated camera for `--demo`
- `index.html`: the GUI
