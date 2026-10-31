"""Stand-in for the Feather: a pseudo-terminal that speaks the board's serial protocol.

  .venv/bin/python tools/fake_feather.py            # prints the port path, links .fake-feather

Understands both firmwares: Kiya's Arduino sketch (tokens like "1S1,2S0", "TS"; one
"OK 1S1" reply per token) and firmware/code.py ("19" -> "OK 19"). Each change is drawn
as a 2x3 cell in this terminal.
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


def respond(line: str, mask: int) -> tuple[list[str], int | None]:
    """Replies to one line and the cell state after it (None if the line changed nothing)."""
    line = line.strip()
    if not line:
        return [], None
    if len(line) == 2 and line.upper() not in ("TS",):
        # firmware/code.py: two hex digits
        try:
            new = int(line, 16) & 0x3F
        except ValueError:
            return [f"ERR {line}"], None
        return [f"OK {new:02X}"], new
    # Kiya's sketch: tokens split on , ; space, each "<servo>S<0|1>" or "TS"
    replies, changed = [], False
    for token in line.replace(";", ",").replace(" ", ",").split(","):
        if not token:
            continue
        if token.upper() == "TS":
            replies += [f"TEST {i} OK" for i in range(1, 7)] + ["TEST DONE"]
        elif len(token) == 3 and token[0] in "123456" and token[1] in "Ss" and token[2] in "01":
            bit = 1 << (int(token[0]) - 1)
            mask = mask | bit if token[2] == "1" else mask & ~bit
            changed = True
            replies.append(f"OK {token}")
        else:
            replies.append(f"ERR {token}")
    return replies, (mask if changed else None)


class FakeFeather:
    def __init__(self, on_mask=None) -> None:
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)  # no echo or newline translation, like a real CDC port
        self.port = os.ttyname(self.slave)
        self.on_mask = on_mask
        self.received: list[int] = []
        self.mask = 0
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
                replies, mask = respond(raw.decode("ascii", "ignore"), self.mask)
                if mask is not None:
                    self.mask = mask
                    self.received.append(mask)
                    if self.on_mask:
                        self.on_mask(mask)
                for reply in replies:
                    os.write(self.master, (reply + "\n").encode())


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
