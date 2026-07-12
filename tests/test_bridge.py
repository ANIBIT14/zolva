import pytest

from zolva.bridge import (
    BridgeError,
    LLMResponse,
    Message,
    ToolCall,
    get_adapter,
    register_adapter,
)
from zolva.bridge.fake import FakeAdapter


async def test_fake_adapter_plays_script_and_records() -> None:
    fake = FakeAdapter(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="1", name="get_dues", args={"customer_id": "c1"})])
        ]
    )
    resp = await fake.complete(
        model="m", system="s", messages=[Message(role="user", content="hi")], tools=[]
    )
    assert resp.tool_calls[0].name == "get_dues"
    assert fake.calls[0]["model"] == "m"


async def test_fake_adapter_exhausted_script_raises() -> None:
    fake = FakeAdapter(script=[])
    with pytest.raises(BridgeError, match="script exhausted"):
        await fake.complete(model="m", system="s", messages=[], tools=[])


def test_adapter_registry_roundtrip() -> None:
    fake = FakeAdapter(script=[])
    register_adapter("test-provider", lambda: fake)
    assert get_adapter("test-provider") is fake


def test_unknown_provider_raises() -> None:
    with pytest.raises(BridgeError, match="unknown provider"):
        get_adapter("nope-provider")
