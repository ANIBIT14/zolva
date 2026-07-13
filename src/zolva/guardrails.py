"""Guardrails plugin: policy-as-config, enforced on the bus.

Three rule shapes (spec section "Guardrails"):
- structural: block_outside_window (contact-hours compliance)
- exact/regex: require_disclaimer
- LLM-judge (binary): refuse_topics, never

`never` violations hard-block; there is deliberately no config switch to
disable a rule at runtime — remove it from the policy file or it runs.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import yaml

from zolva.bridge import LLMAdapter, Message
from zolva.bus import Bus, Step, Verdict
from zolva.config import ConfigError

logger = logging.getLogger("zolva.guardrails")

_JUDGE_SYSTEM = (
    "You are a strict banking-compliance classifier. Answer with exactly one word: "
    "FAIL if the text violates the named policy, PASS otherwise."
)

_KNOWN_RULES = {"block_outside_window", "require_disclaimer", "refuse_topics", "never"}
_JUDGE_RULES = {"refuse_topics", "never"}


class Guardrails:
    def __init__(
        self,
        policy: dict[str, Any],
        *,
        agent: str,
        judge: LLMAdapter | None = None,
        judge_model: str = "",
        now: Callable[[ZoneInfo], datetime] | None = None,
    ) -> None:
        self._agent = agent
        self._pre: list[dict[str, Any]] = policy.get("pre") or []
        self._post: list[dict[str, Any]] = policy.get("post") or []
        self._judge = judge
        self._judge_model = judge_model
        self._now = now if now is not None else (lambda tz: datetime.now(tz))
        # validate at load time: a policy typo must fail startup, not crash a live run
        for section in (self._pre, self._post):
            for rule in section:
                for name, spec in rule.items():
                    if name not in _KNOWN_RULES:
                        raise ConfigError(f"unknown guardrail rule {name!r}")
                    if name in _JUDGE_RULES:
                        if judge is None:
                            raise ConfigError(f"guardrails: rule {name!r} requires a judge adapter")
                        if not isinstance(spec, list):
                            raise ConfigError(
                                f"guardrails: {name} must be a LIST of topics, got {spec!r}"
                            )
                    if name == "block_outside_window":
                        parts = str(spec["hours"]).split("-")
                        if len(parts) != 2 or not all(
                            re.fullmatch(r"\d{2}:\d{2}", p) for p in parts
                        ):
                            raise ConfigError(
                                "block_outside_window hours must be zero-padded "
                                f"'HH:MM-HH:MM', got {spec['hours']!r}"
                            )
                    if name == "require_disclaimer":
                        try:
                            re.compile(str(spec["when"]))
                        except re.error as e:
                            raise ConfigError(
                                f"require_disclaimer 'when' is an invalid regex: {e}"
                            ) from e

    @classmethod
    def from_file(cls, path: str | Path, **kwargs: Any) -> Guardrails:
        raw = yaml.safe_load(Path(path).read_text())
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: policy must be a mapping")
        return cls(raw, **kwargs)

    def attach(self, bus: Bus) -> None:
        bus.on(self._hook)

    async def _hook(self, step: Step) -> Verdict | None:
        if step.agent != self._agent:
            return None
        if step.type == "user_msg":
            return await self._check(self._pre, str(step.data.get("text", "")))
        if step.type == "response":
            return await self._check(self._post, str(step.data.get("text", "")))
        return None

    async def _check(self, rules: list[dict[str, Any]], text: str) -> Verdict | None:
        for rule in rules:
            for name, spec in rule.items():
                verdict = await self._apply(name, spec, text)
                if verdict is not None and not verdict.allow:
                    logger.warning(
                        "guardrail violation agent=%s reason=%s", self._agent, verdict.reason
                    )
                    return verdict
        return None

    async def _apply(self, name: str, spec: Any, text: str) -> Verdict | None:
        if name == "block_outside_window":
            # ponytail: assumes start < end (no overnight windows); zero-padded HH:MM compares fine
            start, end = str(spec["hours"]).split("-")
            now = self._now(ZoneInfo(str(spec["tz"]))).strftime("%H:%M")
            if not (start <= now <= end):
                return Verdict(allow=False, reason=f"outside contact window {spec['hours']}")
            return None
        if name == "require_disclaimer":
            if re.search(str(spec["when"]), text, re.IGNORECASE) and str(spec["text"]) not in text:
                return Verdict(allow=False, reason="required disclaimer missing")
            return None
        if name in ("refuse_topics", "never"):
            for topic in spec:
                if await self._judge_fails(str(topic), text):
                    prefix = "never-rule violation" if name == "never" else "refused topic"
                    return Verdict(allow=False, reason=f"{prefix}: {topic}")
            return None
        raise ConfigError(f"unknown guardrail rule {name!r}")

    async def _judge_fails(self, topic: str, text: str) -> bool:
        if self._judge is None:
            raise ConfigError("guardrails: topic rules require a judge adapter")
        resp = await self._judge.complete(
            model=self._judge_model,
            system=_JUDGE_SYSTEM,
            messages=[Message(role="user", content=f"Policy: {topic}\n\nText:\n{text}")],
            tools=[],
        )
        return resp.text.strip().upper().startswith("FAIL")
