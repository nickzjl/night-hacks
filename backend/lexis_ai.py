"""Claude for the iMessage agent.

One call per incoming text decides whether it is a message meant for the wearer (spell it
on the Braille cell) or a question for Lexis, and answers questions from the CONTEXT block
only. Privacy is enforced before this point: when a contact may not see location/camera
data, or sharing is off, that data is simply not in the context.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

BRAILLE_MAX = 24  # ~1.5 min on the cell at 3.5 s per letter

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["braille", "reply"]},
        "braille_text": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": ["action", "braille_text", "reply"],
    "additionalProperties": False,
}

SYSTEM = """You are Lexis, the assistant inside an accessibility wearable for {name}, who is blind. \
{name}'s approved family and friends text you over iMessage. For each text, choose an action.

action "braille": the text is a message meant for {name} ("tell {obj} dinner is ready", "love you!", \
"call me when you can"). Put a version for a one-letter-at-a-time Braille display in braille_text: \
letters, digits and spaces only, at most {max_len} characters, keep the meaning, drop greetings and \
punctuation if needed. Leave reply empty.

action "reply": the text is a question or request for you ("where is {subj}?", "what has {subj} seen?", \
"has {subj} arrived?", "hi"). Write reply as one or two short, warm, natural sentences in plain text, \
no markdown. Leave braille_text empty.

Rules for replies:
- Use only facts in the CONTEXT block. Never invent places, objects, times or arrival status.
- If a fact is missing or marked stale, say you don't know or that the last update is old, and how old.
- Refer to {name} by name or as {subj}/{obj}. Use relative times ("about 2 minutes ago"), not timestamps.
- Name the street or place, not coordinates. Include the map link only when asked where {subj} is.
- If CONTEXT says sharing is off or not permitted, say {name} isn't sharing location or camera \
details right now, and give no whereabouts."""


@dataclass
class AgentReply:
    action: str
    braille_text: str
    reply: str


class AgentAI:
    def __init__(self, recognizer) -> None:
        # Reuse the camera recognizer's Claude client and fallback handling.
        self.recognizer = recognizer

    @property
    def mock(self) -> bool:
        return self.recognizer.mock

    def respond(self, text: str, context: str, wearer: str, pronouns: tuple[str, str], sender: str) -> AgentReply:
        if self.mock:
            return self._mock(text, context, wearer)
        subj, obj = pronouns
        request = dict(
            model=self.recognizer_model(),
            max_tokens=2000,
            system=SYSTEM.format(name=wearer, subj=subj, obj=obj, max_len=BRAILLE_MAX),
            messages=[{
                "role": "user",
                "content": f"CONTEXT:\n{context}\n\nTEXT FROM {sender}:\n{text}",
            }],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
        message = self.recognizer._create(request)
        if message.stop_reason == "refusal":
            return AgentReply("reply", "", "Sorry, I can't help with that one.")
        raw = next((b.text for b in message.content if getattr(b, "type", None) == "text"), "{}")
        data = json.loads(raw)
        return AgentReply(data.get("action", "reply"), data.get("braille_text", ""), data.get("reply", "").strip())

    def recognizer_model(self) -> str:
        from .vision import MODEL
        return MODEL

    @staticmethod
    def _mock(text: str, context: str, wearer: str) -> AgentReply:
        """Deterministic stand-in so the flow works without an API key."""
        lowered = text.lower().strip()
        if lowered.endswith("?") or re.match(r"^(where|what|has|have|is|did|how|when|are)\b", lowered):
            first = next((line for line in context.splitlines() if line.startswith("Location:")), "")
            return AgentReply("reply", "", f"(mock) {first or 'No location data available.'}")
        return AgentReply("braille", text[:BRAILLE_MAX], "")
