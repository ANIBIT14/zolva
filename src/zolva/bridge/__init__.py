"""Vendor-neutral LLM bridge: one Protocol, one adapter per provider."""

from __future__ import annotations

import asyncio
from importlib import import_module
from typing import Any, Callable, Literal, Protocol

import httpx
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


_ADAPTERS: dict[str, Callable[..., LLMAdapter]] = {}


def register_adapter(provider: str, factory: Callable[..., LLMAdapter]) -> None:
    _ADAPTERS[provider] = factory


def get_adapter(provider: str, **kwargs: Any) -> LLMAdapter:
    """kwargs (e.g. base_url, timeout) forward to the adapter factory;
    None values are dropped so factories keep their own defaults."""
    if provider not in _ADAPTERS:
        # built-in adapters register on import; load zolva.bridge.<provider> lazily
        # so get_adapter("openai") works without a manual side-effect import
        try:
            import_module(f"zolva.bridge.{provider}")
        except ModuleNotFoundError:
            pass
    try:
        factory = _ADAPTERS[provider]
    except KeyError:
        raise BridgeError(f"unknown provider {provider!r}") from None
    return factory(**{k: v for k, v in kwargs.items() if v is not None})


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRY_AFTER = 15.0
_MAX_BACKOFF = 8.0


async def post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    json_body: dict[str, Any],
    provider: str,
    attempts: int = 3,
) -> httpx.Response:
    """POST with bounded exponential backoff on retryable failures.

    Retries 429/5xx and transport errors; honors a numeric Retry-After header
    (capped). Other HTTP errors raise BridgeError immediately, one throttled
    request must never become a customer-facing escalation."""
    last_error = ""
    for attempt in range(attempts):
        delay = min(0.5 * 2**attempt, _MAX_BACKOFF)
        try:
            r = await client.post(url, json=json_body)
        except httpx.TransportError as e:
            last_error = str(e)
        else:
            if r.status_code not in _RETRYABLE_STATUS:
                try:
                    r.raise_for_status()
                except httpx.HTTPStatusError as e:
                    raise BridgeError(f"{provider}: {e}") from e
                return r
            last_error = f"HTTP {r.status_code}"
            retry_after = r.headers.get("Retry-After", "")
            if retry_after.replace(".", "", 1).isdigit():
                delay = min(float(retry_after), _MAX_RETRY_AFTER)
        if attempt < attempts - 1:
            await asyncio.sleep(delay)
    raise BridgeError(f"{provider}: {last_error} after {attempts} attempts")
