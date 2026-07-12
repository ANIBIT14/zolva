"""Middleware bus: every orchestrator step flows through here. Plugins attach as hooks."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel

StepType = Literal["user_msg", "model_call", "tool_call", "response", "handover", "feedback"]


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
            verdict = await hook(step)
            if verdict is not None and not verdict.allow:
                return verdict
        return Verdict()
