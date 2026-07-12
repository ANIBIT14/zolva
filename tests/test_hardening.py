"""Tests for post-review hardening: bus coverage, ticket trigger, contract tightening."""

from pathlib import Path

import pytest

from tests.test_orchestrator import CapturingHandover, make_cfg, make_registry
from tests.test_review_fixes import _no_dangling
from zolva.bridge import LLMResponse, ToolCall
from zolva.bridge.fake import FakeAdapter
from zolva.bus import Bus, Step, Verdict
from zolva.config import ConfigError, load_agents
from zolva.orchestrator import BLOCKED_MESSAGE, AgentApp
from zolva.sessions import InMemorySessionStore
from zolva.tools import ToolContractError, ToolRegistry


async def test_model_call_step_emitted_and_blockable() -> None:
    seen: list[str] = []
    bus = Bus()

    async def observer(s: Step) -> None:
        seen.append(s.type)
        return None

    bus.on(observer)
    app = AgentApp(
        {"collections-agent": make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[LLMResponse(text="hi")]),
        bus=bus,
    )
    await app.run("collections-agent", "s1", "hello")
    assert "model_call" in seen

    blocking_bus = Bus()

    async def block_model(s: Step) -> Verdict | None:
        if s.type == "model_call":
            return Verdict(allow=False, reason="PII redaction failed")
        return None

    blocking_bus.on(block_model)
    handover = CapturingHandover()
    app2 = AgentApp(
        {"collections-agent": make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[LLMResponse(text="never sent")]),
        bus=blocking_bus,
        handover=handover,
    )
    assert await app2.run("collections-agent", "s1", "hi") == BLOCKED_MESSAGE
    assert handover.tickets[0].reason == "PII redaction failed"


async def test_handoff_is_blockable_via_bus() -> None:
    bus = Bus()

    async def block_handoffs(s: Step) -> Verdict | None:
        if s.type == "tool_call" and s.data["name"] == "handoff":
            return Verdict(allow=False, reason="handoff vetoed")
        return None

    bus.on(block_handoffs)
    sessions = InMemorySessionStore()
    handover = CapturingHandover()
    cfg = make_cfg(tools=[], handoffs=["human-escalation"])
    fake = FakeAdapter(
        script=[
            LLMResponse(
                tool_calls=[
                    ToolCall(id="1", name="handoff", args={"to": "human-escalation", "reason": "r"})
                ]
            )
        ]
    )
    app = AgentApp(
        {"collections-agent": cfg},
        registry=ToolRegistry(),
        adapter=fake,
        bus=bus,
        sessions=sessions,
        handover=handover,
    )
    assert await app.run("collections-agent", "s1", "hi") == BLOCKED_MESSAGE
    assert handover.tickets[0].reason == "handoff vetoed"
    assert _no_dangling(await sessions.history("s1"))


async def test_blocked_user_msg_trigger_lands_in_ticket() -> None:
    bus = Bus()

    async def block_users(s: Step) -> Verdict | None:
        if s.type == "user_msg":
            return Verdict(allow=False, reason="unsafe input")
        return None

    bus.on(block_users)
    handover = CapturingHandover()
    app = AgentApp(
        {"collections-agent": make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[]),
        bus=bus,
        handover=handover,
    )
    await app.run("collections-agent", "s1", "the blocked message text")
    assert handover.tickets[0].trigger == "the blocked message text"
    # the blocked content must NOT be in the session transcript
    assert all("blocked message" not in m.content for m in handover.tickets[0].transcript)


async def test_unknown_agent_is_typed_error() -> None:
    app = AgentApp({}, registry=ToolRegistry(), adapter=FakeAdapter(script=[]))
    with pytest.raises(ConfigError, match="unknown agent"):
        await app.run("ghost-agent", "s1", "hi")


def test_reserved_tool_name_rejected() -> None:
    reg = ToolRegistry()
    with pytest.raises(ToolContractError, match="reserved"):

        @reg.register
        def handoff(to: str) -> str:
            return to


async def test_extra_args_rejected_not_dropped() -> None:
    reg = make_registry()
    with pytest.raises(ToolContractError):
        await reg.call("get_dues", {"customer_id": "c1", "surprise": "extra"})


def test_yml_extension_loaded(tmp_path: Path) -> None:
    (tmp_path / "i.md").write_text("x")
    (tmp_path / "a.yml").write_text("name: a\ninstructions: i.md\nmodel: {provider: p, name: n}\n")
    assert "a" in load_agents(tmp_path)
