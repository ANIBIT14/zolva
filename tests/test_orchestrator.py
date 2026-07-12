from zolva.bridge import LLMResponse, ToolCall
from zolva.bridge.fake import FakeAdapter
from zolva.bus import Bus, Step, Verdict
from zolva.config import AgentConfig, ModelConfig
from zolva.handover import HandoverBackend, HandoverRef, Ticket
from zolva.orchestrator import BLOCKED_MESSAGE, AgentApp
from zolva.tools import ToolRegistry


def make_cfg(**kw: object) -> AgentConfig:
    base: dict[str, object] = {
        "name": "collections-agent",
        "instructions": "Collect politely.",
        "model": ModelConfig(provider="test", name="m"),
        "tools": ["get_dues"],
    }
    base.update(kw)
    return AgentConfig.model_validate(base)


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.register
    def get_dues(customer_id: str) -> dict[str, int]:
        """Dues."""
        return {"amount": 4200}

    return reg


class CapturingHandover(HandoverBackend):
    def __init__(self) -> None:
        self.tickets: list[Ticket] = []

    async def escalate(self, ticket: Ticket) -> HandoverRef:
        self.tickets.append(ticket)
        return HandoverRef(id="cap-1", backend="cap")


async def test_plain_reply() -> None:
    app = AgentApp(
        {"collections-agent": make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[LLMResponse(text="Hello!")]),
    )
    assert await app.run("collections-agent", "s1", "hi") == "Hello!"


async def test_tool_call_roundtrip() -> None:
    fake = FakeAdapter(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="1", name="get_dues", args={"customer_id": "c1"})]),
            LLMResponse(text="You owe 4200."),
        ]
    )
    app = AgentApp({"collections-agent": make_cfg()}, registry=make_registry(), adapter=fake)
    assert await app.run("collections-agent", "s1", "dues?") == "You owe 4200."
    # second model call saw the tool result
    tool_msgs = [m for m in fake.calls[1]["messages"] if m.role == "tool"]
    assert "4200" in tool_msgs[0].content


async def test_contract_error_fed_back_to_model() -> None:
    fake = FakeAdapter(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="1", name="get_dues", args={"wrong": True})]),
            LLMResponse(text="Sorry, retrying."),
        ]
    )
    app = AgentApp({"collections-agent": make_cfg()}, registry=make_registry(), adapter=fake)
    await app.run("collections-agent", "s1", "dues?")
    tool_msgs = [m for m in fake.calls[1]["messages"] if m.role == "tool"]
    assert tool_msgs[0].content.startswith("TOOL_ERROR:")


async def test_blocked_response_escalates() -> None:
    bus = Bus()

    async def block_responses(s: Step) -> Verdict | None:
        if s.type == "response":
            return Verdict(allow=False, reason="policy violation")
        return None

    bus.on(block_responses)
    handover = CapturingHandover()
    app = AgentApp(
        {"collections-agent": make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[LLMResponse(text="Buy this fund!")]),
        bus=bus,
        handover=handover,
    )
    result = await app.run("collections-agent", "s1", "advice?")
    assert result == BLOCKED_MESSAGE
    assert handover.tickets[0].reason == "policy violation"


async def test_max_turns_escalates() -> None:
    looping = [
        LLMResponse(tool_calls=[ToolCall(id=str(i), name="get_dues", args={"customer_id": "c"})])
        for i in range(20)
    ]
    handover = CapturingHandover()
    app = AgentApp(
        {"collections-agent": make_cfg()},
        registry=make_registry(),
        adapter=FakeAdapter(script=looping),
        handover=handover,
    )
    assert await app.run("collections-agent", "s1", "dues?") == BLOCKED_MESSAGE
    assert "max turns" in handover.tickets[0].reason
