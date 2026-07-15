import json
from typing import Any

import httpx
import pytest

from zolva.bridge import BridgeError, Message, ToolCall
from zolva.bridge.anthropic import AnthropicAdapter
from zolva.tools import ToolSpec

TOOL = ToolSpec(name="get_dues", description="d", parameters={"type": "object", "properties": {}})


def transport(payload: dict[str, Any], capture: dict[str, Any]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        capture["body"] = json.loads(request.content)
        capture["key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


async def test_wire_format_and_text_parse() -> None:
    cap: dict[str, Any] = {}
    payload = {"content": [{"type": "text", "text": "hello"}]}
    a = AnthropicAdapter(api_key="ak", transport=transport(payload, cap))
    resp = await a.complete(
        model="frontier-model-1",
        system="s",
        messages=[Message(role="user", content="hi")],
        tools=[TOOL],
    )
    assert resp.text == "hello"
    assert cap["key"] == "ak"
    assert cap["body"]["system"] == "s"
    assert cap["body"]["tools"][0]["input_schema"] == TOOL.parameters


async def test_tool_use_parse_and_tool_result_mapping() -> None:
    cap: dict[str, Any] = {}
    payload = {
        "content": [
            {"type": "tool_use", "id": "tu_1", "name": "get_dues", "input": {"customer_id": "c1"}}
        ]
    }
    a = AnthropicAdapter(api_key="ak", transport=transport(payload, cap))
    history = [
        Message(role="user", content="dues?"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="tu_0", name="get_dues", args={"customer_id": "c1"})],
        ),
        Message(role="tool", content='{"amount": 4200}', tool_call_id="tu_0"),
    ]
    resp = await a.complete(model="m", system="s", messages=history, tools=[TOOL])
    assert resp.tool_calls[0].id == "tu_1"
    wire = cap["body"]["messages"]
    assert wire[1]["content"][0]["type"] == "tool_use"
    assert wire[2]["role"] == "user"
    assert wire[2]["content"][0]["type"] == "tool_result"
    assert wire[2]["content"][0]["tool_use_id"] == "tu_0"


async def test_unexpected_response_shape_wrapped_as_bridge_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    a = AnthropicAdapter(api_key="ak", transport=httpx.MockTransport(handler))
    with pytest.raises(BridgeError):
        await a.complete(model="m", system="s", messages=[], tools=[])


def test_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(BridgeError, match="ANTHROPIC_API_KEY"):
        AnthropicAdapter()
