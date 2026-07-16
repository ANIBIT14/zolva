"""The agent loop. Every observable step flows through the Bus so plugins can see or block it."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from zolva.bridge import BridgeError, LLMAdapter, Message, ToolCall, get_adapter
from zolva.bus import Bus, Step
from zolva.config import AgentConfig, ConfigError, load_agents
from zolva.handover import HandoverBackend, LogBackend, Ticket
from zolva.redaction import RedactingAdapter, Redactor, load_redactor
from zolva.sessions import InMemorySessionStore, SessionStore
from zolva.tools import ToolContractError, ToolRegistry, ToolSpec, default_registry

logger = logging.getLogger("zolva.orchestrator")

BLOCKED_MESSAGE = "I can't help with that here, I've connected you with a human teammate."
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
        redactor: Redactor | None = None,
    ) -> None:
        self._agents = agents
        self._registry = registry if registry is not None else default_registry
        for cfg in agents.values():
            # fail fast: a declared-but-unregistered tool must break startup,
            # not raise out of run() mid-conversation
            try:
                self._registry.specs(cfg.tools)
            except ToolContractError as e:
                raise ConfigError(f"agent {cfg.name!r}: {e}") from e
        self._handover = handover if handover is not None else LogBackend()
        self._sessions: SessionStore = sessions if sessions is not None else InMemorySessionStore()
        self.bus = bus if bus is not None else Bus()
        self._redactor = redactor
        # injected adapters (tests, custom gateways) get the same masking
        if adapter is not None and redactor is not None:
            adapter = RedactingAdapter(adapter, redactor)
        self._adapter = adapter
        self._provider_adapters: dict[tuple[str, str | None, float], LLMAdapter] = {}

    @classmethod
    def from_config(
        cls,
        config_dir: str | Path,
        *,
        judge: LLMAdapter | None = None,
        judge_model: str = "",
        redaction: str | None = None,
        **kwargs: Any,
    ) -> AgentApp:
        """Build the app from a config dir; agents with a `guardrails:` policy
        get it attached automatically (paths resolve relative to config_dir).
        `redaction` names a PII-pattern file (relative to config_dir) masked
        into every provider call."""
        agents = load_agents(config_dir)
        if redaction is not None:
            kwargs["redactor"] = load_redactor(config_dir, redaction)
        app = cls(agents, **kwargs)
        from zolva.guardrails import Guardrails  # deferred: plugin import inside core factory

        for cfg in agents.values():
            if cfg.guardrails:
                policy_path = Path(config_dir) / cfg.guardrails
                if not policy_path.is_file():
                    raise ConfigError(f"agent {cfg.name!r}: policy file not found: {policy_path}")
                Guardrails.from_file(
                    policy_path, agent=cfg.name, judge=judge, judge_model=judge_model
                ).attach(app.bus)
            if cfg.evals:
                evals_path = Path(config_dir) / cfg.evals
                if not (evals_path.is_file() or evals_path.is_dir()):
                    raise ConfigError(f"agent {cfg.name!r}: evals path not found: {evals_path}")
        return app

    @property
    def sessions(self) -> SessionStore:
        return self._sessions

    def _adapter_for(self, cfg: AgentConfig) -> LLMAdapter:
        if self._adapter is not None:
            return self._adapter
        m = cfg.model
        key = (m.provider, m.base_url, m.timeout)
        if key not in self._provider_adapters:
            # one adapter (one httpx client/connection pool) per provider+gateway;
            # wrapped once here so redaction applies to every provider call.
            # only overrides are forwarded, so zero-arg custom factories keep working
            overrides: dict[str, Any] = {}
            if m.base_url is not None:
                overrides["base_url"] = m.base_url
            if m.timeout != 60.0:
                overrides["timeout"] = m.timeout
            adapter = get_adapter(m.provider, **overrides)
            if self._redactor is not None:
                adapter = RedactingAdapter(adapter, self._redactor)
            self._provider_adapters[key] = adapter
        return self._provider_adapters[key]

    async def run(self, agent_name: str, session_id: str, user_msg: str) -> str:
        try:
            cfg = self._agents[agent_name]
        except KeyError:
            raise ConfigError(f"unknown agent {agent_name!r}") from None
        verdict = await self.bus.emit(
            Step(type="user_msg", session_id=session_id, agent=cfg.name, data={"text": user_msg})
        )
        if not verdict.allow:
            return await self._escalate(
                cfg, session_id, verdict.reason or "blocked", trigger=user_msg
            )
        await self._sessions.append(session_id, [Message(role="user", content=user_msg)])

        for _ in range(MAX_TURNS):
            history = await self._sessions.history(session_id)
            tools = self._registry.specs(cfg.tools)
            if cfg.handoffs:
                tools = [*tools, _handoff_spec(cfg)]
            verdict = await self.bus.emit(
                Step(
                    type="model_call",
                    session_id=session_id,
                    agent=cfg.name,
                    data={"provider": cfg.model.provider, "model": cfg.model.name},
                )
            )
            if not verdict.allow:
                return await self._escalate(cfg, session_id, verdict.reason or "blocked")
            try:
                response = await self._adapter_for(cfg).complete(
                    model=cfg.model.name,
                    system=cfg.instructions,
                    messages=history,
                    tools=tools,
                )
            except BridgeError as e:
                # degrade to handover, never to silence
                return await self._escalate(cfg, session_id, f"provider error: {e}")
            if response.tool_calls:
                await self._sessions.append(
                    session_id,
                    [
                        Message(
                            role="assistant", content=response.text, tool_calls=response.tool_calls
                        )
                    ],
                )
                batch_agent = cfg.name  # bus attribution stays with the agent that made the calls
                batch_allowed = set(
                    cfg.tools
                )  # the batch belongs to the agent that requested it, even across a mid-batch handoff
                for i, tc in enumerate(response.tool_calls):
                    verdict = await self.bus.emit(
                        Step(
                            type="tool_call",
                            session_id=session_id,
                            agent=batch_agent,
                            data={"name": tc.name, "args": tc.args},
                        )
                    )
                    if not verdict.allow:
                        await self._close_pending(session_id, response.tool_calls[i:])
                        return await self._escalate(
                            cfg,
                            session_id,
                            verdict.reason or "blocked",
                            trigger=json.dumps({"tool": tc.name, "args": tc.args}, default=str),
                        )
                    if tc.name == "handoff":
                        target = str(tc.args.get("to", ""))
                        reason = str(tc.args.get("reason", ""))
                        if target == "human-escalation":
                            # close pending tool_calls or the session history is
                            # permanently rejected by providers on the next turn
                            await self._close_pending(session_id, response.tool_calls[i:])
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
                    try:
                        if tc.name not in batch_allowed:
                            # undeclared == unknown to this agent; same error as an
                            # unregistered tool so nothing leaks about other agents' tools
                            raise ToolContractError(f"unknown tool {tc.name!r}")
                        result = await self._registry.call(tc.name, tc.args)
                        if isinstance(result, BaseModel):
                            content = result.model_dump_json()
                        else:
                            content = json.dumps(result, default=str)
                    except ToolContractError as e:
                        content = f"TOOL_ERROR: {e}"  # fed back; model retries within MAX_TURNS
                    except Exception as e:  # bank tool crashed: handover, never silence
                        await self._close_pending(session_id, response.tool_calls[i:])
                        return await self._escalate(
                            cfg,
                            session_id,
                            f"tool error: {tc.name}: {e}",
                            trigger=json.dumps({"tool": tc.name, "args": tc.args}, default=str),
                        )
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
                return await self._escalate(
                    cfg, session_id, verdict.reason or "blocked", trigger=response.text
                )
            await self._sessions.append(
                session_id, [Message(role="assistant", content=response.text)]
            )
            return response.text

        return await self._escalate(cfg, session_id, "max turns exceeded")

    async def _close_pending(self, session_id: str, pending: list[ToolCall]) -> None:
        """Answer unresolved tool_calls so the session history stays provider-valid."""
        await self._sessions.append(
            session_id,
            [
                Message(role="tool", content="escalated to human", tool_call_id=tc.id)
                for tc in pending
            ],
        )

    async def _escalate(
        self, cfg: AgentConfig, session_id: str, reason: str, trigger: str = ""
    ) -> str:
        ticket = Ticket(
            session_id=session_id,
            agent=cfg.name,
            reason=reason,
            transcript=await self._sessions.history(session_id),
            trigger=trigger,  # blocked content never reaches the session; humans still see it
        )
        await self.bus.emit(
            Step(type="handover", session_id=session_id, agent=cfg.name, data={"reason": reason})
        )
        try:
            await self._handover.escalate(ticket)
        except Exception:
            # a down ticketing backend must not silence the customer; the step
            # above is already in the audit log, so the escalation is on record
            logger.critical(
                "handover delivery failed session=%s agent=%s reason=%s",
                session_id,
                cfg.name,
                reason,
                exc_info=True,
            )
        return BLOCKED_MESSAGE
