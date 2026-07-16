"""Guardrails plugin: policy-as-config, enforced on the bus.

Four rule shapes (spec section "Guardrails"):
- structural: block_outside_window (contact hours), block_contact_frequency
  (per-customer contact caps across sessions, keyed on customer_ref)
- exact/regex: require_disclaimer
- LLM-judge (binary): refuse_topics, never

`never` violations hard-block; there is deliberately no config switch to
disable a rule at runtime, remove it from the policy file or it runs.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import yaml

from zolva._db import sqlite_conn
from zolva._judge import judge_passes
from zolva.bridge import LLMAdapter
from zolva.bus import Bus, Step, Verdict
from zolva.config import ConfigError

logger = logging.getLogger("zolva.guardrails")

_JUDGE_SYSTEM = (
    "You are a strict banking-compliance classifier. Answer with exactly one word: "
    "PASS if the text complies with the named policy, FAIL if it violates it."
)

_KNOWN_RULES = {
    "block_outside_window",
    "block_contact_frequency",
    "require_disclaimer",
    "refuse_topics",
    "never",
}
_JUDGE_RULES = {"refuse_topics", "never"}


def _load_policy_file(path: str | Path) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: policy must be a mapping")
    return raw


def validate_policy_file(path: str | Path) -> None:
    """Shape-check a policy file without constructing adapters (CI validate)."""
    validate_policy(_load_policy_file(path))


def validate_policy(policy: dict[str, Any], *, judge_available: bool = True) -> None:
    """Shape-check a policy mapping; raises ConfigError on the first problem.

    `judge_available=True` skips the judge-adapter requirement so `zolva
    validate` can check shapes without constructing adapters; Guardrails
    passes the real availability at attach time."""
    for section in (policy.get("pre") or [], policy.get("post") or []):
        for rule in section:
            for name, spec in rule.items():
                if name not in _KNOWN_RULES:
                    raise ConfigError(f"unknown guardrail rule {name!r}")
                if name in _JUDGE_RULES:
                    if not judge_available:
                        raise ConfigError(f"guardrails: rule {name!r} requires a judge adapter")
                    if not isinstance(spec, list):
                        raise ConfigError(
                            f"guardrails: {name} must be a LIST of topics, got {spec!r}"
                        )
                if name == "block_outside_window":
                    if not isinstance(spec, dict) or "hours" not in spec or "tz" not in spec:
                        raise ConfigError(f"block_outside_window needs {{hours, tz}}, got {spec!r}")
                    parts = str(spec["hours"]).split("-")
                    if len(parts) != 2 or not all(re.fullmatch(r"\d{2}:\d{2}", p) for p in parts):
                        raise ConfigError(
                            "block_outside_window hours must be zero-padded "
                            f"'HH:MM-HH:MM', got {spec['hours']!r}"
                        )
                if name == "block_contact_frequency":
                    if (
                        not isinstance(spec, dict)
                        or not isinstance(spec.get("max_contacts"), int)
                        or spec["max_contacts"] < 1
                        or not isinstance(spec.get("window_hours"), (int, float))
                        or spec["window_hours"] <= 0
                        or not isinstance(spec.get("ledger"), str)
                    ):
                        raise ConfigError(
                            "block_contact_frequency needs "
                            f"{{max_contacts >= 1, window_hours > 0, ledger}}, got {spec!r}"
                        )
                if name == "require_disclaimer":
                    if not isinstance(spec, dict) or "when" not in spec or "text" not in spec:
                        raise ConfigError(f"require_disclaimer needs {{when, text}}, got {spec!r}")
                    try:
                        re.compile(str(spec["when"]))
                    except re.error as e:
                        raise ConfigError(
                            f"require_disclaimer 'when' is an invalid regex: {e}"
                        ) from e


class Guardrails:
    def __init__(
        self,
        policy: dict[str, Any],
        *,
        agent: str,
        judge: LLMAdapter | None = None,
        judge_model: str = "",
        now: Callable[[ZoneInfo], datetime] | None = None,
        base_dir: str | Path | None = None,
    ) -> None:
        self._agent = agent
        self._pre: list[dict[str, Any]] = policy.get("pre") or []
        self._post: list[dict[str, Any]] = policy.get("post") or []
        self._judge = judge
        self._judge_model = judge_model
        self._now = now if now is not None else (lambda tz: datetime.now(tz))
        self._base_dir = Path(base_dir) if base_dir is not None else Path(".")
        # validate at load time: a policy typo must fail startup, not crash a live run
        validate_policy(policy, judge_available=judge is not None)

    @classmethod
    def from_file(cls, path: str | Path, **kwargs: Any) -> Guardrails:
        # ledger paths in the policy resolve relative to the policy file
        kwargs.setdefault("base_dir", Path(path).parent)
        return cls(_load_policy_file(path), **kwargs)

    def attach(self, bus: Bus) -> None:
        bus.on(self._hook)

    async def _hook(self, step: Step) -> Verdict | None:
        if step.agent != self._agent:
            return None
        if step.type == "user_msg":
            return await self._check(self._pre, str(step.data.get("text", "")), step)
        if step.type == "response":
            return await self._check(self._post, str(step.data.get("text", "")), step)
        return None

    async def _check(self, rules: list[dict[str, Any]], text: str, step: Step) -> Verdict | None:
        for rule in rules:
            for name, spec in rule.items():
                verdict = await self._apply(name, spec, text, step)
                if verdict is not None and not verdict.allow:
                    logger.warning(
                        "guardrail violation agent=%s reason=%s", self._agent, verdict.reason
                    )
                    return verdict
        return None

    async def _apply(self, name: str, spec: Any, text: str, step: Step) -> Verdict | None:
        if name == "block_outside_window":
            # ponytail: assumes start < end (no overnight windows); zero-padded HH:MM compares fine
            start, end = str(spec["hours"]).split("-")
            now = self._now(ZoneInfo(str(spec["tz"]))).strftime("%H:%M")
            if not (start <= now <= end):
                return Verdict(allow=False, reason=f"outside contact window {spec['hours']}")
            return None
        if name == "block_contact_frequency":
            return self._check_contact_frequency(spec, step)
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

    def _check_contact_frequency(self, spec: dict[str, Any], step: Step) -> Verdict | None:
        """Per-customer contact cap across sessions and channels.

        Counts allowed agent responses per customer_ref in a rolling window,
        recorded in a small sqlite ledger next to the policy file. Steps
        without a customer_ref are skipped, the rule can only govern traffic
        that identifies the customer."""
        ref = step.data.get("customer_ref")
        if not ref:
            return None
        ledger = Path(str(spec["ledger"]))
        if not ledger.is_absolute():
            ledger = self._base_dir / ledger
        max_contacts = int(spec["max_contacts"])
        window_hours = float(spec["window_hours"])
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=window_hours)).isoformat()
        with sqlite_conn(str(ledger), immediate=True) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS contacts (customer_ref TEXT NOT NULL, ts TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_contacts_ref ON contacts(customer_ref, ts)"
            )
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE customer_ref = ? AND ts >= ?",
                (str(ref), cutoff),
            ).fetchone()
            if count >= max_contacts:
                return Verdict(
                    allow=False,
                    reason=(
                        f"contact frequency cap: {max_contacts} contacts per "
                        f"{window_hours:g}h reached for this customer"
                    ),
                )
            conn.execute("INSERT INTO contacts VALUES (?, ?)", (str(ref), now.isoformat()))
        return None

    async def _judge_fails(self, topic: str, text: str) -> bool:
        if self._judge is None:
            raise ConfigError("guardrails: topic rules require a judge adapter")
        # fail-closed: anything that isn't an explicit PASS is a violation
        return not await judge_passes(
            self._judge,
            model=self._judge_model,
            system=_JUDGE_SYSTEM,
            content=f"Policy: {topic}\n\nText:\n{text}",
        )
