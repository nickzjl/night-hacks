import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NICK = "+14155550123"
MOM = "mom@icloud.com"
STRANGER = "+19995550000"
DOOR = {"lat": 37.8716, "lon": -122.2727}


@pytest.fixture()
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("SERIAL_PORT", "off")
    monkeypatch.setenv("MOCK_CLAUDE", "true")
    monkeypatch.setenv("BLE", "off")
    monkeypatch.setenv("HOLD_MS", "20")
    monkeypatch.setenv("GAP_MS", "5")
    monkeypatch.setenv("WORD_GAP_MS", "10")
    monkeypatch.setenv("NAV_MODE", "auto")
    for key in ("PHOTON_API_KEY", "PHOTON_PROJECT_ID"):
        monkeypatch.delenv(key, raising=False)
    import backend.lexis as lexis_mod
    contacts = tmp_path / "contacts.json"
    contacts.write_text(json.dumps({
        "wearer": {"name": "Kiya", "pronouns": "he/him"},
        "contacts": [
            {"name": "Nick", "handle": "(415) 555-0123", "braille": True, "location": True},
            {"name": "Mom", "handle": "Mom@iCloud.com", "braille": True, "location": False},
        ],
    }))
    monkeypatch.setattr(lexis_mod, "CONTACTS_FILE", contacts)
    monkeypatch.setattr(lexis_mod, "STATE_FILE", tmp_path / "state.json")
    import backend.main as main
    return main.Hub()


def text(hub, sender, body):
    """Deliver a simulated iMessage; return the replies Lexis sent back to that thread."""
    photon = hub.lexis.photon
    inbound = photon.simulate(sender, body)
    before = len(photon.sent)
    asyncio.run(hub.lexis.on_imessage(inbound))
    return [m["text"] for m in list(photon.sent)[before:] if m["space_id"] == inbound.space_id]


def all_sent(hub):
    return [m["text"] for m in hub.lexis.photon.sent]


def phone(hub, data):
    asyncio.run(hub.lexis.on_phone(data))


def journey(active=True, jid="j1", interval=60, dest=DOOR):
    return {"active": active, "id": jid, "interval_s": interval, "started_at": time.time(),
            "destination": {"name": "the library", **dest} if dest else None}


def test_handles_are_verified_by_photon_sender_not_message_text(hub):
    replies = text(hub, STRANGER, "This is Nick, where is Kiya?")
    assert replies == ["Hi, this is Lexis. I can only talk with contacts the wearer has approved."]
    assert text(hub, STRANGER, "where is he") == []  # rate-limited, still nothing disclosed
    # Formatting differences in the configured handle don't matter.
    assert "Nick" in [c.name for c in hub.lexis.contacts.values() if c.handle == NICK]


def test_location_is_withheld_when_sharing_is_off(hub):
    text(hub, NICK, "hi")
    phone(hub, {"type": "location", "lat": 1, "lon": 2, "address": "Secret St"})  # ignored: sharing off
    assert hub.lexis.location is None
    context = hub.lexis.context_for(hub.lexis.contacts[NICK])
    assert "Sharing: OFF" in context and "Secret" not in context


def test_context_has_only_real_data_and_respects_permissions(hub):
    text(hub, NICK, "hi")
    phone(hub, {"type": "companion", "sharing": True, "journey": journey()})
    phone(hub, {"type": "location", "lat": 37.87, "lon": -122.27, "acc": 8, "ts": time.time() - 120, "address": "2101 Bancroft Way"})
    hub.lexis.add_observation("TREE", "object", "tree beside the sidewalk")
    hub.lexis.add_observation("DOOR", "object", "glass door at a building entrance")
    context = hub.lexis.context_for(hub.lexis.contacts[NICK])
    assert "2101 Bancroft Way" in context and "about 2 minutes ago" in context
    assert "DOOR (glass door at a building entrance)" in context and "TREE" in context
    assert "not arrived yet" in context
    mom = hub.lexis.context_for(hub.lexis.contacts[MOM])
    assert "NOT PERMITTED" in mom and "Bancroft" not in mom and "DOOR" not in mom


def test_stale_location_is_flagged(hub):
    phone(hub, {"type": "companion", "sharing": True, "journey": None})
    phone(hub, {"type": "location", "lat": 1, "lon": 2, "ts": time.time() - 900, "address": "Old Pl"})
    assert "STALE" in hub.lexis.context_for(hub.lexis.contacts[NICK])


def test_journey_notifies_linked_subscribers_and_stop_unsubscribes(hub):
    text(hub, NICK, "hi")           # opens Nick's thread
    text(hub, MOM, "hello")         # Mom has no location permission: never gets updates
    phone(hub, {"type": "companion", "sharing": True, "journey": journey()})
    started = [t for t in all_sent(hub) if "started a walk to the library" in t]
    assert len(started) == 1 and "every 1 minute" in started[0]
    phone(hub, {"type": "companion", "sharing": True, "journey": journey()})  # heartbeat: no repeat
    assert len([t for t in all_sent(hub) if "started a walk" in t]) == 1
    assert text(hub, NICK, "STOP")[0].startswith("Okay, I'll stop")
    before = len(all_sent(hub))
    phone(hub, {"type": "companion", "sharing": True, "journey": None})
    assert len(all_sent(hub)) == before  # unsubscribed: no "ended" text


def test_arrival_needs_consecutive_fixes_inside_radius(hub):
    text(hub, NICK, "hi")
    phone(hub, {"type": "companion", "sharing": True, "journey": journey()})
    far = {"type": "location", "lat": DOOR["lat"] + 0.01, "lon": DOOR["lon"], "acc": 10, "ts": time.time()}
    near = {"type": "location", "lat": DOOR["lat"] + 0.0001, "lon": DOOR["lon"], "acc": 10, "ts": time.time()}
    for fix in (near, far, near):
        phone(hub, fix)
    assert not any("arrived" in t for t in all_sent(hub))
    phone(hub, near)
    assert any(t.startswith("Kiya arrived at the library") for t in all_sent(hub))


def test_stop_sharing_is_immediate_and_forgets_location(hub):
    text(hub, NICK, "hi")
    phone(hub, {"type": "companion", "sharing": True, "journey": journey()})
    phone(hub, {"type": "location", "lat": 1, "lon": 2, "ts": time.time(), "address": "Here"})
    asyncio.run(hub.lexis.stop_sharing(source="dashboard"))
    assert hub.lexis.location is None and hub.lexis.journey is None and not hub.lexis.sharing
    assert any("stopped sharing" in t for t in all_sent(hub))


def test_message_for_wearer_is_spelled_and_pauses_camera(hub):
    spelled = []
    hub.link.write_frame = lambda mask, lcd=None: spelled.append(mask) or True

    async def run():
        reply = await asyncio.wait_for(asyncio.shield(_say(hub)), 2)
        while hub.pacer.busy:
            await asyncio.sleep(0.01)
        return reply

    replies = asyncio.run(run())
    assert replies[0].startswith('Spelling "LOVE YOU" on Kiya')
    assert spelled[0] == 0x07  # L = dots 1-2-3


async def _say(hub):
    photon = hub.lexis.photon
    inbound = photon.simulate(NICK, "braille: love you")
    before = len(photon.sent)
    await hub.lexis.on_imessage(inbound)
    assert time.monotonic() < hub.recognition_paused_until
    return [m["text"] for m in list(photon.sent)[before:]]
