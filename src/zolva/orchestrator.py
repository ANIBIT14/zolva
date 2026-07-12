"""The agent loop. Every observable step flows through the Bus so plugins can see or block it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zolva.bridge import LLMAdapter, Message, get_adapter
from zolva.bus import Bus, Step
from zolva.config import AgentConfig, load_agents
from zolva.handover import HandoverBackend, LogBackend, Ticket
from zolva.sessions import InMemorySessionStore, SessionStore
from zolva.tools import ToolContractError, ToolRegistry, ToolSpec, default_registry

BLOCKED_MESSAGE = "I can't help with that here — I've connected you with a human teammate."
MAX_TURNS = 10

_HANDOFF_SPEC_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {"type": "string", "description": "Target agent name or 'human-escalation'"},
        "reason": {"type": "string"},
    },
    "required": ["to", "reason"],
}


def _handoff_spec(cfg: AgentConfig) -> ToolSpec:
    return ToolSpec(
        name="handoff",
        description=f"Hand this conversation to one of: {', '.join(cfg.handoffs)}",
        parameters=_HANDOFF_SPEC_PARAMS,
    )


class AgentApp:
    def __init__(
        self,
        agents: dict[str, AgentConfig],
        *,
        registry: ToolRegistry | None = None,
        handover: HandoverBackend | None = None,
        sessions: SessionStore | None = None,
        bus: Bus | None = None,
        adapter: LLMAdapter | None = None,
    ) -> None:
        self._agents = agents
        self._registry = registry if registry is not None else default_registry
        self._handover = handover if handover is not None else LogBackend()
        self._sessions: SessionStore = sessions if sessions is not None else InMemorySessionStore()
        self.bus = bus if bus is not None else Bus()
        self._adapter = adapter

    @classmethod
    def from_config(cls, config_dir: str | Path, **kwargs: Any) -> AgentApp:
        return cls(load_agents(config_dir), **kwargs)

    def _adapter_for(self, cfg: AgentConfig) -> LLMAdapter:
        return self._adapter if self._adapter is not None else get_adapter(cfg.model.provider)

    async def run(self, agent_name: str, session_id: str, user_msg: str) -> str:
        cfg = self._agents[agent_name]
        verdict = await self.bus.emit(
            Step(type="user_msg", session_id=session_id, agent=cfg.name, data={"text": user_msg})
        )
        if not verdict.allow:
            return await self._escalate(cfg, session_id, verdict.reason or "blocked")
        await self._sessions.append(session_id, [Message(role="user", content=user_msg)])

        for _ in range(MAX_TURNS):
            history = await self._sessions.history(session_id)
            tools = self._registry.specs(cfg.tools)
            if cfg.handoffs:
                tools = [*tools, _handoff_spec(cfg)]
            response = await self._adapter_for(cfg).complete(
                model=cfg.model.name,
                system=cfg.instructions,
                messages=history,
                tools=tools,
            )
            if response.tool_calls:
                await self._sessions.append(
                    session_id,
                    [
                        Message(
                            role="assistant", content=response.text, tool_calls=response.tool_calls
                        )
                    ],
                )
                for tc in response.tool_calls:
                    if tc.name == "handoff":
                        target = str(tc.args.get("to", ""))
                        reason = str(tc.args.get("reason", ""))
                        if target == "human-escalation":
                            return await self._escalate(cfg, session_id, reason or "agent handoff")
                        if target in cfg.handoffs and target in self._agents:
                            await self._sessions.append(
                                session_id,
                                [
                                    Message(
                                        role="tool",
                                        content=f"handed off to {target}",
                                        tool_call_id=tc.id,
                                    )
                                ],
                            )
                            cfg = self._agents[target]
                            continue
                        await self._sessions.append(
                            session_id,
                            [
                                Message(
                                    role="tool",
                                    content=f"TOOL_ERROR: invalid handoff target {target!r}",
                                    tool_call_id=tc.id,
                                )
                            ],
                        )
                        continue
                    verdict = await self.bus.emit(
                        Step(
                            type="tool_call",
                            session_id=session_id,
                            agent=cfg.name,
                            data={"name": tc.name, "args": tc.args},
                        )
                    )
                    if not verdict.allow:
                        return await self._escalate(cfg, session_id, verdict.reason or "blocked")
                    try:
                        result = await self._registry.call(tc.name, tc.args)
                        content = json.dumps(result, default=str)
                    except ToolContractError as e:
                        content = f"TOOL_ERROR: {e}"  # fed back; model retries within MAX_TURNS
                    await self._sessions.append(
                        session_id, [Message(role="tool", content=content, tool_call_id=tc.id)]
                    )
                continue

            verdict = await self.bus.emit(
                Step(
                    type="response",
                    session_id=session_id,
                    agent=cfg.name,
                    data={"text": response.text},
                )
            )
            if not verdict.allow:
                return await self._escalate(cfg, session_id, verdict.reason or "blocked")
            await self._sessions.append(
                session_id, [Message(role="assistant", content=response.text)]
            )
            return response.text

        return await self._escalate(cfg, session_id, "max turns exceeded")

    async def _escalate(self, cfg: AgentConfig, session_id: str, reason: str) -> str:
        ticket = Ticket(
            session_id=session_id,
            agent=cfg.name,
            reason=reason,
            transcript=await self._sessions.history(session_id),
        )
        await self.bus.emit(
            Step(type="handover", session_id=session_id, agent=cfg.name, data={"reason": reason})
        )
        await self._handover.escalate(ticket)
        return BLOCKED_MESSAGE
