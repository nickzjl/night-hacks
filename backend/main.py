"""Night Hacks backend: iPhone frames -> Claude -> Braille cell over USB serial.

Run from the repo root:
  .venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8765

WebSockets
  /ws/phone  phone -> Mac: binary JPEG frames, JSON telemetry; Mac -> phone: braille/idle/status
  /ws/ui     dashboard: JSON state + binary frames out, JSON commands in
HTTP
  GET  /            dashboard
  GET  /health      liveness + component status
  POST /api/say     {"word": "DOOR"}  spell a word now
  POST /api/recognize                 run recognition on the latest frame now
  POST /api/frame   raw JPEG body     inject a frame (testing without the phone)
  POST /api/clear                     stop and drop all dots
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .ble_link import BLELink
from .display import BraillePacer
from .lexis import Lexis
from .lexis_ai import BRAILLE_MAX
from .serial_link import SerialLink
from .vision import Recognizer

log = logging.getLogger("nighthacks")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

STATIC = Path(__file__).with_name("static")
RECOGNIZE_EVERY_S = float(os.getenv("RECOGNIZE_EVERY_S", "2.5"))
REPEAT_SUPPRESS_S = float(os.getenv("REPEAT_SUPPRESS_S", "10"))
LOOKAHEAD_S = float(os.getenv("LOOKAHEAD_S", "4.5"))  # start recognizing this long before a word ends
NAV_IDLE_S = float(os.getenv("NAV_IDLE_S", "6"))       # manual mode: no new camera word while reading
MESSAGE_HOLD_S = float(os.getenv("MESSAGE_HOLD_S", "120"))  # an unread message keeps the cell this long
JOY_BOUNCE_S = float(os.getenv("JOY_BOUNCE_S", "0.35"))  # an opposite move this soon is spring-back


def joystick_map(spec: str | None = None) -> dict[str, str]:
    """Board direction -> action. JOY_MAP="L=prev,R=next,P=start" by default; for a stick
    mounted a quarter turn off, e.g. "U=next,D=prev,P=start". Actions: prev, next, start."""
    mapping = {}
    for part in (spec or os.getenv("JOY_MAP", "L=prev,R=next,P=start")).split(","):
        key, _, action = part.partition("=")
        if key.strip() and action.strip().lower() in ("prev", "next", "start"):
            mapping[key.strip().upper()] = action.strip().lower()
    return mapping
FRAME_FRESH_S = 4.0  # Bluetooth frames arrive about once a second
UI_FRAME_EVERY_S = 0.2
PHONE_SILENT_S = 5.0  # no frame or telemetry for this long = dead link (e.g. cable pulled, Wi-Fi drop)


class Hub:
    def __init__(self) -> None:
        self.phones: set[WebSocket] = set()
        self.uis: set[WebSocket] = set()
        self.frame: bytes | None = None
        self.frame_at = 0.0
        self.frame_count = 0
        self.telemetry: dict = {}
        self.telemetry_at = 0.0
        self.auto = os.getenv("AUTO_RECOGNIZE", "true").lower() in ("1", "true", "yes")
        self.recognizing = False
        self.last_request_at = 0.0
        self.force_recognize = False
        self.recognition: dict = {}
        self.events: list[dict] = []
        self.link = SerialLink()
        self.recognizer = Recognizer()
        self.pacer = BraillePacer(self.link, self.on_braille)
        self.ble = BLELink(self.on_frame, self.on_phone_json, self.event)
        self.recognition_paused_until = 0.0  # loved-ones' messages own the cell until then
        self.joy_map = joystick_map()
        self._last_joy = ("", 0.0)
        self.scan_requested = False
        self.lexis = Lexis(self)
        self._last_ui_frame = 0.0

    # ---- outbound -------------------------------------------------------------------------
    def state(self) -> dict:
        now = time.monotonic()
        return {
            "type": "state",
            "phone": {
                "connected": bool(self.phones) or self.ble.connected,
                "via": "wifi" if self.phones else ("bluetooth" if self.ble.connected else None),
                "ble": self.ble.status(),
                "frame_age_s": round(now - self.frame_at, 1) if self.frame_at else None,
                "frames": self.frame_count,
                "telemetry": self.telemetry,
                "telemetry_age_s": round(now - self.telemetry_at, 1) if self.telemetry_at else None,
            },
            "braille": self.pacer.state(),
            "serial": self.link.status(),
            "recognizer": {
                "mock": self.recognizer.mock,
                "mock_reason": self.recognizer.mock_reason,
                "auto": self.auto,
                "busy": self.recognizing,
                "every_s": RECOGNIZE_EVERY_S,
                "last": self.recognition,
            },
            "events": self.events[-12:],
            "lexis": self.lexis.state(),
        }

    def event(self, text: str) -> None:
        log.info(text)
        self.events.append({"t": time.strftime("%H:%M:%S"), "text": text})
        del self.events[:-50]

    async def broadcast_ui(self, message: dict | bytes) -> None:
        dead = []
        for ws in list(self.uis):
            try:
                if isinstance(message, bytes):
                    await ws.send_bytes(message)
                else:
                    await ws.send_text(json.dumps(message))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.uis.discard(ws)

    def on_phone_json(self, data: dict) -> None:
        if data.get("type") == "telemetry":
            self.telemetry, self.telemetry_at = data, time.monotonic()
        elif data.get("type") in ("companion", "location"):
            asyncio.get_running_loop().create_task(self.lexis.on_phone(data))

    async def on_board_line(self, line: str) -> None:
        """Lines from the Feather: joystick moves (JOY L / R / P; U and D unused),
        joystick readouts (JOYV ...), boot info and errors. All go to the event log."""
        self.event(f"board: {line}")
        if not line.startswith("JOY "):
            return
        action = self.joy_map.get(line.split()[-1])
        if not action:
            return
        now = time.monotonic()
        last, last_at = self._last_joy
        if {action, last} == {"prev", "next"} and now - last_at < JOY_BOUNCE_S:
            self.event(f"joystick: ignored {action} (spring-back)")
            return
        self._last_joy = (action, now)
        if not self.pacer.cells:
            # Nothing to read yet: any move asks the camera what's ahead right now.
            self.force_recognize = self.scan_requested = True
            await self.pacer.show_status(["LEXIS READY", "SCANNING..."])
            return
        if action == "prev":
            await self.pacer.step(-1)
        elif action == "next":
            await self.pacer.step(1)
        elif action == "start":
            await self.pacer.restart()

    async def braille_message(self, text: str, sender: str) -> float:
        """Put a loved one's message on the cell. Manual mode: it loads at its first character
        and camera words can't replace it until it's read to the end (or MESSAGE_HOLD_S).
        Auto mode: it cuts in on a camera word, waits behind another message, and pauses
        camera recognition until it's done. Returns seconds until finished (0 in manual mode)."""
        if self.pacer.mode == "manual":
            await self.pacer.say(text, max_len=BRAILLE_MAX, priority=1)
            self.event(f"braille ← {text} (message from {sender})")
            return 0.0
        wait = self.pacer.time_left() if (self.pacer.busy and self.pacer.priority >= 1) else 0.0
        if wait:
            await self.pacer.queue(text, max_len=BRAILLE_MAX, priority=1)
        else:
            await self.pacer.say(text, max_len=BRAILLE_MAX, priority=1)
        seconds = wait + self.pacer.estimate(text, BRAILLE_MAX)
        self.recognition_paused_until = time.monotonic() + seconds
        self.event(f"braille ← {text} (message from {sender})")
        return seconds

    async def send_phones(self, message: dict) -> None:
        if message.get("type") != "ping":  # Bluetooth notices dead links by itself
            await self.ble.send(message)
        for ws in list(self.phones):
            try:
                await ws.send_text(json.dumps(message))
            except Exception:
                self.phones.discard(ws)

    async def on_braille(self, braille: dict) -> None:
        if braille["index"] >= 0 and braille["mask"]:
            await self.send_phones({"type": "braille", "word": braille["word"], "index": braille["index"], "mask": braille["mask"]})
        elif not braille["busy"]:
            await self.send_phones({"type": "idle"})
        await self.broadcast_ui(self.state())

    # ---- inbound --------------------------------------------------------------------------
    async def on_frame(self, jpeg: bytes) -> None:
        self.frame, self.frame_at = jpeg, time.monotonic()
        self.frame_count += 1
        if self.frame_at - self._last_ui_frame >= UI_FRAME_EVERY_S and self.uis:
            self._last_ui_frame = self.frame_at
            await self.broadcast_ui(jpeg)

    async def say(self, word: str, source: str) -> str:
        spoken = await self.pacer.say(word)
        if spoken:
            self.event(f"braille ← {spoken} ({source})")
        return spoken

    async def command(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "say":
            await self.say(msg.get("word", ""), "manual")
        elif kind == "recognize":
            self.force_recognize = True
        elif kind == "auto":
            self.auto = bool(msg.get("enabled"))
            self.event(f"auto recognition {'on' if self.auto else 'off'}")
        elif kind == "clear":
            await self.pacer.clear()
            self.event("cleared")
        elif kind == "nav":
            direction = int(msg.get("dir", 0))
            await (self.pacer.restart() if direction == 0 else self.pacer.step(direction))
        elif kind == "mode":
            await self.pacer.set_mode(str(msg.get("mode", "")))
            self.event(f"Braille mode: {self.pacer.mode}")
        elif kind == "stop_sharing":
            await self.lexis.stop_sharing(source="dashboard")
        elif kind == "selftest":
            await self.pacer.clear()
            await asyncio.to_thread(self.link.self_test)
            self.event("cell self-test sent")
        await self.broadcast_ui(self.state())

    # ---- recognition loop -----------------------------------------------------------------
    async def recognition_loop(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            now = time.monotonic()
            fresh = self.frame is not None and now - self.frame_at <= FRAME_FRESH_S
            if self.recognizing or not fresh:
                continue  # a forced request waits for the next fresh frame
            forced = self.force_recognize
            # While a word is being spelled, look ahead only near its end, so the next word is
            # ready when the word gap finishes instead of waiting on Claude afterwards.
            if self.pacer.mode == "manual":
                # Don't pull text away while someone is reading it with the joystick.
                cell_ready = (not self.pacer.holding_message(MESSAGE_HOLD_S)
                              and now - self.pacer.last_nav_at >= NAV_IDLE_S)
            else:
                cell_ready = not self.pacer.busy or (self.pacer.time_left() <= LOOKAHEAD_S and not self.pacer.next_word)
            message_playing = now < self.recognition_paused_until
            if not forced and (not self.auto or not cell_ready or message_playing or now - self.last_request_at < RECOGNIZE_EVERY_S):
                continue
            self.force_recognize = False
            scan = self.scan_requested
            self.recognizing = True
            self.last_request_at = now
            await self.broadcast_ui(self.state())
            try:
                result = await asyncio.to_thread(self.recognizer.recognize, self.frame)
                self.recognition = {"word": result.word, "kind": result.kind, "scene": result.scene, "latency_ms": result.latency_ms, "model": result.model, "error": ""}
                self.lexis.add_observation(result.word, result.kind, result.scene)
                await self.send_phones({"type": "status", "text": f"Saw: {result.word or 'nothing'} ({result.latency_ms} ms)"})
                if not result.word:
                    if self.scan_requested:
                        await self.pacer.show_status(["NOTHING SEEN", "PRESS TO SCAN"])
                    continue
                source = f"{result.model}, {result.latency_ms} ms"
                if forced:
                    await self.say(result.word, source)
                    continue
                spelling_it = (self.pacer.busy or self.pacer.mode == "manual") and result.word == self.pacer.word
                said_recently = result.word == self.pacer.last_word and time.monotonic() - self.pacer.last_word_at < REPEAT_SUPPRESS_S
                if spelling_it or said_recently:
                    self.event(f"saw {result.word} again, not repeating")
                    continue
                if time.monotonic() < self.recognition_paused_until:
                    continue  # a loved one's message arrived while Claude was looking
                if self.pacer.busy:
                    await self.pacer.queue(result.word)
                    self.event(f"next up: {result.word} ({source})")
                else:
                    await self.say(result.word, source)
            except Exception as exc:
                self.recognition = {**self.recognition, "error": str(exc)[:300]}
                self.event(f"recognition failed: {exc}")
                self.last_request_at = time.monotonic() + 3  # back off a little
            finally:
                self.recognizing = False
                if scan:
                    self.scan_requested = False
                await self.broadcast_ui(self.state())

    async def status_loop(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(0.5)
            tick += 1
            if tick % 2 == 0:
                # Heartbeat: lets the phone notice a dead link even while the cell is idle.
                await self.send_phones({"type": "ping"})
            if self.uis:
                await self.broadcast_ui(self.state())


hub = Hub()


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    # The serial thread hands joystick events and (re)connects to the event loop.
    hub.link.on_line = lambda line: loop.call_soon_threadsafe(lambda: loop.create_task(hub.on_board_line(line)))
    hub.link.on_connect = lambda: loop.call_soon_threadsafe(lambda: loop.create_task(hub.pacer.refresh()))
    hub.link.start()
    hub.event(f"Braille mode: {hub.pacer.mode}")
    if hub.recognizer.mock:
        hub.event(f"recognizer in MOCK mode: {hub.recognizer.mock_reason}")
    else:
        hub.event("recognizer live (Claude)")
    hub.event(f"serial: {hub.link.setting}")
    hub.event("bluetooth: scanning for the phone" if hub.ble.enabled else "bluetooth: off")
    photon = hub.lexis.photon.status()
    hub.event("iMessage: Photon " + ("configured" if photon["enabled"] else f"offline ({photon['last_error']})"))
    hub.event(f"lexis: {len(hub.lexis.contacts)} approved contact(s), wearer {hub.lexis.wearer}")
    tasks = [
        asyncio.create_task(hub.recognition_loop()),
        asyncio.create_task(hub.status_loop()),
        asyncio.create_task(hub.ble.run()),
        asyncio.create_task(hub.lexis.photon.run()),
        asyncio.create_task(hub.lexis.tick_loop()),
    ]
    yield
    for task in tasks:
        task.cancel()
    hub.link.stop()


app = FastAPI(title="Night Hacks", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def dashboard():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    state = hub.state()
    return {"status": "ok", **{k: state[k] for k in ("phone", "serial", "recognizer", "braille")}}


@app.get("/api/state")
def api_state():
    return hub.state()


@app.post("/api/say")
async def api_say(payload: dict = Body(...)):
    """{"word": "DOOR"}, optionally "hold_ms" / "gap_ms" for this word only."""
    word = await hub.pacer.say(payload.get("word", ""), payload.get("hold_ms"), payload.get("gap_ms"))
    if word:
        hub.event(f"braille ← {word} (api)")
    return {"word": word, "hold_ms": payload.get("hold_ms") or int(hub.pacer.hold * 1000)}


@app.post("/api/timing")
async def api_timing(payload: dict = Body(...)):
    """Change letter timing for every word from now on: hold_ms, gap_ms, word_gap_ms."""
    hub.pacer.set_timing(payload.get("hold_ms"), payload.get("gap_ms"), payload.get("word_gap_ms"))
    return hub.pacer.state()["timing_ms"]


@app.post("/api/recognize")
async def api_recognize():
    hub.force_recognize = True
    return {"queued": True, "has_frame": hub.frame is not None}


@app.post("/api/frame")
async def api_frame(request: Request):
    await hub.on_frame(await request.body())
    return {"frames": hub.frame_count}


@app.post("/api/clear")
async def api_clear():
    await hub.pacer.clear()
    return {"cleared": True}


@app.get("/api/lexis")
def api_lexis():
    return hub.lexis.state()


@app.post("/api/lexis/stop-sharing")
async def api_stop_sharing():
    """Immediate stop from the Mac (the wearer's phone switch does the same)."""
    await hub.lexis.stop_sharing(source="dashboard")
    return {"sharing": hub.lexis.sharing}


@app.post("/api/lexis/reload-contacts")
def api_reload_contacts():
    hub.lexis.load_contacts()
    return {"contacts": len(hub.lexis.contacts), "wearer": hub.lexis.wearer}


@app.post("/api/lexis/simulate")
async def api_simulate(payload: dict = Body(...)):
    """Testing without Photon: {"from": "+14155550123", "text": "where is he?"}. Replies are returned, not sent."""
    photon = hub.lexis.photon
    inbound = photon.simulate(payload.get("from", ""), payload.get("text", ""))
    before = len(photon.sent)
    await hub.lexis.on_imessage(inbound)
    return {"replies": [m["text"] for m in list(photon.sent)[before:] if m["space_id"] == inbound.space_id]}


@app.post("/api/lexis/phone")
async def api_phone(payload: dict = Body(...)):
    """Testing without the iPhone: inject a "companion" or "location" message."""
    await hub.lexis.on_phone(payload)
    return hub.lexis.state()


@app.post("/api/nav")
async def api_nav(payload: dict = Body(...)):
    """Virtual joystick: {"dir": -1} left, {"dir": 1} right, {"dir": 0} back to the first character."""
    await hub.command({"type": "nav", "dir": payload.get("dir", 0)})
    return hub.pacer.state()


@app.post("/api/mode")
async def api_mode(payload: dict = Body(...)):
    """{"mode": "manual"} (joystick) or {"mode": "auto"} (timed playback)."""
    await hub.command({"type": "mode", "mode": payload.get("mode", "")})
    return hub.pacer.state()


@app.post("/api/joystick-map")
async def api_joystick_map(payload: dict = Body(...)):
    """Re-map joystick directions at runtime: {"map": "U=next,D=prev,P=start"}."""
    mapping = joystick_map(str(payload.get("map", "")))
    if not mapping:
        return {"error": "use e.g. L=prev,R=next,P=start"}
    hub.joy_map = mapping
    hub.event(f"joystick map: {mapping}")
    return mapping


BOARD_COMMANDS = {"JV", "JC", "TS", "HN", "LA"}


@app.post("/api/board")
async def api_board(payload: dict = Body(...)):
    """Send a diagnostic command to the Feather: JV (joystick readout), JC (recalibrate
    joystick centre), TS (servo self-test), HN (Mac mode), LA (LCD back to the board)."""
    cmd = str(payload.get("cmd", "")).upper()
    if cmd not in BOARD_COMMANDS:
        return {"error": f"allowed: {sorted(BOARD_COMMANDS)}"}
    return {"sent": await asyncio.to_thread(hub.link.write_line, cmd)}


@app.post("/api/selftest")
async def api_selftest():
    await hub.command({"type": "selftest"})
    return {"sent": hub.link.last_sent}


@app.websocket("/ws/phone")
async def ws_phone(ws: WebSocket):
    await ws.accept()
    hub.phones.add(ws)
    hub.event(f"phone connected from {ws.client.host if ws.client else '?'}")
    await ws.send_text(json.dumps({"type": "status", "text": "Connected to Mac"}))
    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=PHONE_SILENT_S)
            except asyncio.TimeoutError:
                hub.event("phone went silent, dropping link")
                await ws.close()
                break
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes"):
                await hub.on_frame(msg["bytes"])
            elif msg.get("text"):
                hub.on_phone_json(json.loads(msg["text"]))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.phones.discard(ws)
        hub.event("phone disconnected")


@app.websocket("/ws/ui")
async def ws_ui(ws: WebSocket):
    await ws.accept()
    hub.uis.add(ws)
    await ws.send_text(json.dumps(hub.state()))
    if hub.frame:
        await ws.send_bytes(hub.frame)
    try:
        while True:
            await hub.command(json.loads(await ws.receive_text()))
    except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
        pass
    finally:
        hub.uis.discard(ws)
