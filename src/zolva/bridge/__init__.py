"""Vendor-neutral LLM bridge: one Protocol, one adapter per provider."""

from __future__ import annotations

from importlib import import_module
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel

from zolva.tools import ToolSpec


class BridgeError(Exception):
    """LLM provider or adapter failure."""


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict[str, Any]


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = []


class LLMResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = []


class LLMAdapter(Protocol):
    async def complete(
        self, *, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse: ...


_ADAPTERS: dict[str, Callable[[], LLMAdapter]] = {}


def register_adapter(provider: str, factory: Callable[[], LLMAdapter]) -> None:
    _ADAPTERS[provider] = factory


def get_adapter(provider: str) -> LLMAdapter:
    if provider not in _ADAPTERS:
        # built-in adapters register on import; load zolva.bridge.<provider> lazily
        # so get_adapter("openai") works without a manual side-effect import
        try:
            import_module(f"zolva.bridge.{provider}")
        except ModuleNotFoundError:
            pass
    try:
        return _ADAPTERS[provider]()
    except KeyError:
        raise BridgeError(f"unknown provider {provider!r}") from None
