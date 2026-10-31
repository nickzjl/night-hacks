import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from backend.braille import NUMBER_SIGN, normalize, text_to_cells  # noqa: E402
from backend.display import BraillePacer  # noqa: E402
from backend.serial_link import SerialLink  # noqa: E402
from fake_feather import FakeFeather  # noqa: E402


def masks(text):
    return [m for m, _ in text_to_cells(text)]


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_letters_match_standard_braille():
    # d = 1-4-5, o = 1-3-5, r = 1-2-3-5
    assert masks("DOOR") == [0x19, 0x15, 0x15, 0x17]
    assert masks("exit") == [0x11, 0x2D, 0x0A, 0x1E]


def test_digits_get_number_sign():
    # "B12": b, number sign, 1 (= a), 2 (= b)
    assert masks("B12") == [0x03, NUMBER_SIGN, 0x01, 0x03]
    assert [label for _, label in text_to_cells("B12")] == ["B", "#", "1", "2"]


def test_normalize_strips_punctuation_and_limits_length():
    assert normalize("  exit! ") == "EXIT"
    assert normalize("a/b c") == "A B C"
    assert normalize("x" * 30) == "X" * 12


def test_serial_link_talks_to_fake_feather():
    fake = FakeFeather().start()
    link = SerialLink(fake.port)
    link.start()
    try:
        assert wait_for(lambda: link.connected), link.last_error
        assert link.write_mask(0x19)
        # Kiya's sketch acks each token; the last one for D (dots 1-4-5) is "6S0".
        assert wait_for(lambda: link.last_ack == "6S0"), link.status()
        assert wait_for(lambda: 0x19 in fake.received)
        assert link.last_sent == "1S1,2S0,3S0,4S1,5S1,6S0"
    finally:
        link.stop()


def test_wire_formats():
    from backend.serial_link import format_mask

    assert format_mask(0x19, "servo") == "1S1,2S0,3S0,4S1,5S1,6S0"
    assert format_mask(0x3F, "servo") == "1S1,2S1,3S1,4S1,5S1,6S1"
    assert format_mask(0x00, "servo") == "1S0,2S0,3S0,4S0,5S0,6S0"
    assert format_mask(0x19, "hex") == "19"


def test_pacer_spells_word_with_blank_between_letters():
    fake = FakeFeather().start()
    link = SerialLink(fake.port)
    link.start()
    assert wait_for(lambda: link.connected)
    os.environ.update(HOLD_MS="20", GAP_MS="10", WORD_GAP_MS="10")
    states = []

    async def on_change(state):
        states.append(state)

    async def run():
        pacer = BraillePacer(link, on_change)
        pacer.mode = "auto"
        await pacer.say("door")
        while pacer.busy:
            await asyncio.sleep(0.01)

    try:
        asyncio.run(run())
        assert wait_for(lambda: len(fake.received) >= 8)
        # D, down, O, down, O, down, R, word gap (the connect handshake moves no servos)
        assert fake.received[:8] == [0x19, 0, 0x15, 0, 0x15, 0, 0x17, 0]
        assert fake.host_mode  # backend put the board in Mac mode
        assert states[-1]["busy"] is False
    finally:
        link.stop()
        for key in ("HOLD_MS", "GAP_MS", "WORD_GAP_MS"):
            os.environ.pop(key, None)


def test_app_say_and_frame_endpoints(monkeypatch):
    monkeypatch.setenv("SERIAL_PORT", "off")
    monkeypatch.setenv("MOCK_CLAUDE", "true")
    monkeypatch.setenv("HOLD_MS", "20")
    monkeypatch.setenv("GAP_MS", "10")
    monkeypatch.setenv("WORD_GAP_MS", "10")
    import importlib

    import backend.main as main
    importlib.reload(main)
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        assert client.get("/health").json()["status"] == "ok"
        with client.websocket_connect("/ws/ui") as ui:
            assert ui.receive_json()["type"] == "state"
            assert client.post("/api/say", json={"word": "exit"}).json()["word"] == "EXIT"
            # Phone sends a frame; mock recognizer should spell its first canned word.
            with client.websocket_connect("/ws/phone") as phone:
                assert phone.receive_json()["type"] == "status"
                phone.send_bytes(b"\xff\xd8fake-jpeg\xff\xd9")
                phone.send_json({"type": "telemetry", "distance": 1.1, "level": 2})
                deadline = time.monotonic() + 5
                words = set()
                while time.monotonic() < deadline and "DOOR" not in words:
                    msg = ui.receive()
                    if msg.get("text"):
                        import json
                        state = json.loads(msg["text"])
                        words.add(state["braille"]["word"])
                assert "DOOR" in words
                assert main.hub.telemetry["level"] == 2


class StubLink:
    """Records (time, mask) for every cell change instead of talking to a board."""

    def __init__(self):
        self.writes = []

    def write_mask(self, mask):
        self.writes.append((time.monotonic(), mask))
        return True

    def write_frame(self, mask, lcd=None):
        self.lcd = lcd
        return self.write_mask(mask)


def _pacer(link, hold=60, gap=20, word_gap=150, mode="auto"):
    async def on_change(_state):
        pass

    pacer = BraillePacer(link, on_change)
    pacer.set_timing(hold, gap, word_gap)
    pacer.mode = mode
    return pacer


def _gap_after(writes, index):
    """Seconds the cell stayed in writes[index] before the next change."""
    return writes[index + 1][0] - writes[index][0]


def test_space_in_phrase_is_a_word_gap_not_a_blank_letter():
    link = StubLink()

    async def run():
        pacer = _pacer(link)
        await pacer.say("a b")
        while pacer.busy:
            await asyncio.sleep(0.005)

    asyncio.run(run())
    masks = [m for _, m in link.writes]
    assert masks == [0x01, 0x00, 0x03, 0x00]  # A, down, B, down: no blank "space" letter
    assert abs(_gap_after(link.writes, 1) - 0.150) < 0.04  # down time between words
    assert abs(_gap_after(link.writes, 0) - 0.060) < 0.04  # letter hold


def test_queued_word_starts_after_exactly_one_word_gap():
    link = StubLink()

    async def run():
        pacer = _pacer(link)
        await pacer.say("ab")
        assert await pacer.queue("c") == "C"
        assert pacer.state()["next"] == "C"
        while pacer.busy:
            await asyncio.sleep(0.005)
        assert pacer.state()["next"] == ""

    asyncio.run(run())
    masks = [m for _, m in link.writes]
    assert masks == [0x01, 0x00, 0x03, 0x00, 0x09, 0x00]  # A, gap, B, word gap, C, word gap
    assert abs(_gap_after(link.writes, 1) - 0.020) < 0.03  # between letters
    assert abs(_gap_after(link.writes, 3) - 0.150) < 0.04  # between words


def test_say_interrupts_and_drops_queue():
    link = StubLink()

    async def run():
        pacer = _pacer(link, hold=200)
        await pacer.say("ab")
        await pacer.queue("c")
        await asyncio.sleep(0.05)
        await pacer.say("z")
        while pacer.busy:
            await asyncio.sleep(0.005)

    asyncio.run(run())
    masks = [m for _, m in link.writes]
    assert masks[0] == 0x01 and 0x35 in masks and 0x09 not in masks  # Z replaced AB, C dropped


def test_messages_are_not_trimmed_to_camera_word_length():
    labels = [label for _, label in text_to_cells("DINNER IS READY AT 7")]
    assert labels[-2:] == ["#", "7"] and len(labels) == 21
