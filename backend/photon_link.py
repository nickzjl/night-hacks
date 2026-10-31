"""Two-way iMessage through Photon (API 2.0, Python client `photonhq-api`).

Inbound: polls the project's received messages every couple of seconds, so no public URL
is needed. Each message carries Photon's verified sender handle (E.164 phone or iMessage
email) in `sentBy.handle`; that is what contacts are checked against.
Outbound: `send_message` into a conversation (space) as the agent. Photon's shared lines
can't start a conversation, so a contact must text Lexis once before it can text them.

Needs PHOTON_API_KEY and PHOTON_PROJECT_ID in backend/.env (PHOTON_AGENT_HANDLE optional,
used to ignore other lines on the same project). Without them the link stays offline and
`simulate()` can inject messages for testing.
"""
from __future__ import annotations

import asyncio
import collections
import datetime as dt
import os
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

try:
    from photon_api import Photon
    from photon_api.rpc_generated import ListMessagesInput, SendMessageInput
except ImportError:  # pragma: no cover - handled at runtime
    Photon = None

try:  # the SDK marks absent optional fields with pydantic's MISSING sentinel
    from pydantic.experimental.missing_sentinel import MISSING
except ImportError:  # pragma: no cover
    MISSING = object()

POLL_S = 2.0


@dataclass
class Inbound:
    id: str
    sender: str          # Photon's sender handle, e.g. "+14155550123" or "name@icloud.com"
    text: str
    space_id: str
    agent_user_id: str   # our agent's user id on this line; needed to reply
    created_at: str
    simulated: bool = False


def _get(obj, *path):
    """Walk Photon models / dicts, unwrapping RootModel unions; None for missing values."""
    for key in path:
        if obj is None:
            return None
        while hasattr(obj, "root") and not isinstance(obj, dict):
            obj = obj.root
        obj = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
        if obj is MISSING:
            return None
    while hasattr(obj, "root") and not isinstance(obj, (dict, str)):
        obj = obj.root
    return obj


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


class PhotonLink:
    def __init__(self, on_message: Callable[[Inbound], Awaitable[None]], on_event: Callable[[str], None]) -> None:
        self.on_message = on_message
        self.on_event = on_event
        self.api_key = os.getenv("PHOTON_API_KEY", "").strip()
        self.project_id = os.getenv("PHOTON_PROJECT_ID", "").strip()
        self.agent_handle = os.getenv("PHOTON_AGENT_HANDLE", "").strip()
        self.connected = False
        self.last_error = ""
        self.sent: collections.deque[dict] = collections.deque(maxlen=50)  # simulated / offline sends
        self._seen: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._client = None

    @property
    def enabled(self) -> bool:
        return Photon is not None and bool(self.api_key and self.project_id)

    def status(self) -> dict:
        reason = "" if self.enabled else ("photonhq-api not installed" if Photon is None else "set PHOTON_API_KEY and PHOTON_PROJECT_ID in backend/.env")
        return {
            "enabled": self.enabled,
            "connected": self.connected,
            "agent_handle": self.agent_handle,
            "last_error": self.last_error or reason,
        }

    async def run(self) -> None:
        if not self.enabled:
            return
        self._client = Photon(headers={"Authorization": f"Bearer {self.api_key}"})
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=10)
        while True:
            try:
                messages = await asyncio.to_thread(self._list_since, since)
                if not self.connected:
                    self.on_event("iMessage (Photon) connected")
                self.connected, self.last_error = True, ""
                for inbound, created in messages:
                    # Re-read a small overlap each time; dedupe by message id (delivery is at-least-once).
                    since = max(since, created - dt.timedelta(seconds=30))
                    if inbound.id in self._seen:
                        continue
                    self._seen[inbound.id] = None
                    while len(self._seen) > 500:
                        self._seen.popitem(last=False)
                    await self.on_message(inbound)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.connected or exc.__class__.__name__ + str(exc)[:80] not in self.last_error:
                    self.on_event(f"iMessage poll failed: {str(exc)[:160]}")
                self.connected, self.last_error = False, f"{exc.__class__.__name__}: {str(exc)[:200]}"
                await asyncio.sleep(5)
            await asyncio.sleep(POLL_S)

    def _list_since(self, since: dt.datetime) -> list[tuple[Inbound, dt.datetime]]:
        out = []
        token = None
        while True:
            query = {"direction": "received", "order": "asc", "platform": "imessage", "createdSince": _iso(since), "pageSize": 50}
            if token:
                query["pageToken"] = token
            page = self._client.projects.list_messages(ListMessagesInput(path={"id": self.project_id}, query=query))
            for item in page.items or []:
                parsed = self._parse(item)
                if parsed:
                    out.append(parsed)
            token = _get(page, "nextPageToken")
            if not token:
                return out

    def _parse(self, item) -> tuple[Inbound, dt.datetime] | None:
        if _get(item, "status") != "received" or _get(item, "platform") != "imessage":
            return None
        if _get(item, "content", "type") != "text":
            return None
        receiver = _get(item, "receivedBy", "handle") or ""
        if self.agent_handle and receiver and _norm(receiver) != _norm(self.agent_handle):
            return None  # another line on the same project
        created = _get(item, "createdAt") or _iso(dt.datetime.now(dt.timezone.utc))
        inbound = Inbound(
            id=_get(item, "id"),
            sender=_get(item, "sentBy", "handle") or "",
            text=(_get(item, "content", "text") or "").strip(),
            space_id=_get(item, "space", "id") or "",
            agent_user_id=_get(item, "receivedBy", "id") or "",
            created_at=created,
        )
        when = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
        return inbound, when

    async def send(self, space_id: str, agent_user_id: str, text: str, reply_to: str | None = None) -> bool:
        """Send `text` into a conversation. Returns False (and logs) when Photon isn't configured."""
        record = {"space_id": space_id, "text": text, "at": _iso(dt.datetime.now(dt.timezone.utc))}
        if not self.enabled or not self._client or space_id.startswith("sim:"):
            self.sent.append({**record, "simulated": True})
            return False
        body = {"spaceId": space_id, "senderId": agent_user_id, "content": {"type": "text", "format": "plain", "text": text}}
        if reply_to:
            body["parentMessageId"] = reply_to
        key = f"lexis:{reply_to or uuid.uuid4().hex}:{abs(hash(text)) % 10**8}"
        try:
            await asyncio.to_thread(
                self._client.projects.send_message,
                SendMessageInput(path={"id": self.project_id}, headers={"idempotency-key": key}, body=body),
            )
            self.sent.append(record)
            return True
        except Exception as exc:
            self.last_error = f"send failed: {str(exc)[:200]}"
            self.on_event(self.last_error)
            return False

    def simulate(self, sender: str, text: str) -> Inbound:
        """A fake inbound message for testing without Photon (replies go to `sent`)."""
        now = _iso(dt.datetime.now(dt.timezone.utc))
        return Inbound(id=f"sim-{uuid.uuid4().hex[:8]}", sender=sender, text=text, space_id=f"sim:{_norm(sender)}",
                       agent_user_id="sim-agent", created_at=now, simulated=True)


def _norm(handle: str) -> str:
    """Comparable form of a handle: emails lowercased, phone numbers as +digits."""
    handle = (handle or "").strip()
    if "@" in handle:
        return handle.lower()
    digits = "".join(ch for ch in handle if ch.isdigit())
    if len(digits) == 10:  # bare US number
        digits = "1" + digits
    return "+" + digits if digits else ""


normalize_handle = _norm
