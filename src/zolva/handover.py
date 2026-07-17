"""Human handover: one interface, pluggable backends."""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod

import httpx
from pydantic import BaseModel

from zolva.bridge import Message
from zolva.signing import sign_payload

logger = logging.getLogger("zolva.handover")


class HandoverError(Exception):
    """Escalation could not be delivered."""


class Ticket(BaseModel):
    session_id: str
    agent: str
    reason: str
    transcript: list[Message]
    summary: str = ""
    trigger: str = ""  # the exact content that caused the escalation (may not be in transcript)


class HandoverRef(BaseModel):
    id: str
    backend: str


class HandoverBackend(ABC):
    @abstractmethod
    async def escalate(self, ticket: Ticket) -> HandoverRef: ...

    async def resume(self, ref: HandoverRef, resolution: str) -> None:
        return None


class LogBackend(HandoverBackend):
    async def escalate(self, ticket: Ticket) -> HandoverRef:
        logger.warning(
            "HANDOVER session=%s agent=%s reason=%s", ticket.session_id, ticket.agent, ticket.reason
        )
        return HandoverRef(id=f"log-{uuid.uuid4()}", backend="log")


class WebhookBackend(HandoverBackend):
    def __init__(
        self, url: str, secret: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._url = url
        self._secret = secret
        self._client = httpx.AsyncClient(transport=transport, timeout=30.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def escalate(self, ticket: Ticket) -> HandoverRef:
        body = ticket.model_dump_json().encode()
        # timestamp inside the MAC so a captured request can't be replayed later
        ts, sig = sign_payload(self._secret, body)
        try:
            r = await self._client.post(
                self._url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Zolva-Signature": sig,
                    "X-Zolva-Timestamp": ts,
                },
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise HandoverError(f"webhook escalation failed: {e}") from e
        try:
            ref_id = str(r.json()["id"])
        except Exception as e:
            raise HandoverError(f"webhook returned unexpected body: {e}") from e
        return HandoverRef(id=ref_id, backend="webhook")
