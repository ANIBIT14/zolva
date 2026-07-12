from zolva.bridge import LLMResponse, ToolCall
from zolva.bridge.fake import FakeAdapter
from zolva.config import AgentConfig, ModelConfig
from zolva.orchestrator import BLOCKED_MESSAGE, AgentApp
from zolva.tools import ToolRegistry

from tests.test_orchestrator import CapturingHandover


def cfg(name: str, handoffs: list[str]) -> AgentConfig:
    return AgentConfig(
        name=name,
        instructions=f"You are {name}.",
        model=ModelConfig(provider="test", name="m"),
        handoffs=handoffs,
    )


AGENTS = {
    "collections-agent": cfg("collections-agent", ["hardship-agent", "human-escalation"]),
    "hardship-agent": cfg("hardship-agent", []),
}


async def test_handoff_tool_offered_only_when_configured() -> None:
    fake = FakeAdapter(script=[LLMResponse(text="ok")])
    app = AgentApp(AGENTS, registry=ToolRegistry(), adapter=fake)
    await app.run("hardship-agent", "s1", "hi")
    assert all(t.name != "handoff" for t in fake.calls[0]["tools"])


async def test_agent_to_agent_handoff_switches_and_carries_context() -> None:
    fake = FakeAdapter(
        script=[
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="1",
                        name="handoff",
                        args={"to": "hardship-agent", "reason": "hardship claim"},
                    )
                ]
            ),
            LLMResponse(text="Hardship plan: ..."),
        ]
    )
    app = AgentApp(AGENTS, registry=ToolRegistry(), adapter=fake)
    result = await app.run("collections-agent", "s1", "I lost my job")
    assert result == "Hardship plan: ..."
    assert fake.calls[1]["system"] == "You are hardship-agent."
    assert any("lost my job" in m.content for m in fake.calls[1]["messages"])


async def test_handoff_to_human_escalates() -> None:
    handover = CapturingHandover()
    fake = FakeAdapter(
        script=[
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="1",
                        name="handoff",
                        args={"to": "human-escalation", "reason": "user asked"},
                    )
                ]
            )
        ]
    )
    app = AgentApp(AGENTS, registry=ToolRegistry(), adapter=fake, handover=handover)
    assert await app.run("collections-agent", "s1", "human please") == BLOCKED_MESSAGE
    assert handover.tickets[0].reason == "user asked"


async def test_invalid_handoff_target_fed_back_as_error() -> None:
    fake = FakeAdapter(
        script=[
            LLMResponse(
                tool_calls=[ToolCall(id="1", name="handoff", args={"to": "ghost", "reason": "x"})]
            ),
            LLMResponse(text="ok, staying"),
        ]
    )
    app = AgentApp(AGENTS, registry=ToolRegistry(), adapter=fake)
    assert await app.run("collections-agent", "s1", "hi") == "ok, staying"
    tool_msgs = [m for m in fake.calls[1]["messages"] if m.role == "tool"]
    assert tool_msgs[0].content.startswith("TOOL_ERROR:")
