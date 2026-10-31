"""Lexis companion: trusted contacts, the wearer's sharing switch, journeys, and the iMessage agent.

Privacy model
- Only handles listed in backend/contacts.json are served, matched against Photon's sender
  handle (never against names in the message text).
- Location and camera history are disclosed only to contacts with "location": true, and only
  while the wearer has sharing on (the iPhone app's switch). Turning sharing off stops updates
  at once and clears the stored location.
- Messages to Braille need "braille": true and work whether or not sharing is on.

The phone is the source of truth for sharing and the journey: it sends a "companion" state
message on every change and every 15 s, plus "location" fixes while sharing is on.
"""
from __future__ import annotations

import asyncio
import collections
import datetime as dt
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .braille import normalize
from .lexis_ai import BRAILLE_MAX, AgentAI
from .photon_link import Inbound, PhotonLink, normalize_handle

HERE = Path(__file__).resolve().parent
CONTACTS_FILE = HERE / "contacts.json"
STATE_FILE = HERE / "lexis_state.json"

STALE_S = 5 * 60            # location older than this is reported as stale
ARRIVAL_RADIUS_M = 40
ARRIVAL_FIXES = 2           # consecutive fixes inside the radius
OBSERVATION_WINDOW_S = 30 * 60
UNKNOWN_REPLY_EVERY_S = 30 * 60

HELP_TEXT = (
    "I'm Lexis. Text me a message for {name} and I'll spell it on {pos} Braille display, "
    "or ask things like \"where is {subj}?\" or \"what has {subj} seen?\". "
    "Reply STOP to pause journey updates, START to resume."
)


@dataclass
class Contact:
    name: str
    handle: str
    braille: bool = True
    location: bool = True


def _pronouns(spec: str) -> tuple[str, str, str]:
    table = {"he": ("he", "him", "his"), "she": ("she", "her", "her"), "they": ("they", "them", "their")}
    return table.get((spec or "they").split("/")[0].strip().lower(), table["they"])


def _ago(seconds: float) -> str:
    if seconds < 45:
        return "just now"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = round(seconds / 3600, 1)
    return f"about {hours:g} hour{'s' if hours != 1 else ''} ago"


def _clock(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch).strftime("%-I:%M %p")


def _distance_m(a: dict, b: dict) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def _fmt_distance(meters: float) -> str:
    feet = meters * 3.28084
    if feet < 1000:
        return f"about {int(round(feet, -1))} ft"
    return f"about {meters / 1609.34:.1f} mi"


def _mask(handle: str) -> str:
    return handle if "@" in handle else handle[:2] + "•" * max(0, len(handle) - 6) + handle[-4:]


def _map_link(loc: dict) -> str:
    return f"https://maps.apple.com/?ll={loc['lat']:.5f},{loc['lon']:.5f}"


class Lexis:
    def __init__(self, hub) -> None:
        self.hub = hub
        self.photon = PhotonLink(self.on_imessage, hub.event)
        self.ai = AgentAI(hub.recognizer)
        self.wearer = "the wearer"
        self.pronouns = _pronouns("they")
        self.contacts: dict[str, Contact] = {}
        self.load_contacts()
        self.sharing = False
        self.journey: dict | None = None
        self.location: dict | None = None
        self.observations: collections.deque[dict] = collections.deque(maxlen=60)
        self.log: collections.deque[dict] = collections.deque(maxlen=40)
        self.threads: dict[str, dict] = {}       # handle -> {space_id, agent_user_id, subscribed}
        self.notified_journey = ""
        self._unknown_replied: dict[str, float] = {}
        self._near_count = 0
        self._load_state()

    # ---- config & persistence -----------------------------------------------------------
    def load_contacts(self) -> None:
        path = CONTACTS_FILE if CONTACTS_FILE.exists() else HERE / "contacts.example.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            self.hub.event(f"contacts: could not read {path.name}: {exc}")
            return
        wearer = data.get("wearer", {})
        self.wearer = wearer.get("name") or "the wearer"
        self.pronouns = _pronouns(wearer.get("pronouns", "they"))
        self.contacts = {}
        for item in data.get("contacts", []):
            handle = normalize_handle(item.get("handle", ""))
            if handle:
                self.contacts[handle] = Contact(item.get("name") or handle, handle, bool(item.get("braille", True)), bool(item.get("location", True)))

    def _load_state(self) -> None:
        try:
            data = json.loads(STATE_FILE.read_text())
            # Simulated test conversations are never real iMessage threads; don't keep them.
            self.threads = {h: t for h, t in data.get("threads", {}).items() if not str(t.get("space_id", "")).startswith("sim:")}
            self.notified_journey = data.get("notified_journey", "")
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        try:
            STATE_FILE.write_text(json.dumps({"threads": self.threads, "notified_journey": self.notified_journey}, indent=1))
        except OSError:
            pass

    # ---- dashboard ----------------------------------------------------------------------
    def state(self) -> dict:
        now = time.time()
        loc = self.location
        return {
            "wearer": self.wearer,
            "sharing": self.sharing,
            "journey": self._journey_summary() if self.journey else None,
            "location": {**loc, "age_s": round(now - loc["at"]), "map": _map_link(loc)} if loc else None,
            "observations": [{**o, "age_s": round(now - o["at"])} for o in list(self.observations)[-8:]][::-1],
            "contacts": [
                {
                    "name": c.name,
                    "handle": _mask(c.handle),
                    "braille": c.braille,
                    "location": c.location,
                    "linked": c.handle in self.threads,
                    "subscribed": self.threads.get(c.handle, {}).get("subscribed", True),
                }
                for c in self.contacts.values()
            ],
            "imessage": self.photon.status(),
            "log": list(self.log)[-12:][::-1],
        }

    def _log(self, direction: str, who: str, text: str) -> None:
        self.log.append({"t": time.strftime("%H:%M:%S"), "dir": direction, "who": who, "text": text[:200]})

    # ---- inputs from the hub ------------------------------------------------------------
    def add_observation(self, word: str, kind: str, scene: str) -> None:
        """Remember a real recognition result. The same thing seen again within a minute
        refreshes the last entry instead of filling the history with repeats."""
        if not (word or scene):
            return
        last = self.observations[-1] if self.observations else None
        if last and word and last["word"] == word and time.time() - last["at"] < 60:
            last.update(at=time.time(), scene=scene or last["scene"])
            return
        self.observations.append({"at": time.time(), "word": word, "kind": kind, "scene": scene})

    async def on_phone(self, data: dict) -> None:
        kind = data.get("type")
        if kind == "companion":
            await self.apply_companion(bool(data.get("sharing")), data.get("journey"))
        elif kind == "location" and self.sharing:
            await self.on_location(data)

    async def apply_companion(self, sharing: bool, journey: dict | None) -> None:
        was_sharing = self.sharing
        self.sharing = sharing
        if was_sharing and not sharing:
            await self.stop_sharing(source="phone")
            return
        if not sharing:
            return
        if journey and journey.get("active"):
            jid = str(journey.get("id", ""))
            known = self.journey and self.journey["id"] == jid
            if not known:
                self.journey = {
                    "id": jid,
                    "destination": journey.get("destination"),
                    "interval_s": max(30, int(journey.get("interval_s", 300))),
                    "started_at": float(journey.get("started_at") or time.time()),
                    "last_update_at": time.time(),
                    "arrived_at": None,
                }
                self._near_count = 0
                if self.notified_journey != jid:
                    self.notified_journey = jid
                    self._save_state()
                    await self.notify(self._start_text())
            else:
                self.journey["interval_s"] = max(30, int(journey.get("interval_s", self.journey["interval_s"])))
        elif self.journey:
            ended = self.journey
            self.journey = None
            if not ended.get("arrived_at"):
                await self.notify(f"{self.wearer} ended the walk.")

    async def stop_sharing(self, source: str) -> None:
        """Immediate stop: no more updates, location forgotten, contacts told once."""
        had_journey = self.journey is not None
        self.sharing = False
        self.journey = None
        self.location = None
        self.hub.event(f"sharing stopped ({source})")
        if source != "phone":
            await self.hub.send_phones({"type": "companion_stop"})
        subj = self.pronouns[0]
        verb = "turns" if subj != "they" else "turn"
        await self.notify(f"{self.wearer} stopped sharing{' the walk' if had_journey else ''}. "
                          f"No more location updates until {subj} {verb} it back on.", force=True)

    async def on_location(self, data: dict) -> None:
        try:
            fix = {"lat": float(data["lat"]), "lon": float(data["lon"]), "acc": float(data.get("acc", 0) or 0),
                   "at": float(data.get("ts") or time.time()), "address": (data.get("address") or "").strip()}
        except (KeyError, TypeError, ValueError):
            return
        if not fix["address"] and self.location:
            fix["address"] = self.location.get("address", "")
        self.location = fix
        await self._check_arrival()

    async def _check_arrival(self) -> None:
        j, loc = self.journey, self.location
        if not j or not loc or j.get("arrived_at") or not j.get("destination"):
            return
        dest = j["destination"]
        if dest.get("lat") is None or loc["acc"] > 65:
            return
        self._near_count = self._near_count + 1 if _distance_m(loc, dest) <= ARRIVAL_RADIUS_M else 0
        if self._near_count >= ARRIVAL_FIXES:
            j["arrived_at"] = time.time()
            await self.notify(f"{self.wearer} arrived at {dest.get('name') or 'the destination'} ({_clock(j['arrived_at'])}).")
            await self.hub.send_phones({"type": "journey_arrived"})

    async def tick_loop(self) -> None:
        """Periodic journey updates."""
        while True:
            await asyncio.sleep(5)
            j = self.journey
            if not (self.sharing and j and not j.get("arrived_at")):
                continue
            if time.time() - j["last_update_at"] >= j["interval_s"]:
                j["last_update_at"] = time.time()
                await self.notify(self._update_text())

    # ---- text building (facts only) -----------------------------------------------------
    def _journey_summary(self) -> dict:
        j = self.journey
        out = {"active": True, "started_at": j["started_at"], "interval_s": j["interval_s"], "arrived_at": j.get("arrived_at"),
               "destination": (j.get("destination") or {}).get("name")}
        if self.location and j.get("destination") and j["destination"].get("lat") is not None:
            out["distance_m"] = round(_distance_m(self.location, j["destination"]))
        return out

    def _start_text(self) -> str:
        j = self.journey
        dest = (j.get("destination") or {}).get("name")
        mins = max(1, round(j["interval_s"] / 60))
        return (f"{self.wearer} started a walk{f' to {dest}' if dest else ''}. "
                f"I'll text an update every {mins} minute{'s' if mins != 1 else ''}. Reply STOP to pause updates.")

    def _update_text(self) -> str:
        loc = self.location
        if not loc:
            return f"No location from {self.wearer}'s phone yet."
        age = time.time() - loc["at"]
        where = f"near {loc['address']}" if loc["address"] else "at the map pin below"
        text = f"{self.wearer} is {where} (as of {_ago(age)})."
        if age > STALE_S:
            text = f"{self.wearer}'s last known location was {where}, {_ago(age)}; no newer fix since."
        dest = (self.journey or {}).get("destination") or {}
        if dest.get("lat") is not None:
            text += f" {_fmt_distance(_distance_m(loc, dest)).capitalize()} from {dest.get('name') or 'the destination'}."
        return f"{text} {_map_link(loc)}"

    def context_for(self, contact: Contact) -> str:
        now = time.time()
        lines = [f"Now: {_clock(now)}. Wearer: {self.wearer} ({'/'.join(self.pronouns[:2])})."]
        if not contact.location:
            lines.append("Location and camera details: NOT PERMITTED for this contact.")
            return "\n".join(lines)
        if not self.sharing:
            lines.append("Sharing: OFF. The wearer is not sharing location or camera details right now.")
            return "\n".join(lines)
        lines.append("Sharing: on.")
        j = self.journey
        if j:
            dest = (j.get("destination") or {}).get("name") or "no destination set"
            status = f"arrived {_ago(now - j['arrived_at'])}" if j.get("arrived_at") else "not arrived yet"
            lines.append(f"Journey: started {_ago(now - j['started_at'])}, destination: {dest}, {status}.")
            if self.location and (j.get("destination") or {}).get("lat") is not None:
                lines.append(f"Distance to destination: {_fmt_distance(_distance_m(self.location, j['destination']))}.")
        else:
            lines.append("Journey: none active (no arrival status to report).")
        loc = self.location
        if loc:
            age = now - loc["at"]
            stale = " (STALE: older than 5 minutes)" if age > STALE_S else ""
            place = loc["address"] or "address unknown"
            lines.append(f"Location: near {place}, accuracy ±{int(loc['acc'])} m, updated {_ago(age)}{stale}. Map: {_map_link(loc)}")
        else:
            lines.append("Location: no GPS fix received yet.")
        recent = [o for o in self.observations if now - o["at"] <= OBSERVATION_WINDOW_S]
        if recent:
            lines.append("Camera observations, newest first:")
            for o in reversed(recent[-12:]):
                detail = f" ({o['scene']})" if o.get("scene") else ""
                lines.append(f"- {_ago(now - o['at'])}: {o['word'] or 'scene'}{detail}")
        else:
            lines.append("Camera observations: none in the last 30 minutes.")
        return "\n".join(lines)

    # ---- iMessage ------------------------------------------------------------------------
    async def notify(self, text: str, force: bool = False) -> None:
        """Text every subscribed location contact who has an open thread with Lexis."""
        if not self.sharing and not force:
            return
        for contact in self.contacts.values():
            thread = self.threads.get(contact.handle)
            if not contact.location or not thread or not thread.get("subscribed", True):
                continue
            await self.photon.send(thread["space_id"], thread["agent_user_id"], text)
            self._log("out", contact.name, text)

    async def reply(self, msg: Inbound, who: str, text: str) -> None:
        await self.photon.send(msg.space_id, msg.agent_user_id, text, reply_to=None if msg.simulated else msg.id)
        self._log("out", who, text)

    async def on_imessage(self, msg: Inbound) -> None:
        handle = normalize_handle(msg.sender)
        contact = self.contacts.get(handle)
        if contact is None:
            self._log("in", "unknown sender", msg.text)
            self.hub.event("iMessage from an unapproved sender ignored")
            if time.time() - self._unknown_replied.get(handle, 0) > UNKNOWN_REPLY_EVERY_S:
                self._unknown_replied[handle] = time.time()
                await self.reply(msg, "unknown sender", "Hi, this is Lexis. I can only talk with contacts the wearer has approved.")
            return

        self._log("in", contact.name, msg.text)
        thread = self.threads.get(handle, {"subscribed": True})
        thread.update(space_id=msg.space_id, agent_user_id=msg.agent_user_id)
        self.threads[handle] = thread
        self._save_state()

        text = msg.text.strip()
        command = text.upper().rstrip(".!")
        subj, obj, pos = self.pronouns
        if command in ("STOP", "UNSUBSCRIBE", "PAUSE"):
            thread["subscribed"] = False
            self._save_state()
            return await self.reply(msg, contact.name, "Okay, I'll stop sending journey updates. Text START to turn them back on.")
        if command in ("START", "RESUME", "SUBSCRIBE"):
            thread["subscribed"] = True
            self._save_state()
            return await self.reply(msg, contact.name, f"Okay, you'll get {self.wearer}'s journey updates while {subj} is sharing.")
        if command in ("HELP", "?"):
            return await self.reply(msg, contact.name, HELP_TEXT.format(name=self.wearer, pos=pos, subj=subj))

        explicit = None
        for prefix in ("BRAILLE:", "B:"):
            if text.upper().startswith(prefix):
                explicit = text[len(prefix):].strip()
        if explicit and len(normalize(explicit, BRAILLE_MAX + 20)) <= BRAILLE_MAX:
            return await self.to_braille(msg, contact, explicit)

        try:
            answer = await asyncio.to_thread(self.ai.respond, explicit or text, self.context_for(contact),
                                             self.wearer, (subj, obj), contact.name)
        except Exception as exc:
            self.hub.event(f"agent reply failed: {exc}")
            return await self.reply(msg, contact.name, "Sorry, I couldn't process that just now. Please try again in a moment.")
        if explicit or answer.action == "braille":
            return await self.to_braille(msg, contact, answer.braille_text or explicit or text)
        await self.reply(msg, contact.name, answer.reply or "Sorry, I'm not sure how to answer that.")

    async def to_braille(self, msg: Inbound, contact: Contact, text: str) -> None:
        if not contact.braille:
            return await self.reply(msg, contact.name, f"Sorry, you aren't set up to send Braille messages to {self.wearer}.")
        word = normalize(text, BRAILLE_MAX)
        if not word:
            return await self.reply(msg, contact.name, "I couldn't turn that into Braille. Try letters and numbers.")
        seconds = await self.hub.braille_message(word, contact.name)
        if self.hub.pacer.mode == "manual":
            subj = self.pronouns[0].capitalize()
            verb = "reads" if subj != "They" else "read"
            text = f"\"{word}\" is on {self.wearer}'s Braille display now. {subj} {verb} it letter by letter with the joystick."
        else:
            text = f"Spelling \"{word}\" on {self.wearer}'s Braille display now (about {round(seconds)} seconds)."
        await self.reply(msg, contact.name, text)
