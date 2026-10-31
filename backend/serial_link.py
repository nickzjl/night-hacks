"""USB serial link to the Feather that drives the six Braille servos.

One line per cell state. With Kiya's Arduino sketch (default) the letter D (dots 1-4-5) is
"1S1,2S0,3S0,4S1,5S1,6S0\\n" and the board answers "OK 1S1", "OK 2S0", ... per token.
SERIAL_FORMAT=hex switches to firmware/code.py's two-hex-digit lines ("19\\n").

Adapted from Bridge's serial_out.py, with these changes: the port stays open, because
CircuitPython boards don't reset when it opens, so Bridge's 2 s wait isn't needed. It also
reconnects on its own, and writes never block the asyncio event loop.

SERIAL_PORT:
  auto (default)  first Adafruit USB serial port, found by vendor ID; else .fake-feather if running
  off             no hardware; the dashboard still shows the simulated cell
  <path or URL>   explicit port, e.g. /dev/cu.usbmodem1101, a fake-feather pty, or loop://
"""
from __future__ import annotations

import collections
import os
import threading
import time
from pathlib import Path

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - handled at runtime
    serial = None
    list_ports = None

ADAFRUIT_VID = 0x239A
BAUD = 115200  # ignored by USB CDC, but pyserial wants a value
FAKE_FEATHER = Path(__file__).resolve().parent.parent / ".fake-feather"

# Wire format, set by SERIAL_FORMAT:
#   servo (default)  Kiya's Arduino sketch (Hackaton1/src/main.cpp): "1S1,2S0,3S0,4S1,5S1,6S0"
#                    = servo n up (1) or flat (0); servo n is dot n. "TS" runs its self-test.
#   hex              firmware/code.py (CircuitPython): two hex digits, e.g. "19"
SERIAL_FORMAT = os.getenv("SERIAL_FORMAT", "servo").strip().lower()
SELF_TEST = {"servo": "TS", "hex": "T"}


def format_mask(mask: int, fmt: str | None = None) -> str:
    mask &= 0x3F
    if (fmt or SERIAL_FORMAT) == "hex":
        return f"{mask:02X}"
    return ",".join(f"{dot}S{(mask >> (dot - 1)) & 1}" for dot in range(1, 7))


def find_feather_port() -> str | None:
    if list_ports is None:
        return None
    ports = [p for p in list_ports.comports() if p.vid == ADAFRUIT_VID]
    if not ports:
        # No board plugged in: use tools/fake_feather.py if it's running.
        return str(FAKE_FEATHER) if FAKE_FEATHER.exists() else None
    # A plain CircuitPython build exposes one CDC port (the console). If boot.py also enables
    # usb_cdc.data there are two; the firmware listens on both, so either one works.
    named_data = [p for p in ports if "data" in (p.interface or "").lower()]
    if named_data:
        return named_data[0].device
    return sorted(p.device for p in ports)[0]


class SerialLink:
    def __init__(self, setting: str | None = None) -> None:
        self.setting = (setting or os.getenv("SERIAL_PORT", "auto")).strip() or "auto"
        self.port: str | None = None
        self.connected = False
        self.last_error = ""
        self.last_sent: str | None = None
        self.last_ack: str | None = None
        self.last_ack_at = 0.0
        self.lines: collections.deque[str] = collections.deque(maxlen=30)
        self._ser = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self.setting.lower() != "off" and serial is not None

    def start(self) -> None:
        if not self.enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="serial-link", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()

    def write_mask(self, mask: int) -> bool:
        return self.write_line(format_mask(mask))

    def write_line(self, line: str) -> bool:
        self.last_sent = line
        with self._lock:
            ser = self._ser
            if ser is None:
                return False
            try:
                ser.write((line + "\n").encode("ascii"))
                return True
            except Exception as exc:  # port vanished (unplugged, board reset)
                self.last_error = f"write failed: {exc}"
        self._close()
        return False

    def self_test(self) -> bool:
        return self.write_line(SELF_TEST.get(SERIAL_FORMAT, "TS"))

    def status(self) -> dict:
        return {
            "setting": self.setting,
            "format": SERIAL_FORMAT,
            "enabled": self.enabled,
            "port": self.port,
            "connected": self.connected,
            "last_sent": self.last_sent,
            "last_ack": self.last_ack,
            "ack_age_s": round(time.monotonic() - self.last_ack_at, 1) if self.last_ack_at else None,
            "last_error": self.last_error,
            "lines": list(self.lines)[-8:],
        }

    def _resolve_port(self) -> str | None:
        if self.setting.lower() == "auto":
            return find_feather_port()
        return self.setting

    def _open(self) -> bool:
        port = self._resolve_port()
        if not port:
            self.last_error = "no Adafruit board found (plug in the Feather)"
            return False
        try:
            ser = serial.serial_for_url(port, baudrate=BAUD, timeout=0.2, write_timeout=0.5)
        except Exception as exc:
            self.last_error = f"open {port} failed: {exc}"
            return False
        with self._lock:
            self._ser = ser
        self.port = port
        self.connected = True
        self.last_error = ""
        self.write_mask(0)  # start from a blank cell
        return True

    def _close(self) -> None:
        with self._lock:
            ser, self._ser = self._ser, None
        self.connected = False
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def _real_board_appeared(self) -> bool:
        """On the fake Feather in auto mode, has the real board just been plugged in?"""
        if self.setting.lower() != "auto" or self.port != str(FAKE_FEATHER):
            return False
        port = find_feather_port()
        return port is not None and port != str(FAKE_FEATHER)

    def _run(self) -> None:
        last_probe = 0.0
        while not self._stop.is_set():
            if self._ser is None:
                if not self._open():
                    self._stop.wait(1.0)
                    continue
            if time.monotonic() - last_probe > 2:
                last_probe = time.monotonic()
                if self._real_board_appeared():
                    self.lines.append("real Feather detected, switching from the fake one")
                    self._close()
                    continue
            try:
                raw = self._ser.readline() if self._ser else b""
            except Exception as exc:
                self.last_error = f"read failed: {exc}"
                self._close()
                continue
            if not raw:
                continue
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            self.lines.append(text)
            if text.startswith("OK "):
                self.last_ack = text[3:].strip()
                self.last_ack_at = time.monotonic()
