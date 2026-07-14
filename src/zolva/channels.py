"""Channels plugin: the company's one CX endpoint, resolved per declared channel.

A ChannelAdapter turns a raw inbound payload (a WhatsApp webhook, an app-chat
POST) into an InboundMessage and delivers outbound replies back on the same
channel. ChannelHub is what the company wires its webhook handlers to: it
resolves the adapter, enforces the per-agent channel allowlist (mirrors the
tool allowlist: not declared = not reachable), namespaces the session per
channel so identities can never collide across channels, runs the agent, and
sends the reply. Both directions are emitted on the bus as `channel` steps so
audit and guardrails see customer contact itself, not just the conversation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml
from pydantic import BaseModel

from zolva.bus import Step
from zolva.config import ConfigError, resolve_refs
from zolva.orchestrator import BLOCKED_MESSAGE, AgentApp

logger = logging.getLogger("zolva.channels")


class ChannelError(Exception):
    """Inbound payload invalid, route not declared, or outbound delivery failed."""


class InboundMessage(BaseModel):
    session_id: str  # channel-native id (phone number hash, chat session, ...)
    text: str
    meta: dict[str, Any] = {}


class ChannelAdapter(ABC):
    @abstractmethod
    async def receive(self, raw: dict[str, Any]) -> InboundMessage: ...

    @abstractmethod
    async def send(self, session_id: str, text: str) -> None: ...


def _parse_inbound(raw: dict[str, Any]) -> InboundMessage:
    """Validate at the trust boundary; unknown keys ride along as meta."""
    if not isinstance(raw, dict):  # payload body is attacker-influenced; never trust the caller
        raise ChannelError("invalid inbound payload: expected a JSON object")
    try:
        session_id, text = str(raw["session_id"]), str(raw["text"])
    except KeyError as e:
        raise ChannelError(f"invalid inbound payload: missing {e.args[0]!r}") from None
    meta = {k: v for k, v in raw.items() if k not in {"session_id", "text"}}
    return InboundMessage(session_id=session_id, text=text, meta=meta)


class LogChannel(ChannelAdapter):
    """Dev default: replies go to the log, mirrors handover.LogBackend."""

    async def receive(self, raw: dict[str, Any]) -> InboundMessage:
        return _parse_inbound(raw)

    async def send(self, session_id: str, text: str) -> None:
        logger.info("CHANNEL-OUT session=%s text=%s", session_id, text)


class WebhookChannel(ChannelAdapter):
    """Generic HTTP gateway: outbound replies are HMAC-signed POSTs
    (timestamp inside the MAC, replay-resistant, same scheme as handover)."""

    def __init__(
        self, url: str, secret: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._url = url
        self._secret = secret.encode()
        self._client = httpx.AsyncClient(transport=transport, timeout=30.0)

    async def receive(self, raw: dict[str, Any]) -> InboundMessage:
        return _parse_inbound(raw)

    async def send(self, session_id: str, text: str) -> None:
        body = json.dumps({"session_id": session_id, "text": text}).encode()
        ts = str(int(time.time()))
        sig = hmac.new(self._secret, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        try:
            r = await self._client.post(
                self._url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Zolva-Signature": sig,
                    "X-Zolva-Timestamp": ts,
                },
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ChannelError(f"channel delivery failed: {e}") from e


class FakeChannel(ChannelAdapter):
    """Records sends for tests and offline development. Shipped on purpose."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def receive(self, raw: dict[str, Any]) -> InboundMessage:
        return _parse_inbound(raw)

    async def send(self, session_id: str, text: str) -> None:
        self.sent.append((session_id, text))


_ADAPTERS: dict[str, Callable[..., ChannelAdapter]] = {
    "log": LogChannel,
    "webhook": WebhookChannel,
    "fake": FakeChannel,
}


def _build_adapter(name: str, cfg: dict[str, Any]) -> ChannelAdapter:
    kind = cfg.pop("adapter", None)
    if kind == "elevenlabs":  # deferred: provider adapter imports core, not vice versa
        from zolva.channels_elevenlabs import ElevenLabsChannel

        cls: Callable[..., ChannelAdapter] | None = ElevenLabsChannel
    else:
        cls = _ADAPTERS.get(str(kind))
    if cls is None:
        raise ConfigError(f"channel {name!r}: unknown channel adapter {kind!r}")
    try:
        return cls(**cfg)
    except TypeError as e:
        raise ConfigError(f"channel {name!r}: {e}") from e


class ChannelHub:
    def __init__(
        self,
        app: AgentApp,
        *,
        channels: dict[str, ChannelAdapter],
        agents: dict[str, list[str]],
    ) -> None:
        self._app = app
        self._channels = channels
        self._agents = agents
        for agent, names in agents.items():
            for name in names:
                if name not in channels:
                    raise ConfigError(f"agent {agent!r} routed to unknown channel {name!r}")

    @classmethod
    def from_config(cls, path: str | Path, app: AgentApp) -> ChannelHub:
        """channels.yaml: `channels:` adapter configs, `agents:` per-agent allowlists.
        Secrets must be ${ENV:VAR} references; inline credentials are rejected."""
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"channels file not found: {p}")
        raw = yaml.safe_load(p.read_text())
        if not isinstance(raw, dict):
            raise ConfigError(f"{p}: top level must be a mapping")
        raw = resolve_refs(raw)
        channel_cfgs = raw.get("channels")
        agent_cfgs = raw.get("agents")
        if not isinstance(channel_cfgs, dict) or not isinstance(agent_cfgs, dict):
            raise ConfigError(f"{p}: 'channels' and 'agents' must be mappings")
        channels: dict[str, ChannelAdapter] = {}
        for name, cfg in channel_cfgs.items():
            if not isinstance(cfg, dict):
                raise ConfigError(f"{p}: channel {name!r} must be a mapping")
            channels[str(name)] = _build_adapter(str(name), dict(cfg))
        agents: dict[str, list[str]] = {}
        for agent, names in agent_cfgs.items():
            if not isinstance(names, list):
                raise ConfigError(f"{p}: agents.{agent} must be a list of channel names")
            agents[str(agent)] = [str(n) for n in names]
        return cls(app, channels=channels, agents=agents)

    async def dispatch(self, channel: str, agent: str, raw: dict[str, Any]) -> str:
        """One inbound customer payload in, one reply delivered on the same channel."""
        adapter = self._channels.get(channel)
        if adapter is None:
            raise ChannelError(f"unknown channel {channel!r}")
        if channel not in self._agents.get(agent, []):
            raise ChannelError(f"channel {channel!r} not allowed for agent {agent!r}")
        msg = await adapter.receive(raw)
        # channel-namespaced session id: a WhatsApp session can never address
        # a webchat session even if the channel-native ids collide
        session_id = f"{channel}:{msg.session_id}"
        verdict = await self._app.bus.emit(
            Step(
                type="channel",
                session_id=session_id,
                agent=agent,
                data={"channel": channel, "direction": "in", "text": msg.text},
            )
        )
        reply = (
            await self._app.run(agent, session_id, msg.text) if verdict.allow else BLOCKED_MESSAGE
        )
        verdict = await self._app.bus.emit(
            Step(
                type="channel",
                session_id=session_id,
                agent=agent,
                data={"channel": channel, "direction": "out", "text": reply},
            )
        )
        if not verdict.allow:
            reply = BLOCKED_MESSAGE
        await adapter.send(msg.session_id, reply)
        return reply
