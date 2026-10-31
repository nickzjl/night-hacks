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
from .serial_link import SerialLink
from .vision import Recognizer

log = logging.getLogger("nighthacks")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

STATIC = Path(__file__).with_name("static")
RECOGNIZE_EVERY_S = float(os.getenv("RECOGNIZE_EVERY_S", "2.5"))
REPEAT_SUPPRESS_S = float(os.getenv("REPEAT_SUPPRESS_S", "10"))
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
            if not forced and (not self.auto or self.pacer.busy or now - self.last_request_at < RECOGNIZE_EVERY_S):
                continue
            self.force_recognize = False
            self.recognizing = True
            self.last_request_at = now
            await self.broadcast_ui(self.state())
            try:
                result = await asyncio.to_thread(self.recognizer.recognize, self.frame)
                self.recognition = {"word": result.word, "kind": result.kind, "latency_ms": result.latency_ms, "model": result.model, "error": ""}
                await self.send_phones({"type": "status", "text": f"Saw: {result.word or 'nothing'} ({result.latency_ms} ms)"})
                if not result.word:
                    continue
                repeat = result.word == self.pacer.last_word and time.monotonic() - self.pacer.last_word_at < REPEAT_SUPPRESS_S
                if repeat and not forced:
                    self.event(f"saw {result.word} again, not repeating")
                    continue
                await self.say(result.word, f"{result.model}, {result.latency_ms} ms")
            except Exception as exc:
                self.recognition = {**self.recognition, "error": str(exc)[:300]}
                self.event(f"recognition failed: {exc}")
                self.last_request_at = time.monotonic() + 3  # back off a little
            finally:
                self.recognizing = False
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
    hub.link.start()
    if hub.recognizer.mock:
        hub.event(f"recognizer in MOCK mode: {hub.recognizer.mock_reason}")
    else:
        hub.event("recognizer live (Claude)")
    hub.event(f"serial: {hub.link.setting}")
    hub.event("bluetooth: scanning for the phone" if hub.ble.enabled else "bluetooth: off")
    tasks = [
        asyncio.create_task(hub.recognition_loop()),
        asyncio.create_task(hub.status_loop()),
        asyncio.create_task(hub.ble.run()),
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
    return {"word": await hub.say(payload.get("word", ""), "api")}


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
                data = json.loads(msg["text"])
                if data.get("type") == "telemetry":
                    hub.telemetry, hub.telemetry_at = data, time.monotonic()
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
