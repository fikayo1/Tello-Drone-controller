# Tello Ground Control — Technical Presentation Guide

A speaker's guide for presenting this project. It explains the architecture,
the protocol, every subsystem, the engineering trade-offs, and the bugs we hit
and fixed — plus a slide outline, a live-demo script, and likely Q&A.

> **One-line pitch:** *A browser becomes a drone remote control — a small Flask
> server translates web clicks into the Tello's raw UDP radio protocol, and
> streams the drone's video and live telemetry back into the page.*

---

## 1. The core problem (start your talk here)

A DJI Tello drone has **no internet bridge and no HTTP API**. It only speaks a
small **UDP text protocol** over the Wi-Fi hotspot it broadcasts. Your laptop
joins that hotspot — so **your laptop literally *is* the remote control**.

But there's a catch: **browsers cannot send raw UDP packets.** They speak
HTTP/WebSocket, not UDP. So a web UI can't talk to the drone directly.

**The whole project exists to bridge that gap:**

```
  Browser  ──HTTP──►  Flask server (server.py)  ──UDP──►  Tello drone
 (buttons,           "the translator /            (192.168.10.1)
  joystick)           bridge"            ◄──UDP──  replies, telemetry, video
```

Everything else — video, telemetry, joystick, photos — is a variation on this
one idea: *the server speaks UDP to the drone and HTTP to the browser.*

---

## 2. Architecture at a glance

Two files do all the work:

| File | Role |
|------|------|
| `server.py` | The bridge. Flask web server + UDP sockets + background threads. |
| `templates/index.html` | The entire frontend — HTML, CSS, and vanilla JS in one file. No build step, no framework. |

**Design philosophy to mention:** deliberately minimal. No database, no
front-end framework, no JS bundler. One Python file, one HTML file. Easy to
read, easy to demo, easy to extend.

The drone uses **four UDP ports**, and understanding them is the key to the
whole system:

| Port | Direction | Purpose | Where in code |
|------|-----------|---------|---------------|
| **8889** | server → drone | Send text commands (`takeoff`, `rc`, …) | `TELLO_PORT` |
| **9000** | drone → server | Command replies (`ok` / `error`) | `LOCAL_PORT` |
| **8890** | drone → server | Telemetry broadcast (height, battery, …) | `STATE_PORT` |
| **11111** | drone → server | Raw H.264 video stream | `VIDEO_PORT` |

> Talking point: *"The drone is chatty over four separate channels. The server's
> job is to manage all four and present them to the browser as simple HTTP."*

---

## 3. The command channel (the heart of the bridge)

This is the simplest and most important piece. Show this code:

```python
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # UDP socket
sock.bind(("", LOCAL_PORT))                              # listen on :9000

def send_command(command: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    _reply_event.clear()
    sock.sendto(command.encode("utf-8"), (TELLO_IP, TELLO_PORT))  # → :8889
    if _reply_event.wait(timeout):
        with _lock:
            return _last_reply["text"]
    return "timeout: no reply from drone (check Wi-Fi connection)"
```

**The key challenge: UDP is fire-and-forget.** When you `sendto`, you get no
acknowledgement. The reply comes back **asynchronously** on a *different* port
(9000), at some unknown later time. So we can't just "send and read the reply"
on one line.

**The solution — a listener thread + an Event:**

```python
def _listen():                       # runs forever in the background
    while True:
        data, _addr = sock.recvfrom(1518)         # block until a packet arrives
        with _lock:
            _last_reply["text"] = data.decode(...).strip()
        _reply_event.set()           # wake up whoever is waiting

threading.Thread(target=_listen, daemon=True).start()
```

The flow to explain on a slide:

1. A web request calls `send_command("takeoff")`.
2. It clears the event, sends the UDP packet, then **blocks** on
   `_reply_event.wait(timeout)`.
3. Meanwhile the **listener thread** receives the drone's reply on port 9000,
   stores it, and calls `_reply_event.set()`.
4. The waiting request wakes up, reads the stored reply, and returns it as JSON.
5. If 7 seconds pass with no reply → timeout message (almost always "you're not
   on the drone's Wi-Fi").

> **Concurrency vocabulary to drop:** `threading.Event` is the
> producer/consumer handoff; `threading.Lock` protects the shared
> `_last_reply` dict from being read and written at the same time; `daemon=True`
> means the thread dies automatically when the program exits.

Every button on the page ultimately becomes one `send_command(...)` call:
`takeoff`, `land`, `emergency`, `up 30`, `cw 45`, `flip f`, `battery?`.

---

## 4. SDK mode — why "Connect" must be clicked first

```python
@app.route("/api/connect", methods=["POST"])
def api_connect():
    reply = send_command("command")   # puts the Tello into "SDK mode"
    return jsonify(ok=ok(reply), reply=reply)
```

The Tello ignores every command until it receives the literal word `command`,
which switches it into **SDK mode**. That's why the UI disables Takeoff, the
joystick, etc. until "Connect to drone" succeeds — it's not just UX polish, the
drone genuinely won't respond otherwise. Sending `command` also makes the drone
start broadcasting its telemetry on port 8890 (next section).

---

## 5. Telemetry — the drone broadcasts, we listen

**Feature:** the right-hand panel shows live height, distance, barometric
altitude, battery, speed (X/Y/Z), attitude (pitch/roll/yaw), temperature, etc.,
and a few of those are overlaid on the video as a HUD.

**How it works:** once in SDK mode, the Tello *automatically broadcasts* a
state string several times a second to port 8890 — we didn't have to ask for
each value. It looks like:

```
pitch:0;roll:0;yaw:0;vgx:0;vgy:0;vgz:0;templ:60;temph:62;tof:10;h:0;bat:84;baro:104.71;time:0;agx:-3.00;agy:5.00;agz:-998.00;
```

A second listener thread keeps the latest snapshot:

```python
def _state_listen():
    while True:
        data, _addr = state_sock.recvfrom(1518)
        parsed = _parse_state(data.decode(errors="replace"))
        if parsed:
            with _state_lock:
                _state["data"] = parsed
                _state["ts"] = time.time()    # remember WHEN we last heard from it
```

`_parse_state` just splits on `;` and `:` and converts each value to a number.

The browser polls `GET /api/state` **twice a second** and redraws the tiles.
Two design touches worth mentioning:

- **`fresh` flag:** the server compares "now" to the last-received timestamp;
  if it's older than 3 seconds, it tells the UI the data is stale (so the panel
  shows "no data" instead of frozen lies if the link drops).
- **Labels/units live on the server** (`STATE_FIELDS` dict). The frontend
  renders whatever fields it's given, so adding a new readout is a one-line
  change and the UI adapts automatically — *self-describing API.*

> Contrast to highlight: **commands are pull** (we ask, we wait for a reply),
> **telemetry is push** (the drone broadcasts, we just catch it). Different
> problem, different pattern.

---

## 6. Video — the cleverest part

**The problem:** after `streamon`, the Tello fires **raw H.264 video packets**
over UDP port 11111 — no container, no HTTP, no acknowledgement. Browsers can't
play that.

**The pipeline** (`_video_loop`, runs in its own thread):

```
Tello ─H.264/UDP:11111─►  OpenCV (VideoCapture + FFmpeg)  ─decode→ frames
                          ─re-encode each frame as JPEG (quality 80)
                          ─store the latest JPEG in memory
```

Then we serve it to the browser as **MJPEG** (Motion JPEG) — a stream of JPEGs
separated by boundary markers:

```python
def _mjpeg_generator():
    boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
    while _video_running.is_set():
        frame = _video_frame["jpeg"]
        yield boundary + frame + b"\r\n"     # keep yielding forever
        time.sleep(0.03)                     # ~30 fps cap
```

served at `/video_feed` with mimetype `multipart/x-mixed-replace`.

**The elegant payoff:** the browser displays it with literally one line —
`<img src="/video_feed">`. The `multipart/x-mixed-replace` content type tells
the browser "each part *replaces* the previous image," so a plain `<img>` tag
animates as video. **No JavaScript video library, no WebRTC, no codec in the
browser.** That's the trick to emphasize.

Robustness details you can mention:
- `CAP_PROP_BUFFERSIZE = 1` → keep latency low (don't buffer old frames).
- A "miss counter" reopens the capture if the stream goes stale (Tello Wi-Fi
  drops happen constantly).
- A `threading.Event` (`_video_running`) cleanly starts/stops the whole
  pipeline when you click Start/Stop video.

---

## 7. Photos — snapshots from the live frame

This one is almost free *because* of the video design. Since the latest decoded
frame is already sitting in memory as a JPEG:

```python
@app.route("/api/snapshot", methods=["POST"])
def api_snapshot():
    with _video_lock:
        frame = _video_frame["jpeg"]        # the most recent decoded frame
    if frame is None:
        return jsonify(ok=False, reply="...start the video stream first"), 409
    filename = "tello_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3] + ".jpg"
    with open(os.path.join(SNAPSHOT_DIR, filename), "wb") as fh:
        fh.write(frame)
    return jsonify(ok=True, filename=filename)
```

Take a photo → grab that in-memory JPEG → write it to the `snapshots/` folder
→ return the filename. The frontend then shows a thumbnail gallery (served by
`/snapshots/<file>`) and a camera-flash animation. Point: *we reused the video
frame; capturing a photo costs almost nothing.*

---

## 8. The joystick — analog control, and the hardest lesson

**Why a joystick?** The buttons send *discrete* moves ("forward 30 cm"). For
real flying you want *continuous analog* control. The Tello SDK supports this
via the `rc a b c d` command, four channels each `-100..100`:

| channel | axis | meaning |
|---------|------|---------|
| a | left/right | roll (strafe) |
| b | forward/back | pitch |
| c | up/down | throttle (altitude) |
| d | yaw | rotation |

The two on-screen sticks map to these channels; a 10 Hz loop posts the current
stick position to `/api/rc`, which forwards `rc a b c d` to the drone.

**Crucial difference from commands:** `rc` is **fire-and-forget — the drone
sends no reply**. So we added a separate sender:

```python
def send_command_noreply(command: str) -> None:
    sock.sendto(command.encode("utf-8"), (TELLO_IP, TELLO_PORT))  # don't wait
```

### This is your best storytelling moment — the two bugs we hit

Telling the debugging story makes the talk memorable and shows real engineering.

**Bug #1 — "Release the stick, drone keeps climbing forever."**
The Tello *holds the last `rc` value until it receives a new one.* My first
version sent the "stop" (`rc 0 0 0 0`) only **once** on release. But `rc` rides
on **lossy UDP** — if that single stop packet is dropped, the drone *never*
hears "stop" and keeps climbing. **Fix attempt:** resend the state continuously
at 10 Hz so a lost packet is corrected ~100 ms later.

**Bug #2 — the overcorrection: "Land won't land, arrows don't work."**
Streaming `rc` *all the time* monopolized the drone's single control channel.
Press **Land** → drone starts descending → 100 ms later the next `rc 0 0 0 0`
arrives and effectively says "hold," cancelling the landing. Same for the move
arrows: each `forward 30` was instantly overridden by the next `rc` packet.

**The final design (the lesson):**
- Stream `rc` **only while a stick is actually held.**
- On release, fire a **short burst** of ~6 `rc 0 0 0 0` packets over ~0.6 s
  (loss-tolerant stop), then **go silent** so Land / move / takeoff have the
  control channel to themselves.

```js
setInterval(() => {
  if (!rcEnabled) return;
  if (anyJoyHeld()) {              // a stick is down → stream live values
    stopBurst = 6;                 // arm the stop-burst for the eventual release
    post('/api/rc', rcState);
  } else if (stopBurst > 0) {      // just released → send a few zeros, then quiet
    stopBurst--;
    post('/api/rc', {lr:0, fb:0, ud:0, yaw:0});
  }
}, 100);
```

> **The takeaway line for your audience:** *"On a lossy network, 'send the stop
> command once' is a safety bug. But 'send it constantly' starved every other
> command. The right answer was a middle path — stream only while active, then
> a redundant stop burst, then silence."* That's a great systems-thinking point.

---

## 9. Frontend notes (keep this section short)

- **Single file, vanilla JS.** No React/Vue. A tiny `$ = id => document.getElementById(id)`
  helper and `fetch()` calls. Chosen for transparency and zero build tooling.
- **Two-column responsive layout** (CSS grid): camera + photos on the left,
  controls + telemetry + log on the right; collapses to one column under 900 px
  for phones.
- **The Log panel** echoes every command and the drone's raw reply — your best
  friend during the live demo and for debugging on stage.
- **Joystick uses Pointer Events**, so the same code handles mouse *and* touch —
  that's why it works on a laptop and a phone with no changes.
- **Keyboard shortcuts:** WASD / arrows / Space (photo).

---

## 10. Networking reality: phone control & why ngrok doesn't fit

A good "I really understood the constraints" slide.

- The drone's Wi-Fi has **no internet.** Your laptop must stay on it to fly.
- **Phone control the easy way:** put the phone on the *same* Tello Wi-Fi and
  open `http://<laptop-ip>:5001` (e.g. `192.168.10.2:5001`). No internet, no
  tunnel, lowest latency.
- **Why not ngrok?** ngrok needs internet to expose the server publicly, but the
  laptop's only network *is* the internet-less drone Wi-Fi. With a single
  adapter you can't be on both. It's only possible if the laptop has a *second*
  internet path (Ethernet or USB-tether) while Wi-Fi stays on the drone.
- **macOS gotcha we hit:** port 5000 is taken by macOS **AirPlay Receiver**, so
  the server defaults to **5001**. (A fun, relatable debugging anecdote: the UI
  threw "Unexpected token '<'" because requests were hitting AirPlay's HTML page
  instead of our JSON API.)

---

## 11. Suggested slide outline (~12–15 min)

1. **Title** — "Flying a drone from a web browser."
2. **The problem** — drone speaks UDP, browsers can't. (Section 1 diagram.)
3. **Architecture** — the bridge + the four UDP ports table. (Section 2.)
4. **Command channel** — UDP + listener thread + Event. (Section 3.)
5. **SDK mode** — why "Connect" comes first. (Section 4.)
6. **Telemetry** — push vs pull; self-describing API. (Section 5.)
7. **Video** — H.264 → OpenCV → MJPEG → `<img>`. The clever bit. (Section 6.)
8. **Photos** — reusing the frame. (Section 7.)
9. **Joystick + the bug story** — your highlight. (Section 8.)
10. **Networking constraints** — phone/ngrok/AirPlay. (Section 10.)
11. **Live demo.** (Script below.)
12. **Lessons / what I'd do next** (Section 12) + Q&A.

---

## 12. "What I learned / what's next" (good closing slide)

**Lessons:**
- Async I/O over an unreliable transport (UDP) forces you to think about
  threads, timeouts, and packet loss explicitly.
- Idempotency & control-channel ownership matter: who is allowed to send, and
  how often, is a *design decision* with safety consequences.
- The simplest viable tech often wins (MJPEG + `<img>` vs a full WebRTC stack).

**Possible next steps (shows vision):**
- Replace 10 Hz HTTP polling for `rc`/telemetry with a **WebSocket** for lower
  latency and less overhead.
- A safety **failsafe**: auto-hover/land if the browser stops sending heartbeats.
- Record video to disk; computer-vision (face/object tracking) on the frames.
- Multi-drone support; mission scripting (a queue of commands).

---

## 13. Live-demo script (rehearse this)

> Fly somewhere open. Have Land and Emergency ready. Battery > 30%.

1. Show the terminal: `python server.py` → "Serving on :5001". Note the threads
   already listening for replies/telemetry/video.
2. Open `http://localhost:5001`. Point out the **Log** panel.
3. Click **Connect** → log shows `command -> ok`. Telemetry tiles come alive,
   battery appears. *"That one word put it in SDK mode and started the
   broadcast."*
4. Click **Start video** → live feed + HUD overlay. *"H.264 decoded server-side,
   re-served as MJPEG into a plain `<img>` tag."*
5. **Take photo** → flash animation, thumbnail appears in the gallery.
6. **Takeoff** → hovers. Use the **arrows** for a precise 30 cm move, then the
   **joystick** for smooth flight. Watch height change in telemetry.
7. **Land.** Mention Emergency exists as the last resort.
8. (Optional) Pull up the phone on the same Wi-Fi to show it works there too.

**Backup plan if Wi-Fi/drone misbehaves on stage:** keep a short screen
recording of a working flight, and walk the code while it plays. The Log panel
screenshots also make great fallback slides.

---

## 14. Likely Q&A (prep answers)

- **Why Flask and not just a desktop app?** A browser UI runs on anything
  (laptop/phone) with zero install, and HTTP is trivial to build buttons on.
- **Why threads instead of `asyncio`?** The blocking parts (UDP recv, OpenCV
  capture) are naturally thread-shaped; threads kept it simple. asyncio/WebSocket
  is the natural next iteration.
- **Is it secure?** No auth — it's meant for a local, trusted Wi-Fi (the drone's
  own hotspot). Don't expose it to the open internet without adding auth/TLS.
- **What's the latency?** Command round-trip is tens of ms on the drone Wi-Fi;
  video is laggier and bursty because it's Tello Wi-Fi H.264 over UDP.
- **What happens if Wi-Fi drops mid-flight?** The Tello has its own failsafe —
  it hovers, then auto-lands after a timeout. Our `fresh` flag surfaces the loss
  in the UI.
- **Why does it hold the last `rc`?** That's the SDK's design for smooth analog
  control — which is exactly what made the "send stop once" bug dangerous.

---

## 15. 30-second elevator version (if you're time-boxed)

> "A Tello drone only speaks a raw UDP text protocol, and browsers can't send
> UDP. So I built a small Flask server that bridges the two: it sends commands
> to the drone on one UDP port, catches replies on another, listens to a
> telemetry broadcast on a third, and decodes the H.264 video on a fourth —
> re-serving it as MJPEG so a plain `<img>` tag shows live video. The web UI
> gives you buttons, a live data readout, photo capture, and two analog
> joysticks. The trickiest part was the joystick: because `rc` commands ride on
> lossy UDP and the drone holds the last value it received, I had to design the
> control loop carefully so the drone always stops when you let go — without
> drowning out the land and move commands."
