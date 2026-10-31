"""Puts words on the single Braille cell, one character at a time, and mirrors it on the LCD.

Two modes (NAV_MODE, switchable from the dashboard):
  manual (default)  new text loads at its first character and the cell holds it; the joystick
                    moves left/right one character and a press returns to the first one.
  auto              timed playback, described below.

LCD: top row = the text (a 16-character window that follows the position for longer
text), bottom row = "CHAR 2/4: O". Every change writes servos + LCD in one serial write.

Auto mode timing lives here, not in the firmware, so it can be tuned without reflashing:
raise the dots for HOLD_MS, drop them all for GAP_MS between letters (so "OO" in DOOR
reads as two letters), and keep them all down for WORD_GAP_MS between words, both for
spaces inside a phrase and between one word and the next. Defaults: a new letter every
3.5 s and 1.25 s of down time between words.

`say` interrupts whatever is being spelled; `queue` waits for the current word and its
word gap to finish, so live recognition can line up the next word without a longer pause.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Awaitable, Callable

try:
    from .braille import MAX_WORD_LEN, normalize, text_to_cells
    from .serial_link import SerialLink
except ImportError:  # pragma: no cover - running as a script
    from braille import MAX_WORD_LEN, normalize, text_to_cells
    from serial_link import SerialLink

SPACE = "␣"  # label text_to_cells gives a space
LCD_WIDTH = 16
IDLE_LCD = ["LEXIS READY", "PRESS TO SCAN"]


def lcd_window(text: str, pos: int, width: int = LCD_WIDTH) -> str:
    """The `width` characters of `text` around position `pos` (all of it if it fits)."""
    if len(text) <= width:
        return text
    start = min(max(0, pos - width // 2 + 1), len(text) - width)
    return text[start:start + width]


def _ms(name: str, default: int) -> float:
    return int(os.getenv(name, str(default))) / 1000


class BraillePacer:
    def __init__(self, link: SerialLink, on_change: Callable[[dict], Awaitable[None]]) -> None:
        self.link = link
        self.on_change = on_change
        self.hold = _ms("HOLD_MS", 3250)  # + GAP_MS = a new letter every 3.5 s
        self.gap = _ms("GAP_MS", 250)
        self.word_gap = _ms("WORD_GAP_MS", 1250)
        self.word = ""
        self.labels: list[str] = []
        self.index = -1
        self.mask = 0
        self.busy = False
        self.last_word = ""
        self.last_word_at = 0.0
        self.next_word = ""
        self.priority = 0          # 0 = camera word, 1 = message from a loved one
        self.mode = os.getenv("NAV_MODE", "manual").strip().lower()
        if self.mode not in ("manual", "auto"):
            self.mode = "manual"
        self.cells: list[tuple[int, str]] = []
        self.loaded_at = 0.0
        self.last_nav_at = 0.0
        self.unread = False        # a loved one's message not yet stepped through to the end
        self.lcd = list(IDLE_LCD)
        self._next: tuple[str, list, float, float, int] | None = None
        self._ends_at = 0.0
        self._task: asyncio.Task | None = None
        self._generation = 0

    def state(self) -> dict:
        return {
            "word": self.word,
            "labels": self.labels,
            "index": self.index,
            "mask": self.mask,
            "busy": self.busy,
            "next": self.next_word,
            "mode": self.mode,
            "lcd": self.lcd,
            "unread": self.unread,
            "timing_ms": {"hold": int(self.hold * 1000), "gap": int(self.gap * 1000), "word_gap": int(self.word_gap * 1000)},
        }

    def set_timing(self, hold_ms: int | None = None, gap_ms: int | None = None, word_gap_ms: int | None = None) -> None:
        if hold_ms is not None:
            self.hold = max(0, hold_ms) / 1000
        if gap_ms is not None:
            self.gap = max(0, gap_ms) / 1000
        if word_gap_ms is not None:
            self.word_gap = max(0, word_gap_ms) / 1000

    def time_left(self) -> float:
        """Seconds until the current word (including its word gap) is done; 0 when idle."""
        return max(0.0, self._ends_at - time.monotonic()) if self.busy else 0.0

    def estimate(self, text: str, max_len: int = MAX_WORD_LEN) -> float:
        """Seconds `text` takes on the cell, including the word gap after it."""
        cells = text_to_cells(normalize(text, max_len))
        return self._duration(cells, self.hold, self.gap) if cells else 0.0

    # ---- manual (joystick) navigation ---------------------------------------------------
    async def set_mode(self, mode: str) -> None:
        if mode not in ("manual", "auto") or mode == self.mode:
            return
        self.mode = mode
        text, priority = self.word, self.priority
        if mode == "manual":
            self._cancel()
            self._generation += 1
            self._next, self.next_word, self.busy = None, "", False
            if text:
                await self._load(text, len(text), priority)
        elif text:
            await self.say(text, max_len=len(text), priority=priority)

    async def step(self, delta: int) -> None:
        """Joystick left (-1) / right (+1). Stops at the ends. Using the joystick during
        timed playback switches to manual at the current letter."""
        if not self.cells:
            return
        if self.mode == "auto":
            self._cancel()
            self._generation += 1
            self._next, self.next_word, self.busy = None, "", False
            self.mode = "manual"
        current = self.index if self.index >= 0 else 0
        index = max(0, min(len(self.cells) - 1, current + delta))
        self.last_nav_at = time.monotonic()
        if index == len(self.cells) - 1:
            self.unread = False
        await self._show(index, self.cells[index][0])

    async def restart(self) -> None:
        """Joystick press: back to the first character."""
        if self.cells:
            await self.step(-len(self.cells))

    async def refresh(self) -> None:
        """Re-send the current cell and LCD (after the board reconnects or resets)."""
        await self._show(self.index, self.mask)

    def holding_message(self, hold_s: float) -> bool:
        """A loved one's message is on the cell and hasn't been read to the end yet."""
        return self.priority >= 1 and self.unread and time.monotonic() - self.loaded_at < hold_s

    async def _load(self, text: str, max_len: int, priority: int) -> str:
        word = normalize(text, max_len)
        if not word:
            return ""
        self._cancel()
        self._generation += 1
        self._next, self.next_word, self.busy = None, "", False
        self.cells = text_to_cells(word)
        self.word, self.labels = word, [label for _, label in self.cells]
        self.priority = priority
        self.unread = priority >= 1 and len(self.cells) > 1
        self.loaded_at = self.last_word_at = time.monotonic()
        self.last_word = word
        await self._show(0, self.cells[0][0])
        return word

    # ---- loading text (both modes) ----------------------------------------------------
    async def say(self, text: str, hold_ms: int | None = None, gap_ms: int | None = None,
                  max_len: int = MAX_WORD_LEN, priority: int = 0) -> str:
        """Show `text` now, replacing what's there. Manual mode: hold its first character.
        Auto mode: spell it; hold_ms / gap_ms apply to this word only."""
        if self.mode == "manual":
            return await self._load(text, max_len, priority)
        plan = self._plan(text, hold_ms, gap_ms, max_len, priority)
        if not plan:
            return ""
        self._cancel()
        self._generation += 1
        self._next, self.next_word = None, ""
        self.busy = True
        self._task = asyncio.create_task(self._run(self._generation, plan))
        return plan[0]

    async def queue(self, text: str, max_len: int = MAX_WORD_LEN, priority: int = 0) -> str:
        """Spell `text` right after the current word's gap; starts now if the cell is idle.
        Never replaces a queued word of higher priority. Manual mode loads it right away."""
        if self.mode == "manual":
            return await self._load(text, max_len, priority)
        if not self.busy:
            return await self.say(text, max_len=max_len, priority=priority)
        if self._next and priority < self._next[4]:
            return ""
        plan = self._plan(text, None, None, max_len, priority)
        if not plan:
            return ""
        self._next, self.next_word = plan, plan[0]
        await self.on_change(self.state())
        return plan[0]

    async def clear(self) -> None:
        self._cancel()
        self._generation += 1
        self._next, self.next_word = None, ""
        self.busy = False
        self.word, self.labels, self.cells, self.unread = "", [], [], False
        await self._show(-1, 0)

    def _plan(self, text: str, hold_ms: int | None, gap_ms: int | None, max_len: int = MAX_WORD_LEN, priority: int = 0):
        word = normalize(text, max_len)
        if not word:
            return None
        hold = self.hold if hold_ms is None else max(0, hold_ms) / 1000
        gap = self.gap if gap_ms is None else max(0, gap_ms) / 1000
        return word, text_to_cells(word), hold, gap, priority

    def _cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    def _duration(self, cells: list, hold: float, gap: float) -> float:
        letters = [label for _, label in cells if label != SPACE]
        words = 1 + sum(1 for _, label in cells if label == SPACE)
        return len(letters) * hold + (len(letters) - words) * gap + words * self.word_gap

    async def _run(self, generation: int, plan) -> None:
        try:
            while plan:
                word, cells, hold, gap, self.priority = plan
                self.word, self.labels, self.cells = word, [label for _, label in cells], cells
                self.last_word = word
                self._ends_at = time.monotonic() + self._duration(cells, hold, gap)
                for i, (mask, label) in enumerate(cells):
                    if label == SPACE:
                        continue  # the down time before it already separates the words
                    await self._show(i, mask)
                    await asyncio.sleep(hold)
                    word_ends = i + 1 == len(cells) or cells[i + 1][1] == SPACE
                    await self._show(i, 0)
                    await asyncio.sleep(self.word_gap if word_ends else gap)
                self.last_word_at = time.monotonic()  # repeat suppression counts from the end
                plan, self._next, self.next_word = self._next, None, ""
        finally:
            # A newer word owns the cell now; don't touch it.
            if generation == self._generation:
                self.busy = False
                self.priority = 0
                self.index = -1
                await self.on_change(self.state())

    def lcd_rows(self) -> list[str]:
        if not self.word:
            return list(IDLE_LCD)
        if self.index < 0 or self.index >= len(self.labels):
            return [lcd_window(self.word, 0), "DONE"]
        label = self.labels[self.index]
        # Number signs are extra cells with no character of their own in the text.
        pos = sum(1 for lab in self.labels[:self.index] if lab != "#")
        shown = "_" if label == SPACE else label
        return [lcd_window(self.word, pos), f"CHAR {self.index + 1}/{len(self.labels)}: {shown}"]

    async def show_status(self, rows: list[str]) -> None:
        """Put a status message on the LCD while no text is loaded (cell stays flat)."""
        if self.cells:
            return
        self.lcd = list(rows)
        await asyncio.to_thread(self.link.write_frame, 0, self.lcd)
        await self.on_change(self.state())

    async def _show(self, index: int, mask: int) -> None:
        self.index, self.mask = index, mask
        self.lcd = self.lcd_rows()
        await asyncio.to_thread(self.link.write_frame, mask, self.lcd)
        await self.on_change(self.state())
