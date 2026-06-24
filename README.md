# Tello Ground Control

A minimal web UI (buttons) + Flask backend for flying a DJI Tello (or
Tello-compatible) drone from your browser.

## How it works

The Tello has no Wi-Fi-to-internet bridge and no HTTP API — it only speaks a
small UDP text protocol on port 8889 directly to whatever device is connected
to its Wi-Fi. Browsers can't send raw UDP, so this project uses a tiny local
Flask server as the bridge:

```
Browser (buttons) --HTTP--> server.py --UDP--> Tello drone
```

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Power on the Tello. On your computer, connect to its Wi-Fi network
   (it shows up as something like `TELLO-XXXXXX`). Your computer must stay
   on this network for the whole flight — it's acting as the remote control.
3. Start the server:
   ```
   python server.py
   ```
4. Open **http://localhost:5001** in your browser.
   (Port 5000 is taken by macOS AirPlay Receiver; the server uses 5001 by
   default. To pick a different port, run `PORT=5002 python server.py`.)
5. Click **Connect to drone** first — this puts the Tello into "SDK mode"
   and is required before takeoff or any movement command will work.

## Layout

The interface is split into two columns so the live camera feed and the flight
controls are visible at the same time:

- **Left** — the camera feed (with a live HUD overlay) and the captured-photos
  gallery.
- **Right** — link/battery, the flight controls, live telemetry, and the log.

On narrow screens (phones, small windows) the two columns stack vertically.

## Flying from your phone

The Tello's WiFi has **no internet connection**, and your computer has to stay
joined to it to talk to the drone — so internet tunnels like ngrok don't work
in the normal single-WiFi setup. The easy way to use your phone is to put it on
the **same Tello WiFi network** and point its browser at your computer:

1. Connect your **computer** to the Tello WiFi (`TELLO-XXXXXX`) and start the
   server (`python server.py`).
2. Find your computer's IP on that network — on macOS:
   `ipconfig getifaddr en0` (it'll be something like `192.168.10.2`).
3. Connect your **phone** to the same `TELLO-XXXXXX` WiFi.
4. In the phone's browser open `http://<computer-ip>:5001`
   (e.g. `http://192.168.10.2:5001`).

This needs no internet, no ngrok, and has the lowest latency — which matters
when flying.

> **Want to control it over the internet (ngrok)?** It's only possible if your
> computer has a *second* internet path while its WiFi stays on the drone — e.g.
> plug in **Ethernet**, or **USB-tether** your phone for internet. With WiFi on
> the Tello and internet on the other interface, you can run `ngrok http 5001`
> and use the public URL. Expect higher latency, so it's not ideal for real-time
> flying.

## Using the controls

- **Camera** — click **Start video** (after Connect) to see the live feed.
  Click **Stop video** to turn it off and free up bandwidth/CPU.
- **📷 Take photo** — saves the current camera frame as a JPEG on the server
  (in the `snapshots/` folder) and adds it to the **Photos** gallery. Click any
  thumbnail to download the full-size image. Available while the video is live.
- **Telemetry** — once connected, this panel shows live flight data broadcast
  by the drone: height, time-of-flight distance, barometric altitude, battery,
  speed (X/Y/Z), attitude (pitch/roll/yaw), temperature, and motor time. Key
  values are also overlaid on the camera feed as a HUD.
- **Joystick** — two on-screen analog sticks (great on a phone/touch screen)
  for smooth, continuous flight, instead of fixed-distance steps:
  - **Left stick** — up/down = throttle (altitude), left/right = rotate (yaw).
  - **Right stick** — up/down = forward/back, left/right = strafe.
  - **Speed** slider scales how aggressive the sticks are.
  - Hold to fly, release to hover. Take off first. These use the Tello's
    real-time `rc` command, so there's no per-move log line.
- **D-pad vs joystick** — the D-pad/ALT buttons move a precise fixed distance
  (good for indoors/careful moves); the joystick is fluid analog control.
- **Keyboard** — with the page focused: `WASD` to move, `↑/↓` for altitude,
  `←/→` to rotate, and `Space` to take a photo.
- **Takeoff / Land** — start/end the flight.
- **D-pad (▲▼◀▶)** — move forward/back/left/right by the "cm step" amount.
- **ALT ▲▼** — move up/down by the same step amount.
- **↺ ↻** — rotate counter-clockwise/clockwise by the "° turn" amount.
- **cm step / ° turn** — adjust how far each button press moves/rotates
  the drone (20–500 cm, 1–360°).
- **Emergency stop** — immediately cuts motor power. The drone will drop
  out of the air — only use this if it's about to hit something and
  landing normally isn't fast enough.
- The **Log** panel shows every command sent and the drone's reply, which
  is the easiest way to see what went wrong if a button doesn't seem to work.

## How the camera feed works

The Tello doesn't speak HTTP for video either — once you send it `streamon`,
it just starts firing raw H.264 video packets over UDP at your computer on
port 11111, with no acknowledgement or container format. The server:

1. Sends `streamon` to the drone.
2. Opens that UDP port with OpenCV (which uses FFmpeg under the hood) to
   decode the H.264 into individual frames.
3. Re-encodes each frame as a JPEG and serves them as an MJPEG stream at
   `/video_feed` — a format browsers can display directly in an `<img>` tag
   with no extra JavaScript video library needed.

This means **OpenCV needs working FFmpeg support**, which the
`opencv-python-headless` wheel installed by `requirements.txt` includes out
of the box on Windows/Mac/Linux — you shouldn't need to install FFmpeg
separately.

Photos work the same way: **📷 Take photo** simply grabs the most recently
decoded video frame and writes it to disk, so the video stream must be running
to capture one.

## How telemetry works

In SDK mode the Tello continuously *broadcasts* a state string several times a
second to UDP port 8890, e.g. `pitch:0;roll:0;yaw:0;...;h:0;bat:84;baro:104.71;...`.
The server listens on that port, parses the latest snapshot, and exposes it at
`GET /api/state`; the browser polls it twice a second to update the telemetry
tiles and the camera HUD.

## Safety notes (please read)

- Always fly somewhere open, away from people, animals, and obstacles,
  ideally outdoors with good GPS-free visual reference (the basic Tello has
  no GPS and can drift, especially low battery or low light).
- Keep your computer connected to the Tello's Wi-Fi the entire flight — if
  the link drops, the drone will hover briefly then attempt to land on its
  own.
- Check battery before flying; the SDK link can get unreliable below ~10%.
- The "Emergency stop" button cuts power instantly and the drone *will*
  fall — it's a last resort, not a normal landing method.

## Troubleshooting

- **"timeout: no reply from drone"** — check that your computer is actually
  connected to the Tello's Wi-Fi network (not your home Wi-Fi), and that no
  other app (like the official Tello app) is also connected to it.
- **Buttons stay greyed out** — click "Connect to drone" first; movement and
  takeoff are disabled until that succeeds.
- **Works once, then stops responding** — the Tello can drop its Wi-Fi
  under load; move closer, then click Connect again.
- **Video says "Start video" but nothing appears** — give it a couple of
  seconds; the first frame takes a moment to arrive. If it never shows up,
  check the terminal running `server.py` for errors — it usually means
  OpenCV couldn't open the UDP stream (rare, but can happen on some
  Windows firewall configurations — try allowing Python through the
  Windows Defender Firewall prompt if one appears).
- **Video is laggy/choppy** — this is normal for Tello's Wi-Fi video at any
  distance; move closer to the drone and avoid other Wi-Fi traffic if
  possible.

## Extending this

- `server.py` is a thin wrapper around the raw Tello SDK text commands
  (`command`, `takeoff`, `land`, `up <cm>`, `cw <deg>`, etc.) — add new
  routes the same way for things like `streamon` (video) or `flip <dir>`.
- The `flip` endpoint is already wired up in the backend
  (`POST /api/flip` with `{"direction": "f"|"b"|"l"|"r"}`) if you want to
  add a button for it in `templates/index.html`.
