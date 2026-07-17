"""Human-loop resume: a teammate's resolution lands in session + audit, and
the agent has it when the customer returns."""

import json

import pytest
from fastapi.testclient import TestClient

from tests.test_orchestrator import make_cfg
from zolva.bridge import LLMResponse
from zolva.bridge.fake import FakeAdapter
from zolva.bus import Bus, Step, Verdict
from zolva.channels import ChannelHub, FakeChannel
from zolva.config import ConfigError
from zolva.orchestrator import AgentApp
from zolva.serve import create_app
from zolva.signing import sign_payload
from zolva.tools import ToolRegistry

AGENT = "collections-agent"


def make_app(script: list[LLMResponse], bus: Bus | None = None) -> tuple[AgentApp, FakeAdapter]:
    fake = FakeAdapter(script=script)
    app = AgentApp({AGENT: make_cfg(tools=[])}, registry=ToolRegistry(), adapter=fake, bus=bus)
    return app, fake


async def test_resume_records_step_and_session_message() -> None:
    seen: list[Step] = []
    bus = Bus()

    async def observe(step: Step) -> Verdict | None:
        seen.append(step)
        return None

    bus.on(observe)
    app, _ = make_app([], bus=bus)
    await app.resume(AGENT, "s1", "waived the late fee, customer notified by phone")

    assert [s.type for s in seen] == ["resume"]
    assert seen[0].data == {"resolution": "waived the late fee, customer notified by phone"}
    history = await app.sessions.history("s1")
    assert history[-1].role == "assistant"
    assert "[human teammate] waived the late fee" in history[-1].content


async def test_agent_sees_resolution_when_customer_returns() -> None:
    app, fake = make_app([LLMResponse(text="Yes, the fee was waived yesterday.")])
    await app.resume(AGENT, "s1", "waived the late fee")
    await app.run(AGENT, "s1", "did anything happen with my fee?")
    sent = fake.calls[0]["messages"]
    assert any("[human teammate] waived the late fee" in m.content for m in sent)


async def test_resume_unknown_agent_is_config_error() -> None:
    app, _ = make_app([])
    with pytest.raises(ConfigError, match="unknown agent"):
        await app.resume("ghost", "s1", "done")


def serve_client(*, secret: str | None = None) -> tuple[TestClient, AgentApp]:
    app, _ = make_app([LLMResponse(text="ok")])
    hub = ChannelHub(app, channels={"webchat": FakeChannel()}, agents={AGENT: ["webchat"]})
    return TestClient(create_app(app, hub, inbound_secret=secret)), app


async def test_serve_resume_endpoint_happy_path() -> None:
    client, app = serve_client()
    r = client.post(
        f"/sessions/{AGENT}/resume",
        json={"session_id": "webchat:c1", "resolution": "dispute upheld, refund issued"},
    )
    assert r.status_code == 200 and r.json() == {"ok": True}
    history = await app.sessions.history("webchat:c1")
    assert "[human teammate] dispute upheld, refund issued" in history[-1].content


def test_serve_resume_requires_signature_when_secret_set() -> None:
    client, _ = serve_client(secret="s3cr3t")
    body = json.dumps({"session_id": "s1", "resolution": "done"}).encode()
    assert client.post(f"/sessions/{AGENT}/resume", content=body).status_code == 401
    ts, sig = sign_payload("s3cr3t", body)
    r = client.post(
        f"/sessions/{AGENT}/resume",
        content=body,
        headers={"X-Zolva-Signature": sig, "X-Zolva-Timestamp": ts},
    )
    assert r.status_code == 200


def test_serve_resume_bad_body_and_unknown_agent() -> None:
    client, _ = serve_client()
    assert client.post(f"/sessions/{AGENT}/resume", json={"session_id": "s1"}).status_code == 400
    r = client.post("/sessions/ghost/resume", json={"session_id": "s1", "resolution": "done"})
    assert r.status_code == 400 and "unknown agent" in r.json()["error"]
