"""Shared binary LLM-judge call. Fail-closed: any answer that isn't PASS is a fail."""

from __future__ import annotations

from zolva.bridge import LLMAdapter, Message


async def judge_result(
    adapter: LLMAdapter, *, model: str, system: str, content: str
) -> tuple[bool, str]:
    """Verdict plus the judge's raw output (kept as a debugging artifact).

    The verdict is read from the last non-empty line, so judge prompts may ask
    for reasoning before the answer; one-word PASS/FAIL judges parse the same.
    """
    resp = await adapter.complete(
        model=model,
        system=system,
        messages=[Message(role="user", content=content)],
        tools=[],
    )
    text = resp.text.strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    passed = bool(lines) and lines[-1].upper().startswith("PASS")
    return passed, text


async def judge_passes(adapter: LLMAdapter, *, model: str, system: str, content: str) -> bool:
    passed, _ = await judge_result(adapter, model=model, system=system, content=content)
    return passed
