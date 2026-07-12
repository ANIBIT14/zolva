"""End-to-end: real config dir, real tools, scripted model. The repo's dogfood gate seed."""

from pathlib import Path

import examples.mockbank.bank  # noqa: F401  (registers tools into default_registry)
from zolva import AgentApp
from zolva.bridge import LLMResponse, ToolCall
from zolva.bridge.fake import FakeAdapter

AGENTS_DIR = Path(__file__).parent.parent / "examples" / "mockbank" / "agents"


async def test_collections_flow_end_to_end() -> None:
    fake = FakeAdapter(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="1", name="get_dues", args={"customer_id": "c1"})]),
            LLMResponse(text="You owe ₹4200, due 2026-07-20. Pay in full or in parts?"),
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="2", name="send_payment_link", args={"customer_id": "c1", "amount": 2100}
                    )
                ]
            ),
            LLMResponse(text="Done — sent a link for ₹2100."),
        ]
    )
    app = AgentApp.from_config(AGENTS_DIR, adapter=fake)
    r1 = await app.run("collections-agent", "sess-1", "what do I owe?")
    assert "4200" in r1
    r2 = await app.run("collections-agent", "sess-1", "I'll pay 2100 now")
    assert "2100" in r2


async def test_cli_validates_example() -> None:
    from zolva.cli import main

    assert main(["validate", str(AGENTS_DIR)]) == 0
