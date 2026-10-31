"""Camera frame -> one short word for the Braille cell, via Claude vision.

Keeps the shape of Bridge's vision.py (base64 image block, MOCK_CLAUDE switch) but asks for a
single navigation word with structured output instead of a classroom summary.
"""
import base64
import itertools
import json
import os
import time
from dataclasses import dataclass

try:
    from anthropic import Anthropic, BadRequestError
except ImportError:  # pragma: no cover - handled at runtime
    Anthropic = None
    BadRequestError = Exception

try:
    from .braille import normalize
except ImportError:  # pragma: no cover - running as a script
    from braille import normalize

MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("CLAUDE_EFFORT", "low")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_WORD = 8

SYSTEM = (
    "You are the eyes of a blind person. Each image comes from a phone strapped to their head, "
    "looking straight ahead. Your answer is spelled out letter by letter on a single refreshable "
    "Braille cell at about one letter per second, so it must be one short word."
)

PROMPT = (
    "Name the single most useful thing ahead. "
    "If clearly readable sign text is prominent (for example EXIT, STOP, a room number), give that text. "
    "Otherwise name the main object or obstacle in the path, such as DOOR, CHAIR, TABLE, PERSON, "
    "STAIRS, TREE, WALL, BOTTLE or LAPTOP. "
    f"Use one common English word, letters or digits only, at most {MAX_WORD} characters. "
    "If nothing notable is visible, or the image is too dark or blurry, return an empty word with kind none."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "word": {"type": "string"},
        "kind": {"type": "string", "enum": ["object", "sign", "none"]},
    },
    "required": ["word", "kind"],
    "additionalProperties": False,
}

MOCK_WORDS = ["DOOR", "CHAIR", "EXIT", "TREE", "PERSON"]


@dataclass
class Recognition:
    word: str
    kind: str
    latency_ms: int
    model: str


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes")


class Recognizer:
    def __init__(self) -> None:
        self.mock_reason = ""
        if _flag("MOCK_CLAUDE", False):
            self.mock_reason = "MOCK_CLAUDE=true"
        elif Anthropic is None:
            self.mock_reason = "anthropic package not installed"
        elif not os.getenv("ANTHROPIC_API_KEY"):
            self.mock_reason = "no ANTHROPIC_API_KEY in backend/.env"
        self.mock = bool(self.mock_reason)
        self._client = None if self.mock else Anthropic(max_retries=1, timeout=20)
        self._use_fallbacks = True
        self._mock_words = itertools.cycle(MOCK_WORDS)

    def recognize(self, jpeg: bytes) -> Recognition:
        start = time.monotonic()
        if self.mock:
            time.sleep(0.5)
            return Recognition(next(self._mock_words), "object", int((time.monotonic() - start) * 1000), "mock")

        b64 = base64.standard_b64encode(jpeg).decode("ascii")
        request = dict(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": PROMPT},
                ],
            }],
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
        )
        message = self._create(request)

        if message.stop_reason == "refusal":
            return Recognition("", "none", int((time.monotonic() - start) * 1000), message.model)
        text = next((b.text for b in message.content if getattr(b, "type", None) == "text"), "")
        data = json.loads(text) if text else {}
        word = normalize(data.get("word", ""), max_len=MAX_WORD)
        kind = data.get("kind", "none") if word else "none"
        return Recognition(word, kind, int((time.monotonic() - start) * 1000), message.model)

    def _create(self, request: dict):
        if self._use_fallbacks:
            try:
                return self._client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **request)
            except BadRequestError as exc:
                if "fallback" not in str(exc).lower():
                    raise
                # Account or model without server-side fallbacks: plain request from now on.
                self._use_fallbacks = False
        return self._client.messages.create(**request)
