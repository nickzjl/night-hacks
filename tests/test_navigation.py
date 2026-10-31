"""Manual (joystick) Braille navigation and the LCD mirror."""
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from backend.display import BraillePacer, lcd_window  # noqa: E402
from backend.serial_link import SerialLink, lcd_text  # noqa: E402
from fake_feather import FakeFeather  # noqa: E402
from test_pipeline import StubLink, wait_for  # noqa: E402


def manual_pacer(link):
    async def on_change(_state):
        pass

    pacer = BraillePacer(link, on_change)
    pacer.mode = "manual"
    return pacer


def test_new_text_holds_first_character_and_joystick_moves():
    link = StubLink()

    async def run():
        pacer = manual_pacer(link)
        await pacer.say("door")
        await asyncio.sleep(0.05)
        assert not pacer.busy and pacer.index == 0  # holds, no timed playback
        assert link.writes[-1][1] == 0x19 and link.lcd == ["DOOR", "CHAR 1/4: D"]
        await pacer.step(1)
        assert link.writes[-1][1] == 0x15 and link.lcd == ["DOOR", "CHAR 2/4: O"]
        await pacer.step(-1)
        await pacer.step(-1)  # already at the first character: stays
        assert pacer.index == 0 and link.lcd[1] == "CHAR 1/4: D"
        await pacer.step(10)  # past the end: stays on the last one
        assert link.lcd[1] == "CHAR 4/4: R" and link.writes[-1][1] == 0x17
        await pacer.restart()
        assert pacer.index == 0 and link.lcd[1] == "CHAR 1/4: D"
        await pacer.say("exit")  # new text starts at its first character
        assert link.lcd == ["EXIT", "CHAR 1/4: E"]

    asyncio.run(run())


def test_long_message_window_follows_position_and_counts_number_sign():
    link = StubLink()

    async def run():
        pacer = manual_pacer(link)
        await pacer.say("dinner is ready at 7", max_len=24, priority=1)
        assert link.lcd == ["DINNER IS READY ", "CHAR 1/21: D"]
        assert pacer.holding_message(120)
        await pacer.step(100)
        assert link.lcd == ["ER IS READY AT 7", "CHAR 21/21: 7"]
        assert not pacer.holding_message(120)  # read to the end
        await pacer.step(-2)
        assert link.lcd[1] == "CHAR 19/21: _"  # space before the number

    asyncio.run(run())


def test_lcd_helpers_fit_the_display():
    assert lcd_window("ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0) == "ABCDEFGHIJKLMNOP"
    assert lcd_window("ABCDEFGHIJKLMNOPQRSTUVWXYZ", 25) == "KLMNOPQRSTUVWXYZ"
    assert lcd_text("café ✓ longer than sixteen") == "caf? ? longer th"


def test_joystick_on_the_board_drives_cell_and_lcd_over_serial():
    fake = FakeFeather().start()
    link = SerialLink(fake.port)

    async def run():
        loop = asyncio.get_running_loop()
        pacer = manual_pacer(link)
        link.on_line = lambda line: loop.call_soon_threadsafe(
            lambda: loop.create_task(pacer.step(-1 if line.endswith("L") else 1)))
        link.start()
        for _ in range(100):
            if fake.host_mode:
                break
            await asyncio.sleep(0.02)
        assert fake.host_mode, "backend should switch the board to Mac mode"
        await pacer.say("door")
        await asyncio.sleep(0.2)
        assert fake.mask == 0x19 and fake.lcd == ["DOOR", "CHAR 1/4: D"]
        fake.joystick("R")
        for _ in range(100):
            if fake.lcd[1] == "CHAR 2/4: O":
                break
            await asyncio.sleep(0.02)
        assert fake.lcd[1] == "CHAR 2/4: O" and fake.mask == 0x15
        assert pacer.index == 1

    try:
        asyncio.run(run())
    finally:
        link.stop()
