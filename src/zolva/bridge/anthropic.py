"""Anthropic messages adapter."""

from __future__ import annotations

import os
from typing import Any

import httpx

from zolva.bridge import (
    BridgeError,
    LLMResponse,
    Message,
    ToolCall,
    post_with_retry,
    register_adapter,
)
from zolva.tools import ToolSpec


class AnthropicAdapter:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com",
        transport: httpx.AsyncBaseTransport | None = None,
        max_tokens: int = 4096,
        timeout: float = 60.0,
    ) -> None:
        self._max_tokens = max_tokens
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise BridgeError("ANTHROPIC_API_KEY not set and no api_key given")
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            transport=transport,
            timeout=timeout,
        )

    def _wire_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool":
                wire.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id,
                                "content": m.content,
                            }
                        ],
                    }
                )
            elif m.role == "assistant" and m.tool_calls:
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                blocks += [
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args}
                    for tc in m.tool_calls
                ]
                wire.append({"role": "assistant", "content": blocks})
            else:
                wire.append({"role": m.role, "content": m.content})
        return wire

    async def complete(
        self, *, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": self._wire_messages(messages),
        }
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        r = await post_with_retry(
            self._client, "/v1/messages", json_body=body, provider="anthropic"
        )
        try:
            text = ""
            calls: list[ToolCall] = []
            for block in r.json()["content"]:
                if block["type"] == "text":
                    text += block["text"]
                elif block["type"] == "tool_use":
                    calls.append(ToolCall(id=block["id"], name=block["name"], args=block["input"]))
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise BridgeError(f"anthropic: unexpected response: {e!r}") from e
        return LLMResponse(text=text, tool_calls=calls)


register_adapter("anthropic", AnthropicAdapter)
