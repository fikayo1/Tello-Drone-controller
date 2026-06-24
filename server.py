"""
Tello Drone Web Controller — backend
-------------------------------------
A small Flask server that bridges a browser-based button UI to a DJI Tello
(or Tello-compatible) drone using its built-in UDP SDK protocol.

  Browser (buttons) --HTTP--> this server --UDP:8889--> Tello drone
                                          <--UDP:9000-- (replies)

SETUP
  1. Power on the Tello and connect your computer to its Wi-Fi
     network (named something like "TELLO-XXXXXX").
  2. pip install -r requirements.txt
  3. python server.py
  4. Open http://localhost:5001 in a browser.

The Tello must stay in range of your computer's Wi-Fi the whole time —
there is no separate "remote" link, your laptop/phone *is* the remote.
"""

import os
import socket
import threading
import time
from datetime import datetime

import cv2
from flask import Flask, render_template, jsonify, request, Response, send_from_directory

TELLO_IP = "192.168.10.1"   # Fixed IP of the Tello when connected to its Wi-Fi
TELLO_PORT = 8889           # Tello command port
LOCAL_PORT = 9000           # Port we listen on for command replies
STATE_PORT = 8890           # Port the Tello broadcasts its telemetry/state to
VIDEO_PORT = 11111          # Port the Tello streams raw H.264 video to
DEFAULT_TIMEOUT = 7         # seconds to wait for a reply before giving up

# Where captured photos are written to on the server.
SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
os.makedirs(SNAPSHOT_DIR, exist_ok=True)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# UDP link to the drone
# ---------------------------------------------------------------------------

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("", LOCAL_PORT))

_lock = threading.Lock()
_last_reply = {"text": None}
_reply_event = threading.Event()


def _listen():
    """Background thread that just waits for whatever the Tello sends back."""
    while True:
        try:
            data, _addr = sock.recvfrom(1518)
        except OSError:
            break
        with _lock:
            _last_reply["text"] = data.decode(errors="replace").strip()
        _reply_event.set()


threading.Thread(target=_listen, daemon=True).start()


def send_command(command: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Send one SDK text command to the Tello and wait for its reply."""
    _reply_event.clear()
    sock.sendto(command.encode("utf-8"), (TELLO_IP, TELLO_PORT))
    if _reply_event.wait(timeout):
        with _lock:
            return _last_reply["text"]
    return "timeout: no reply from drone (check Wi-Fi connection)"


def send_command_noreply(command: str) -> None:
    """Fire a command without waiting for a reply. Used for the high-frequency
    'rc' joystick command, which the Tello does not acknowledge."""
    sock.sendto(command.encode("utf-8"), (TELLO_IP, TELLO_PORT))


def ok(reply: str) -> bool:
    return reply.strip().lower() == "ok"


# ---------------------------------------------------------------------------
# Telemetry / state link
#
# Once in SDK mode the Tello *broadcasts* a full state string several times a
# second to UDP port 8890. It looks like:
#   pitch:0;roll:0;yaw:0;vgx:0;vgy:0;vgz:0;templ:60;temph:62;tof:10;h:0;
#   bat:84;baro:104.71;time:0;agx:-3.00;agy:5.00;agz:-998.00;
# We just listen for it continuously and keep the latest parsed snapshot.
# ---------------------------------------------------------------------------

state_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
state_sock.bind(("", STATE_PORT))

_state_lock = threading.Lock()
_state = {"data": {}, "ts": 0.0}

# Human-friendly labels + units for the raw Tello field names.
STATE_FIELDS = {
    "bat":   ("Battery", "%"),
    "h":     ("Height", "cm"),
    "tof":   ("Distance (ToF)", "cm"),
    "baro":  ("Barometer", "cm"),
    "vgx":   ("Speed X", "cm/s"),
    "vgy":   ("Speed Y", "cm/s"),
    "vgz":   ("Speed Z", "cm/s"),
    "pitch": ("Pitch", "°"),
    "roll":  ("Roll", "°"),
    "yaw":   ("Yaw", "°"),
    "templ": ("Temp (low)", "°C"),
    "temph": ("Temp (high)", "°C"),
    "agx":   ("Accel X", ""),
    "agy":   ("Accel Y", ""),
    "agz":   ("Accel Z", ""),
    "time":  ("Motor time", "s"),
}


def _parse_state(raw: str) -> dict:
    """Turn the 'key:value;key:value;' state string into a number dict."""
    out = {}
    for pair in raw.strip().rstrip(";").split(";"):
        if ":" not in pair:
            continue
        key, _, value = pair.partition(":")
        try:
            num = float(value)
            out[key] = int(num) if num.is_integer() else round(num, 2)
        except ValueError:
            out[key] = value
    return out


def _state_listen():
    """Background thread: keep the latest telemetry snapshot up to date."""
    while True:
        try:
            data, _addr = state_sock.recvfrom(1518)
        except OSError:
            break
        parsed = _parse_state(data.decode(errors="replace"))
        if parsed:
            with _state_lock:
                _state["data"] = parsed
                _state["ts"] = time.time()


threading.Thread(target=_state_listen, daemon=True).start()


# ---------------------------------------------------------------------------
# Video feed (Tello streams raw H.264 over UDP — we decode it with OpenCV
# and re-serve it to the browser as an MJPEG stream, which an <img> tag
# can display directly with no client-side decoding needed)
# ---------------------------------------------------------------------------

_video_lock = threading.Lock()
_video_frame = {"jpeg": None}
_video_running = threading.Event()
_video_thread = None


def _video_loop():
    cap = cv2.VideoCapture(f"udp://0.0.0.0:{VIDEO_PORT}")
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass

    misses = 0
    while _video_running.is_set():
        ret, frame = cap.read()
        if not ret:
            misses += 1
            if misses > 200:  # capture went stale — try reopening
                cap.release()
                cap = cv2.VideoCapture(f"udp://0.0.0.0:{VIDEO_PORT}")
                misses = 0
            time.sleep(0.02)
            continue
        misses = 0
        encoded, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if encoded:
            with _video_lock:
                _video_frame["jpeg"] = buf.tobytes()

    cap.release()
    with _video_lock:
        _video_frame["jpeg"] = None


def _mjpeg_generator():
    boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
    while _video_running.is_set():
        with _video_lock:
            frame = _video_frame["jpeg"]
        if frame is None:
            time.sleep(0.05)
            continue
        yield boundary + frame + b"\r\n"
        time.sleep(0.03)  # ~30fps cap


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/connect", methods=["POST"])
def api_connect():
    """Must be called once before any other command — puts the Tello into
    'SDK mode' so it will accept further text commands."""
    reply = send_command("command")
    return jsonify(ok=ok(reply), reply=reply)


@app.route("/api/takeoff", methods=["POST"])
def api_takeoff():
    reply = send_command("takeoff", timeout=10)
    return jsonify(ok=ok(reply), reply=reply)


@app.route("/api/land", methods=["POST"])
def api_land():
    reply = send_command("land", timeout=10)
    return jsonify(ok=ok(reply), reply=reply)


@app.route("/api/emergency", methods=["POST"])
def api_emergency():
    """Immediately cuts all motor power. The drone will drop — only for
    genuine emergencies (e.g. about to hit something)."""
    reply = send_command("emergency")
    return jsonify(ok=True, reply=reply)


@app.route("/api/move", methods=["POST"])
def api_move():
    body = request.get_json(force=True) or {}
    direction = body.get("direction")
    distance = int(body.get("distance", 30))
    distance = max(20, min(500, distance))  # Tello SDK range: 20-500 cm

    if direction not in {"up", "down", "left", "right", "forward", "back"}:
        return jsonify(ok=False, reply="invalid direction"), 400

    reply = send_command(f"{direction} {distance}")
    return jsonify(ok=ok(reply), reply=reply)


@app.route("/api/rotate", methods=["POST"])
def api_rotate():
    body = request.get_json(force=True) or {}
    direction = body.get("direction")  # "cw" or "ccw"
    degrees = int(body.get("degrees", 45))
    degrees = max(1, min(360, degrees))

    if direction not in {"cw", "ccw"}:
        return jsonify(ok=False, reply="invalid direction"), 400

    reply = send_command(f"{direction} {degrees}")
    return jsonify(ok=ok(reply), reply=reply)


@app.route("/api/rc", methods=["POST"])
def api_rc():
    """Continuous analog control for the on-screen joysticks. Maps to the
    Tello 'rc a b c d' command where each channel is -100..100:
        a = left/right (roll), b = forward/back (pitch),
        c = up/down (throttle), d = yaw (rotate).
    Sent at high frequency, so it returns immediately without waiting."""
    body = request.get_json(force=True) or {}

    def clamp(v):
        try:
            return max(-100, min(100, int(v)))
        except (TypeError, ValueError):
            return 0

    a = clamp(body.get("lr", 0))
    b = clamp(body.get("fb", 0))
    c = clamp(body.get("ud", 0))
    d = clamp(body.get("yaw", 0))
    send_command_noreply(f"rc {a} {b} {c} {d}")
    return jsonify(ok=True)


@app.route("/api/flip", methods=["POST"])
def api_flip():
    body = request.get_json(force=True) or {}
    direction = body.get("direction")  # "f", "b", "l", "r"
    if direction not in {"f", "b", "l", "r"}:
        return jsonify(ok=False, reply="invalid direction"), 400
    reply = send_command(f"flip {direction}")
    return jsonify(ok=ok(reply), reply=reply)


@app.route("/api/battery", methods=["GET"])
def api_battery():
    reply = send_command("battery?")
    return jsonify(reply=reply)


@app.route("/api/state", methods=["GET"])
def api_state():
    """Return the latest broadcast telemetry, plus labels/units so the UI
    can render any field without hard-coding them."""
    with _state_lock:
        data = dict(_state["data"])
        ts = _state["ts"]
    # 'fresh' tells the UI whether we've heard from the drone recently.
    fresh = bool(data) and (time.time() - ts) < 3.0
    return jsonify(data=data, fields=STATE_FIELDS, fresh=fresh)


@app.route("/api/snapshot", methods=["POST"])
def api_snapshot():
    """Grab the most recent decoded video frame, save it to disk as a photo,
    and hand the filename back so the browser can offer it for download."""
    with _video_lock:
        frame = _video_frame["jpeg"]
    if frame is None:
        return jsonify(ok=False, reply="no video frame available — start the video stream first"), 409

    filename = "tello_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3] + ".jpg"
    path = os.path.join(SNAPSHOT_DIR, filename)
    with open(path, "wb") as fh:
        fh.write(frame)
    return jsonify(ok=True, reply="captured", filename=filename)


@app.route("/snapshots/<path:filename>")
def snapshots(filename):
    """Serve a saved photo (used for the gallery + downloads)."""
    return send_from_directory(SNAPSHOT_DIR, filename)


@app.route("/api/snapshots", methods=["GET"])
def api_snapshots():
    """List saved photos, newest first."""
    files = [f for f in os.listdir(SNAPSHOT_DIR) if f.lower().endswith(".jpg")]
    files.sort(reverse=True)
    return jsonify(files=files)


@app.route("/api/streamon", methods=["POST"])
def api_streamon():
    """Tells the Tello to start sending video, then starts the background
    thread that decodes it."""
    global _video_thread
    reply = send_command("streamon")
    if ok(reply):
        _video_running.set()
        if _video_thread is None or not _video_thread.is_alive():
            _video_thread = threading.Thread(target=_video_loop, daemon=True)
            _video_thread.start()
    return jsonify(ok=ok(reply), reply=reply)


@app.route("/api/streamoff", methods=["POST"])
def api_streamoff():
    reply = send_command("streamoff")
    _video_running.clear()
    return jsonify(ok=ok(reply), reply=reply)


@app.route("/video_feed")
def video_feed():
    if not _video_running.is_set():
        return "Video stream is not active. Click 'Start video' first.", 409
    return Response(
        _mjpeg_generator(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


if __name__ == "__main__":
    # macOS uses port 5000 for AirPlay Receiver (the "ControlCenter" process),
    # so we default to 5001. Override with: PORT=5002 python server.py
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
