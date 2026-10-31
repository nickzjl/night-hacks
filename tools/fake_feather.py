"""Stand-in for the Feather: a pseudo-terminal that speaks the board's serial protocol.

  .venv/bin/python tools/fake_feather.py            # prints the port path, links .fake-feather

Follows Kiya's Arduino sketch (Hackaton1/src/main.cpp): servo tokens "1S1,2S0,...",
"TS", Mac mode "HN"/"HF", LCD "L1:text" / "L2:text" / "LC" / "LA", one "OK <token>"
reply per token, and joystick events "JOY L|R|P" (see `joystick()`). Also accepts
firmware/code.py's two-hex-digit lines ("19" -> "OK 19"). The cell and LCD are drawn
in this terminal.
"""
from __future__ import annotations

import os
import sys
import threading
import tty
from pathlib import Path

LINK = Path(__file__).resolve().parent.parent / ".fake-feather"


def render(mask: int, lcd: list[str]) -> str:
    dot = lambda bit: "●" if mask & (1 << bit) else "○"
    rows = [f"|{row:<16}|" for row in lcd]
    return (f"{dot(0)} {dot(3)}   {chr(0x2800 + mask)}  {mask:02X}     {rows[0]}\n"
            f"{dot(1)} {dot(4)}                {rows[1]}\n"
            f"{dot(2)} {dot(5)}")


class FakeFeather:
    def __init__(self, on_change=None) -> None:
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)  # no echo or newline translation, like a real CDC port
        self.port = os.ttyname(self.slave)
        self.on_change = on_change
        self.received: list[int] = []   # cell state after every servo line
        self.mask = 0
        self.lcd = ["", ""]
        self.host_mode = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "FakeFeather":
        os.write(self.master, b"READY\n")
        self._thread.start()
        return self

    def joystick(self, move: str) -> None:
        """Simulate the stick: "L", "R" or "P". Only reported in Mac mode, like the sketch."""
        if self.host_mode:
            os.write(self.master, f"JOY {move}\n".encode())

    def handle_line(self, line: str) -> list[str]:
        line = line.strip()
        if not line:
            return []
        if line[:3].upper() in ("L1:", "L2:"):
            self.lcd[int(line[1]) - 1] = line[3:3 + 16]
            return [f"OK L{line[1]}"]
        if len(line) == 2 and line.upper() not in ("TS", "HN", "HF", "LC", "LA"):
            try:  # firmware/code.py: two hex digits
                self.mask = int(line, 16) & 0x3F
            except ValueError:
                return [f"ERR {line}"]
            self.received.append(self.mask)
            return [f"OK {self.mask:02X}"]
        replies, servo_line = [], False
        for token in line.replace(";", ",").replace(" ", ",").split(","):
            upper = token.upper()
            if not token:
                continue
            if upper == "TS":
                replies += [f"TEST {i} OK" for i in range(1, 7)] + ["TEST DONE"]
            elif upper in ("HN", "HF"):
                self.host_mode = upper == "HN"
                replies.append(f"OK {token}")
            elif upper in ("LC", "LA"):
                if upper == "LC":
                    self.lcd = ["", ""]
                replies.append(f"OK {token}")
            elif len(token) == 3 and token[0] in "123456" and token[1] in "Ss" and token[2] in "01":
                bit = 1 << (int(token[0]) - 1)
                self.mask = self.mask | bit if token[2] == "1" else self.mask & ~bit
                servo_line = True
                replies.append(f"OK {token}")
            else:
                replies.append(f"ERR {token}")
        if servo_line:
            self.received.append(self.mask)
        return replies

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
                replies = self.handle_line(raw.decode("ascii", "ignore"))
                if self.on_change:
                    self.on_change(self)
                for reply in replies:
                    os.write(self.master, (reply + "\n").encode())


def main() -> None:
    def show(fake: FakeFeather) -> None:
        sys.stdout.write("\033[2J\033[H" + render(fake.mask, fake.lcd) + "\n")
        sys.stdout.flush()

    fake = FakeFeather(on_change=show).start()
    if LINK.is_symlink() or LINK.exists():
        LINK.unlink()
    LINK.symlink_to(fake.port)
    print(f"fake Feather on {fake.port} (also {LINK})")
    print("type l / r / p + Enter here to move the joystick")
    try:
        for line in sys.stdin:
            move = {"l": "L", "r": "R", "p": "P"}.get(line.strip().lower())
            if move:
                fake.joystick(move)
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        if LINK.is_symlink():
            LINK.unlink()


if __name__ == "__main__":
    main()
