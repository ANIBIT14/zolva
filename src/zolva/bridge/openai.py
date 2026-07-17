"""OpenAI chat-completions adapter."""

from __future__ import annotations

import json
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


class OpenAIAdapter:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise BridgeError("OPENAI_API_KEY not set and no api_key given")
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {key}"},
            transport=transport,
            timeout=timeout,
        )

    def _wire_messages(self, system: str, messages: list[Message]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            item: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.role == "tool":
                item["tool_call_id"] = m.tool_call_id
            if m.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
                    }
                    for tc in m.tool_calls
                ]
            wire.append(item)
        return wire

    async def complete(
        self, *, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse:
        body: dict[str, Any] = {"model": model, "messages": self._wire_messages(system, messages)}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        r = await post_with_retry(
            self._client, "/chat/completions", json_body=body, provider="openai"
        )
        try:
            msg = r.json()["choices"][0]["message"]
            calls = [
                ToolCall(
                    id=tc["id"],
                    name=tc["function"]["name"],
                    args=json.loads(tc["function"]["arguments"]),
                )
                for tc in (msg.get("tool_calls") or [])
            ]
            text = msg.get("content") or ""
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise BridgeError(f"openai: unexpected response: {e!r}") from e
        return LLMResponse(text=text, tool_calls=calls)


register_adapter("openai", OpenAIAdapter)
