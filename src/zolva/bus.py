"""Middleware bus: every orchestrator step flows through here. Plugins attach as hooks."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel

logger = logging.getLogger("zolva.bus")

StepType = Literal[
    "user_msg", "model_call", "tool_call", "response", "handover", "feedback", "channel"
]


class Step(BaseModel):
    type: StepType
    session_id: str
    agent: str
    data: dict[str, Any]


class Verdict(BaseModel):
    allow: bool = True
    reason: str | None = None


Hook = Callable[[Step], Awaitable[Verdict | None]]


class Bus:
    def __init__(self) -> None:
        self._hooks: list[Hook] = []

    def on(self, hook: Hook) -> None:
        self._hooks.append(hook)

    async def emit(self, step: Step) -> Verdict:
        for hook in self._hooks:
            try:
                verdict = await hook(step)
            except Exception as e:
                # fail closed, never crash the conversation: a broken safety
                # hook (audit disk full, guardrail bug) blocks the step, and
                # the orchestrator's block path degrades to human handover
                logger.exception(
                    "bus hook failed on %s step (session=%s)", step.type, step.session_id
                )
                return Verdict(allow=False, reason=f"safety hook failure: {e}")
            if verdict is not None and not verdict.allow:
                return verdict
        return Verdict()
