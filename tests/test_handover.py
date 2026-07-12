import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest

from zolva.bridge import Message
from zolva.handover import HandoverError, LogBackend, Ticket, WebhookBackend

TICKET = Ticket(
    session_id="s1",
    agent="collections-agent",
    reason="guardrail: never rule",
    transcript=[Message(role="user", content="hi")],
)


async def test_log_backend_returns_ref() -> None:
    ref = await LogBackend().escalate(TICKET)
    assert ref.backend == "log" and ref.id.startswith("log-")


async def test_webhook_posts_signed_payload() -> None:
    cap: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["body"] = request.content
        cap["sig"] = request.headers["x-zolva-signature"]
        return httpx.Response(200, json={"id": "T-42"})

    b = WebhookBackend(
        "https://desk.bank.internal/hook", secret="s3cr3t", transport=httpx.MockTransport(handler)
    )
    ref = await b.escalate(TICKET)
    assert ref.id == "T-42" and ref.backend == "webhook"
    expected = hmac.new(b"s3cr3t", cap["body"], hashlib.sha256).hexdigest()
    assert cap["sig"] == expected
    assert json.loads(cap["body"])["session_id"] == "s1"


async def test_webhook_http_failure_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    b = WebhookBackend("https://x", secret="s", transport=httpx.MockTransport(handler))
    with pytest.raises(HandoverError):
        await b.escalate(TICKET)
