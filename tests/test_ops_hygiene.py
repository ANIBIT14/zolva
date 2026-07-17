"""Operational hygiene: client lifecycle, production-metric purity, redacted
exports, and channel-level blocks degrading to handover."""

import json
from pathlib import Path

import pytest

from tests.test_orchestrator import CapturingHandover, make_cfg
from zolva.audit import AuditLog, InMemoryAuditStore, scorecard
from zolva.bridge import LLMResponse
from zolva.bridge.fake import FakeAdapter
from zolva.bridge.openai import OpenAIAdapter
from zolva.bus import Bus, Step, Verdict
from zolva.channels import ChannelHub, FakeChannel, WebhookChannel
from zolva.cli import main
from zolva.feedback import FeedbackQueue
from zolva.handover import WebhookBackend
from zolva.orchestrator import BLOCKED_MESSAGE, AgentApp
from zolva.redaction import BUILTIN_PATTERNS, Redactor
from zolva.tools import ToolRegistry

AGENT = "collections-agent"


def step(sid: str, step_type: str = "user_msg", **data: object) -> Step:
    return Step(type=step_type, session_id=sid, agent=AGENT, data=dict(data))  # type: ignore[arg-type]


# --- client lifecycle --------------------------------------------------------


async def test_adapters_and_backends_close_their_clients() -> None:
    adapter = OpenAIAdapter(api_key="sk-test")
    backend = WebhookBackend("http://t", "s")
    channel = WebhookChannel("http://t", "s")
    for owner in (adapter, backend, channel):
        await owner.aclose()  # type: ignore[attr-defined]
        assert owner._client.is_closed  # type: ignore[attr-defined]


async def test_app_aclose_reaches_wrapped_and_injected_adapters() -> None:
    fake = OpenAIAdapter(api_key="sk-test")
    app = AgentApp(
        {AGENT: make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=fake,
        redactor=Redactor({"email": BUILTIN_PATTERNS["email"]}),  # wraps the adapter
        handover=WebhookBackend("http://t", "s"),
    )
    await app.aclose()
    assert fake._client.is_closed  # unwrapped through RedactingAdapter
    assert app._handover._client.is_closed  # type: ignore[attr-defined]


async def test_hub_aclose_closes_channel_adapters() -> None:
    app = AgentApp(
        {AGENT: make_cfg(tools=[])}, registry=ToolRegistry(), adapter=FakeAdapter(script=[])
    )
    web = WebhookChannel("http://t", "s")
    hub = ChannelHub(app, channels={"web": web}, agents={AGENT: ["web"]})
    await hub.aclose()
    assert web._client.is_closed


# --- eval/synthetic traffic never moves production evidence ------------------


def seeded_log() -> AuditLog:
    log = AuditLog(InMemoryAuditStore())
    for sid in ("s-1", "eval-dues-0-abc", "synthetic-patrol-xyz"):
        log.append(step(sid, "user_msg", text="hi"))
        log.append(step(sid, "response", text="ok"))
    return log


def test_scorecard_excludes_eval_and_synthetic_sessions() -> None:
    log = seeded_log()
    assert scorecard(log).sessions == 1  # only the production session counts
    assert scorecard(log, exclude_session_prefixes=()).sessions == 3  # opt-out


async def test_feedback_ignores_non_production_handovers(tmp_path: Path) -> None:
    q = FeedbackQueue(tmp_path / "failures.db")
    await q._observe(step("eval-dues-0-abc", "handover", reason="graded escalation"))
    await q._observe(step("synthetic-patrol-1", "handover", reason="patrol"))
    assert q.pending() == []
    await q._observe(step("prod-session", "handover", reason="real customer"))
    assert len(q.pending()) == 1


# --- redacted training exports -----------------------------------------------


async def test_export_dataset_masks_pii_when_redactor_given(tmp_path: Path) -> None:
    q = FeedbackQueue(tmp_path / "failures.db")
    app = AgentApp(
        {AGENT: make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[]),  # exhausted -> escalates, capturing the transcript
    )
    q.attach(app)
    await app.run(AGENT, "s1", "my card 4111 1111 1111 1111 was charged twice")
    q.accept(1, tmp_path / "cohort.yaml", expect="files a dispute")
    out = tmp_path / "data.jsonl"
    q.export_dataset(out, redactor=Redactor({"card": BUILTIN_PATTERNS["card"]}))
    text = out.read_text()
    assert "[REDACTED:card]" in text and "4111" not in text
    # DB keeps the truth; only the export is masked
    assert "4111" in json.dumps([m.content for m in q.accepted()[0].transcript])


def test_cli_export_redaction_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "redaction.yaml").write_text("builtin: [card]\n")
    FeedbackQueue(tmp_path / "failures.db")  # create empty DB
    rc = main(
        [
            "export-dataset",
            str(tmp_path / "failures.db"),
            str(tmp_path / "out.jsonl"),
            "--redaction",
            str(tmp_path / "redaction.yaml"),
        ]
    )
    assert rc == 0 and (tmp_path / "out.jsonl").exists()


# --- channel-level blocks degrade to handover, never to silence ---------------


def blocking_bus(direction: str) -> Bus:
    bus = Bus()

    async def hook(s: Step) -> Verdict | None:
        if s.type == "channel" and s.data.get("direction") == direction:
            return Verdict(allow=False, reason=f"blocked {direction}")
        return None

    bus.on(hook)
    return bus


@pytest.mark.parametrize("direction", ["in", "out"])
async def test_blocked_channel_step_opens_a_ticket(direction: str) -> None:
    handover = CapturingHandover()
    app = AgentApp(
        {AGENT: make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[LLMResponse(text="ok")]),
        bus=blocking_bus(direction),
        handover=handover,
    )
    channel = FakeChannel()
    hub = ChannelHub(app, channels={"web": channel}, agents={AGENT: ["web"]})
    reply = await hub.dispatch("web", AGENT, {"session_id": "c1", "text": "hi"})
    assert reply == BLOCKED_MESSAGE
    assert len(handover.tickets) == 1
    assert f"blocked {direction}" in handover.tickets[0].reason
    assert channel.sent == [("c1", BLOCKED_MESSAGE)]  # customer still gets an answer
