"""Stand-in for the Feather: a pseudo-terminal that speaks the firmware's protocol.

  .venv/bin/python tools/fake_feather.py            # prints the port path, links .fake-feather
  SERIAL_PORT=.fake-feather .venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8765

Each "XX\\n" line is drawn as a 2x3 cell in this terminal and answered with "OK XX",
just like firmware/code.py.
"""
from __future__ import annotations

import os
import sys
import threading
import tty
from pathlib import Path

LINK = Path(__file__).resolve().parent.parent / ".fake-feather"


def render(mask: int) -> str:
    dot = lambda bit: "●" if mask & (1 << bit) else "○"
    return f"{dot(0)} {dot(3)}   {chr(0x2800 + mask)}  {mask:02X}\n{dot(1)} {dot(4)}\n{dot(2)} {dot(5)}"


def respond(line: str) -> tuple[str, int | None]:
    """Same parsing as firmware/code.py's handle()."""
    line = line.strip().upper()
    if line == "T":
        return "OK T", None
    if line == "?":
        return "STATE 00", None
    if len(line) == 2:
        try:
            mask = int(line, 16) & 0x3F
        except ValueError:
            return f"ERR {line}", None
        return f"OK {mask:02X}", mask
    return f"ERR {line}", None


class FakeFeather:
    def __init__(self, on_mask=None) -> None:
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)  # no echo or newline translation, like a real CDC port
        self.port = os.ttyname(self.slave)
        self.on_mask = on_mask
        self.received: list[int] = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "FakeFeather":
        os.write(self.master, b"READY\n")
        self._thread.start()
        return self

    def _run(self) -> None:
        buf = b""
        while True:
            try:
                chunk = os.read(self.master, 1024)
            except OSError:
                return
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                answer, mask = respond(raw.decode("ascii", "ignore"))
                if mask is not None:
                    self.received.append(mask)
                    if self.on_mask:
                        self.on_mask(mask)
                os.write(self.master, (answer + "\n").encode())


def main() -> None:
    def show(mask: int) -> None:
        sys.stdout.write("\033[2J\033[H" + render(mask) + "\n")
        sys.stdout.flush()

    fake = FakeFeather(on_mask=show).start()
    if LINK.is_symlink() or LINK.exists():
        LINK.unlink()
    LINK.symlink_to(fake.port)
    print(f"fake Feather on {fake.port} (also {LINK})")
    print(f"start the backend with SERIAL_PORT={LINK}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        if LINK.is_symlink():
            LINK.unlink()


if __name__ == "__main__":
    main()
