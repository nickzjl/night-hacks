"""Bluetooth LE link to the iPhone app, for venues whose Wi-Fi blocks phone-to-Mac traffic.

The phone advertises the Night Hacks service and the Mac connects as the central, so the
Mac keeps its own Wi-Fi (and internet for Claude). Packets from the phone (notifications):
  b"J" + JSON                                   telemetry / hello
  b"S" + frame id + uint32 length (BE) + bytes  first chunk of a JPEG frame
  b"C" + frame id + bytes                       following chunks
Messages to the phone are JSON written to the downlink characteristic.

BLE=off in backend/.env disables it.
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import time
from typing import Awaitable, Callable

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover - handled at runtime
    BleakClient = BleakScanner = None

SERVICE = "7a1e0001-4e48-4b9c-9a3d-1c5d2e8f0a01"
UPLINK = "7a1e0002-4e48-4b9c-9a3d-1c5d2e8f0a01"
DOWNLINK = "7a1e0003-4e48-4b9c-9a3d-1c5d2e8f0a01"
LOCAL_NAME = "NightHacks"


class BLELink:
    def __init__(
        self,
        on_frame: Callable[[bytes], Awaitable[None]],
        on_json: Callable[[dict], None],
        on_event: Callable[[str], None],
    ) -> None:
        self.on_frame = on_frame
        self.on_json = on_json
        self.on_event = on_event
        self.connected = False
        self.device_name = ""
        self.last_error = ""
        self.frames = 0
        self.last_frame_ms = 0
        self._client: BleakClient | None = None
        self._buf = bytearray()
        self._expect = 0
        self._frame_id: int | None = None
        self._frame_started = 0.0

    @property
    def enabled(self) -> bool:
        return BleakClient is not None and os.getenv("BLE", "on").lower() not in ("0", "off", "false")

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "connected": self.connected,
            "device": self.device_name,
            "frames": self.frames,
            "last_frame_ms": self.last_frame_ms,
            "last_error": self.last_error,
        }

    async def run(self) -> None:
        if not self.enabled:
            return
        while True:
            try:
                device = await BleakScanner.find_device_by_filter(self._matches, timeout=8)
                if device is None:
                    continue
                disconnected = asyncio.Event()
                async with BleakClient(device, disconnected_callback=lambda _: disconnected.set(), timeout=15) as client:
                    self._client = client
                    self.device_name = device.name or device.address
                    await client.start_notify(UPLINK, self._on_packet)
                    self.connected = True
                    self.last_error = ""
                    self.on_event(f"phone connected over Bluetooth ({self.device_name}, MTU {client.mtu_size})")
                    await disconnected.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)[:200]
                await asyncio.sleep(2)
            finally:
                self._client = None
                if self.connected:
                    self.connected = False
                    self.on_event("phone Bluetooth disconnected")

    async def send(self, message: dict) -> None:
        client = self._client
        if not self.connected or client is None:
            return
        try:
            await client.write_gatt_char(DOWNLINK, json.dumps(message, separators=(",", ":")).encode(), response=False)
        except Exception as exc:
            self.last_error = f"write failed: {exc}"[:200]

    @staticmethod
    def _matches(device, adv) -> bool:
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        return SERVICE in uuids or (adv.local_name or device.name or "") == LOCAL_NAME

    def _on_packet(self, _characteristic, data: bytearray) -> None:
        kind = data[:1]
        if kind == b"J":
            try:
                self.on_json(json.loads(bytes(data[1:])))
            except ValueError:
                pass
        elif kind == b"S" and len(data) >= 6:
            self._frame_id = data[1]
            self._expect = struct.unpack(">I", bytes(data[2:6]))[0]
            self._buf = bytearray(data[6:])
            self._frame_started = time.monotonic()
            self._maybe_finish()
        elif kind == b"C" and len(data) >= 2 and data[1] == self._frame_id and self._expect:
            self._buf += data[2:]
            self._maybe_finish()

    def _maybe_finish(self) -> None:
        if not self._expect or len(self._buf) < self._expect:
            return
        jpeg = bytes(self._buf[: self._expect])
        self._expect = 0
        self.frames += 1
        self.last_frame_ms = int((time.monotonic() - self._frame_started) * 1000)
        asyncio.get_running_loop().create_task(self.on_frame(jpeg))
