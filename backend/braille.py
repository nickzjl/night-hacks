"""Text -> 6-dot braille cells, one byte per cell.

Bit layout: bit0 = dot 1, bit1 = dot 2, bit2 = dot 3 (left column, top to bottom),
bit3 = dot 4, bit4 = dot 5, bit5 = dot 6 (right column). One bit drives one servo.

Grade 1 (uncontracted) only, so every letter maps to exactly one cell. The letter table
comes from Bridge's braille.py; digits now get the number sign, which Bridge omitted.
"""
import re

NUMBER_SIGN = 0x3C  # dots 3-4-5-6

LETTER_TO_CELL = {
    " ": 0x00,
    "a": 0x01,
    "b": 0x03,
    "c": 0x09,
    "d": 0x19,
    "e": 0x11,
    "f": 0x0B,
    "g": 0x1B,
    "h": 0x13,
    "i": 0x0A,
    "j": 0x1A,
    "k": 0x05,
    "l": 0x07,
    "m": 0x0D,
    "n": 0x1D,
    "o": 0x15,
    "p": 0x0F,
    "q": 0x1F,
    "r": 0x17,
    "s": 0x0E,
    "t": 0x1E,
    "u": 0x25,
    "v": 0x27,
    "w": 0x3A,
    "x": 0x2D,
    "y": 0x3D,
    "z": 0x35,
}

# Digits are written as the number sign followed by a-j (1 = a ... 9 = i, 0 = j).
DIGIT_TO_LETTER = "jabcdefghi"

MAX_WORD_LEN = 12


def normalize(text: str, max_len: int = MAX_WORD_LEN) -> str:
    """Uppercase A-Z, 0-9 and single spaces only, trimmed to `max_len`."""
    cleaned = re.sub(r"[^A-Z0-9 ]", " ", (text or "").upper())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_len].strip()


def text_to_cells(text: str) -> list[tuple[int, str]]:
    """Return (mask, label) per cell. Labels are what the dashboard shows under each cell."""
    cells: list[tuple[int, str]] = []
    in_number = False
    # Callers decide the length limit (camera words vs. messages); don't trim here.
    for ch in normalize(text, max_len=len(text or "")).lower():
        if ch.isdigit():
            if not in_number:
                cells.append((NUMBER_SIGN, "#"))
                in_number = True
            cells.append((LETTER_TO_CELL[DIGIT_TO_LETTER[int(ch)]], ch))
            continue
        in_number = False
        cells.append((LETTER_TO_CELL.get(ch, 0x00), ch.upper() if ch != " " else "␣"))
    return cells


def mask_to_dots(mask: int) -> list[int]:
    """Dot numbers (1-6) raised by `mask`."""
    return [bit + 1 for bit in range(6) if mask & (1 << bit)]
