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
        await pacer.say("door")
        while pacer.busy:
            await asyncio.sleep(0.01)

    try:
        asyncio.run(run())
        assert wait_for(lambda: len(fake.received) >= 9)
        # leading 00 from connect, then D,0,O,0,O,0,R,0, then 00 for idle
        sent = fake.received[1:]
        assert sent[:8] == [0x19, 0, 0x15, 0, 0x15, 0, 0x17, 0]
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
