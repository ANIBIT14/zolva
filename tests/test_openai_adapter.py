import json
from typing import Any

import httpx
import pytest

from zolva.bridge import BridgeError, Message
from zolva.bridge.openai import OpenAIAdapter
from zolva.tools import ToolSpec

TOOL = ToolSpec(name="get_dues", description="d", parameters={"type": "object", "properties": {}})


def transport(payload: dict[str, Any], capture: dict[str, Any]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        capture["body"] = json.loads(request.content)
        capture["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


async def test_sends_wire_format_and_parses_text() -> None:
    cap: dict[str, Any] = {}
    payload = {"choices": [{"message": {"content": "hello", "tool_calls": None}}]}
    a = OpenAIAdapter(api_key="sk-test", transport=transport(payload, cap))
    resp = await a.complete(
        model="gpt-5", system="be nice", messages=[Message(role="user", content="hi")], tools=[TOOL]
    )
    assert resp.text == "hello" and resp.tool_calls == []
    assert cap["auth"] == "Bearer sk-test"
    assert cap["body"]["messages"][0] == {"role": "system", "content": "be nice"}
    assert cap["body"]["tools"][0]["function"]["name"] == "get_dues"


async def test_parses_tool_calls() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "get_dues", "arguments": '{"customer_id": "c1"}'},
                        }
                    ],
                }
            }
        ]
    }
    a = OpenAIAdapter(api_key="k", transport=transport(payload, {}))
    resp = await a.complete(model="m", system="s", messages=[], tools=[TOOL])
    assert resp.tool_calls[0].name == "get_dues"
    assert resp.tool_calls[0].args == {"customer_id": "c1"}


async def test_http_error_wrapped_as_bridge_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    a = OpenAIAdapter(api_key="k", transport=httpx.MockTransport(handler))
    with pytest.raises(BridgeError):
        await a.complete(model="m", system="s", messages=[], tools=[])


def test_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(BridgeError, match="OPENAI_API_KEY"):
        OpenAIAdapter()
