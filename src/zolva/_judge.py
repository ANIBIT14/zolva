"""Shared binary LLM-judge call. Fail-closed: any answer that isn't PASS is a fail."""

from __future__ import annotations

from zolva.bridge import LLMAdapter, Message


async def judge_passes(adapter: LLMAdapter, *, model: str, system: str, content: str) -> bool:
    resp = await adapter.complete(
        model=model,
        system=system,
        messages=[Message(role="user", content=content)],
        tools=[],
    )
    return resp.text.strip().upper().startswith("PASS")
