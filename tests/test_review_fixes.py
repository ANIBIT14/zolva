"""Regression tests for final-review findings: every case here was a shipped bug."""

from pathlib import Path

import pytest
from pydantic import BaseModel

from tests.test_orchestrator import CapturingHandover, make_cfg
from zolva.bridge import LLMResponse, Message, ToolCall
from zolva.bridge.fake import FakeAdapter
from zolva.config import ConfigError, load_agents
from zolva.orchestrator import BLOCKED_MESSAGE, AgentApp
from zolva.sessions import InMemorySessionStore
from zolva.tools import ToolRegistry


class Payment(BaseModel):
    amount: int
    currency: str = "INR"


def pydantic_registry() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.register
    def charge(payment: Payment) -> Payment:
        """Charge a payment. Pydantic in, Pydantic out."""
        return Payment(amount=payment.amount * 2, currency=payment.currency)

    return reg


async def test_pydantic_param_arrives_as_model_not_dict() -> None:
    reg = pydantic_registry()
    result = await reg.call("charge", {"payment": {"amount": 100}})
    assert isinstance(result, Payment) and result.amount == 200  # .amount worked → not a dict


async def test_pydantic_return_serialized_as_json_in_session() -> None:
    sessions = InMemorySessionStore()
    fake = FakeAdapter(
        script=[
            LLMResponse(
                tool_calls=[ToolCall(id="1", name="charge", args={"payment": {"amount": 5}})]
            ),
            LLMResponse(text="done"),
        ]
    )
    app = AgentApp(
        {"collections-agent": make_cfg(tools=["charge"])},
        registry=pydantic_registry(),
        adapter=fake,
        sessions=sessions,
    )
    await app.run("collections-agent", "s1", "charge it")
    tool_msg = next(m for m in await sessions.history("s1") if m.role == "tool")
    assert tool_msg.content == '{"amount":10,"currency":"INR"}'  # JSON, not repr


def _no_dangling(history: list[Message]) -> bool:
    answered = {m.tool_call_id for m in history if m.role == "tool"}
    wanted = [tc.id for m in history if m.role == "assistant" for tc in m.tool_calls]
    return all(tc_id in answered for tc_id in wanted)


async def test_session_not_poisoned_after_human_handoff() -> None:
    sessions = InMemorySessionStore()
    cfg = make_cfg(tools=[], handoffs=["human-escalation"])
    fake = FakeAdapter(
        script=[
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="h1", name="handoff", args={"to": "human-escalation", "reason": "r"}
                    )
                ]
            )
        ]
    )
    app = AgentApp(
        {"collections-agent": cfg},
        registry=ToolRegistry(),
        adapter=fake,
        sessions=sessions,
        handover=CapturingHandover(),
    )
    assert await app.run("collections-agent", "s1", "human please") == BLOCKED_MESSAGE
    assert _no_dangling(await sessions.history("s1"))


async def test_provider_error_escalates_not_raises() -> None:
    handover = CapturingHandover()
    app = AgentApp(
        {"collections-agent": make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[]),  # exhausted script raises BridgeError
        handover=handover,
    )
    assert await app.run("collections-agent", "s1", "hi") == BLOCKED_MESSAGE
    assert "provider error" in handover.tickets[0].reason


async def test_tool_crash_escalates_and_closes_pending() -> None:
    sessions = InMemorySessionStore()
    reg = ToolRegistry()

    @reg.register
    def get_dues(customer_id: str) -> dict[str, int]:
        """Dues."""
        raise KeyError(customer_id)  # unknown customer: bank tool crashes

    handover = CapturingHandover()
    fake = FakeAdapter(
        script=[
            LLMResponse(
                tool_calls=[ToolCall(id="1", name="get_dues", args={"customer_id": "ghost"})]
            )
        ]
    )
    app = AgentApp(
        {"collections-agent": make_cfg()},
        registry=reg,
        adapter=fake,
        sessions=sessions,
        handover=handover,
    )
    assert await app.run("collections-agent", "s1", "dues?") == BLOCKED_MESSAGE
    assert "tool error" in handover.tickets[0].reason
    assert _no_dangling(await sessions.history("s1"))


def test_load_agents_missing_dir_raises() -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_agents("/does/not/exist")


def test_load_agents_empty_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no .*yaml"):
        load_agents(tmp_path)


def test_duplicate_agent_names_rejected(tmp_path: Path) -> None:
    (tmp_path / "i.md").write_text("x")
    for fname in ("a.yaml", "b.yaml"):
        (tmp_path / fname).write_text(
            "name: same-agent\ninstructions: i.md\nmodel: {provider: p, name: n}\n"
        )
    with pytest.raises(ConfigError, match="duplicate"):
        load_agents(tmp_path)
