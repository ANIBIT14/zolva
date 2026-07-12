"""Scripted adapter for tests and offline development. Shipped on purpose."""

from __future__ import annotations

from typing import Any

from zolva.bridge import BridgeError, LLMResponse, Message
from zolva.tools import ToolSpec


class FakeAdapter:
    def __init__(self, script: list[LLMResponse]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, *, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse:
        self.calls.append({"model": model, "system": system, "messages": messages, "tools": tools})
        if not self._script:
            raise BridgeError("FakeAdapter script exhausted")
        return self._script.pop(0)
