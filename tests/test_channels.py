"""Channels plugin: one CX endpoint per company, any agent reachable on declared channels."""

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import examples.mockbank.bank  # noqa: F401  (registers tools into default_registry)
from zolva import AgentApp, BLOCKED_MESSAGE, Step, Verdict
from zolva.bridge import LLMResponse
from zolva.bridge.fake import FakeAdapter
from zolva.channels import (
    ChannelError,
    ChannelHub,
    FakeChannel,
    InboundMessage,
    WebhookChannel,
)
from zolva.config import ConfigError

AGENTS_DIR = Path(__file__).parent.parent / "examples" / "mockbank" / "agents"
CHANNELS_YAML = Path(__file__).parent.parent / "examples" / "mockbank" / "channels.yaml"


def _app(replies: list[str]) -> AgentApp:
    fake = FakeAdapter(script=[LLMResponse(text=t) for t in replies])
    return AgentApp.from_config(AGENTS_DIR, adapter=fake)


def _hub(
    app: AgentApp, agents: dict[str, list[str]] | None = None
) -> tuple[ChannelHub, FakeChannel]:
    ch = FakeChannel()
    hub = ChannelHub(
        app,
        channels={"whatsapp": ch},
        agents=agents if agents is not None else {"collections-agent": ["whatsapp"]},
    )
    return hub, ch


# ---- dispatch ----


async def test_dispatch_runs_agent_and_replies_on_channel() -> None:
    hub, ch = _hub(_app(["Your dues are 4200."]))
    reply = await hub.dispatch(
        "whatsapp", "collections-agent", {"session_id": "wa-91", "text": "dues?"}
    )
    assert reply == "Your dues are 4200."
    assert ch.sent == [("wa-91", "Your dues are 4200.")]


async def test_sessions_are_isolated_per_channel() -> None:
    """The same raw session_id on two channels must never share history."""
    app = _app(["hi wa", "hi web"])
    ch_a, ch_b = FakeChannel(), FakeChannel()
    hub = ChannelHub(
        app,
        channels={"whatsapp": ch_a, "webchat": ch_b},
        agents={"collections-agent": ["whatsapp", "webchat"]},
    )
    await hub.dispatch("whatsapp", "collections-agent", {"session_id": "s1", "text": "a"})
    await hub.dispatch("webchat", "collections-agent", {"session_id": "s1", "text": "b"})
    h1 = await app.sessions.history("whatsapp:s1")
    h2 = await app.sessions.history("webchat:s1")
    assert [m.content for m in h1 if m.role == "user"] == ["a"]
    assert [m.content for m in h2 if m.role == "user"] == ["b"]


async def test_unknown_channel_rejected() -> None:
    hub, _ = _hub(_app([]))
    with pytest.raises(ChannelError, match="unknown channel"):
        await hub.dispatch("sms", "collections-agent", {"session_id": "1", "text": "x"})


async def test_channel_not_allowed_for_agent_rejected() -> None:
    """Per-agent channel allowlist mirrors the tool allowlist: not declared = not reachable."""
    hub, ch = _hub(_app([]), agents={"collections-agent": []})
    with pytest.raises(ChannelError, match="not allowed"):
        await hub.dispatch("whatsapp", "collections-agent", {"session_id": "1", "text": "x"})
    assert ch.sent == []


async def test_malformed_inbound_payload_rejected() -> None:
    hub, _ = _hub(_app([]))
    with pytest.raises(ChannelError, match="invalid inbound"):
        await hub.dispatch("whatsapp", "collections-agent", {"text": "no session id"})


async def test_non_object_inbound_payload_rejected() -> None:
    hub, _ = _hub(_app([]))
    with pytest.raises(ChannelError, match="invalid inbound"):
        await hub.dispatch("whatsapp", "collections-agent", ["not", "an", "object"])  # type: ignore[arg-type]


async def test_channel_steps_emitted_on_bus() -> None:
    app = _app(["ok"])
    seen: list[Step] = []

    async def spy(step: Step) -> None:
        seen.append(step)

    app.bus.on(spy)
    hub, _ = _hub(app)
    await hub.dispatch("whatsapp", "collections-agent", {"session_id": "s1", "text": "hello"})
    channel_steps = [s for s in seen if s.type == "channel"]
    assert [s.data["direction"] for s in channel_steps] == ["in", "out"]
    assert all(s.data["channel"] == "whatsapp" for s in channel_steps)
    assert channel_steps[0].session_id == "whatsapp:s1"


async def test_blocked_outbound_sends_blocked_message() -> None:
    app = _app(["secret internal details"])

    async def block_outbound(step: Step) -> Verdict | None:
        if step.type == "channel" and step.data["direction"] == "out":
            return Verdict(allow=False, reason="policy")
        return None

    app.bus.on(block_outbound)
    hub, ch = _hub(app)
    reply = await hub.dispatch("whatsapp", "collections-agent", {"session_id": "s1", "text": "hi"})
    assert reply == BLOCKED_MESSAGE
    assert ch.sent == [("s1", BLOCKED_MESSAGE)]


# ---- WebhookChannel ----


async def test_webhook_channel_sends_signed_payload() -> None:
    cap: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["body"] = request.content
        cap["sig"] = request.headers["x-zolva-signature"]
        cap["ts"] = request.headers["x-zolva-timestamp"]
        return httpx.Response(200)

    ch = WebhookChannel(
        "https://gw.bank.internal/wa/send", secret="s3cr3t", transport=httpx.MockTransport(handler)
    )
    await ch.send("wa-91", "hello")
    expected = hmac.new(
        b"s3cr3t", cap["ts"].encode() + b"." + cap["body"], hashlib.sha256
    ).hexdigest()
    assert cap["sig"] == expected
    assert json.loads(cap["body"]) == {"session_id": "wa-91", "text": "hello"}


async def test_webhook_channel_delivery_failure_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    ch = WebhookChannel("https://x", secret="s", transport=httpx.MockTransport(handler))
    with pytest.raises(ChannelError, match="delivery failed"):
        await ch.send("s1", "hi")


async def test_webhook_channel_receive_parses_and_keeps_meta() -> None:
    ch = WebhookChannel("https://x", secret="s")
    msg = await ch.receive({"session_id": "s1", "text": "hi", "wa_profile": "Ravi"})
    assert msg == InboundMessage(session_id="s1", text="hi", meta={"wa_profile": "Ravi"})


# ---- config ----


async def test_from_config_builds_hub_and_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCKBANK_WA_SECRET", "test-secret")
    app = _app(["Your dues are 4200."])
    hub = ChannelHub.from_config(CHANNELS_YAML, app)
    reply = await hub.dispatch(
        "ops-log", "collections-agent", {"session_id": "s1", "text": "dues?"}
    )
    assert reply == "Your dues are 4200."


def test_from_config_rejects_unknown_adapter(tmp_path: Path) -> None:
    p = tmp_path / "channels.yaml"
    p.write_text("channels:\n  sms: { adapter: carrier-pigeon }\nagents: {}\n")
    with pytest.raises(ConfigError, match="unknown channel adapter"):
        ChannelHub.from_config(p, _app([]))


def test_from_config_rejects_agent_route_to_unknown_channel(tmp_path: Path) -> None:
    p = tmp_path / "channels.yaml"
    p.write_text("channels:\n  sms: { adapter: log }\nagents:\n  a: [whatsapp]\n")
    with pytest.raises(ConfigError, match="unknown channel"):
        ChannelHub.from_config(p, _app([]))


def test_from_config_rejects_inline_secret(tmp_path: Path) -> None:
    p = tmp_path / "channels.yaml"
    p.write_text(
        "channels:\n  wa: { adapter: webhook, url: https://x, secret: hunter2 }\nagents: {}\n"
    )
    with pytest.raises(ConfigError, match="inline credential"):
        ChannelHub.from_config(p, _app([]))
