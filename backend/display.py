"""Paces a word onto the single Braille cell, one character at a time.

All timing lives here, not in the firmware, so it can be tuned without reflashing:
raise the dots for HOLD_MS, drop them all for GAP_MS (so "OO" in DOOR reads as two
letters), and pause WORD_GAP_MS after the word. A new word interrupts the current one.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Awaitable, Callable

try:
    from .braille import normalize, text_to_cells
    from .serial_link import SerialLink
except ImportError:  # pragma: no cover - running as a script
    from braille import normalize, text_to_cells
    from serial_link import SerialLink


def _ms(name: str, default: int) -> float:
    return int(os.getenv(name, str(default))) / 1000


class BraillePacer:
    def __init__(self, link: SerialLink, on_change: Callable[[dict], Awaitable[None]]) -> None:
        self.link = link
        self.on_change = on_change
        self.hold = _ms("HOLD_MS", 900)
        self.gap = _ms("GAP_MS", 250)
        self.word_gap = _ms("WORD_GAP_MS", 1200)
        self.word = ""
        self.labels: list[str] = []
        self.index = -1
        self.mask = 0
        self.busy = False
        self.last_word = ""
        self.last_word_at = 0.0
        self._task: asyncio.Task | None = None
        self._generation = 0

    def state(self) -> dict:
        return {
            "word": self.word,
            "labels": self.labels,
            "index": self.index,
            "mask": self.mask,
            "busy": self.busy,
            "timing_ms": {"hold": int(self.hold * 1000), "gap": int(self.gap * 1000), "word_gap": int(self.word_gap * 1000)},
        }

    async def say(self, text: str) -> str:
        word = normalize(text)
        if not word:
            return ""
        self._cancel()
        self._generation += 1
        cells = text_to_cells(word)
        self.word, self.labels = word, [label for _, label in cells]
        self.last_word, self.last_word_at = word, time.monotonic()
        self.busy = True
        self._task = asyncio.create_task(self._spell(self._generation, [mask for mask, _ in cells]))
        return word

    async def clear(self) -> None:
        self._cancel()
        self._generation += 1
        self.busy = False
        await self._show(-1, 0)

    def _cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _spell(self, generation: int, masks: list[int]) -> None:
        try:
            for i, mask in enumerate(masks):
                await self._show(i, mask)
                await asyncio.sleep(self.hold)
                if self.gap > 0:
                    await self._show(i, 0)
                    await asyncio.sleep(self.gap)
            await self._show(-1, 0)
            await asyncio.sleep(self.word_gap)
        finally:
            # A newer word owns the cell now; don't touch it.
            if generation == self._generation:
                self.busy = False
                await self.on_change(self.state())

    async def _show(self, index: int, mask: int) -> None:
        self.index, self.mask = index, mask
        await asyncio.to_thread(self.link.write_mask, mask)
        await self.on_change(self.state())
