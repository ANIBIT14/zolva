"""Guardrails plugin: policy-as-config, enforced on the bus.

Four rule shapes (spec section "Guardrails"):
- structural: block_outside_window (contact hours), block_contact_frequency
  (per-customer contact caps across sessions, keyed on customer_ref)
- exact/regex: require_disclaimer
- LLM-judge (binary): refuse_topics, never
- tool calls (`tools:` section): require_approval, max_calls, max_amount
- tool results (`tool_output:` section): block_patterns, plus the judge rules,
  screening what a tool returns before the model ever reads it (prompt
  injection planted in CRM notes, emails, documents)

`never` violations hard-block; there is deliberately no config switch to
disable a rule at runtime, remove it from the policy file or it runs.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
_TOOL_RULES = {"require_approval", "max_calls", "max_amount"}
_TEXT_RULES = _KNOWN_RULES  # pre/post sections
_SECTIONS: dict[str, set[str]] = {
    "pre": _TEXT_RULES,
    "post": _TEXT_RULES,
    "tools": _TOOL_RULES,
    "tool_output": {"block_patterns"} | _JUDGE_RULES,
}


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
    unknown = set(policy) - set(_SECTIONS)
    if unknown:
        # fail closed: a typo'd section ("tool:") would silently disable its rules
        raise ConfigError(
            f"unknown policy section(s) {sorted(unknown)}; known: {sorted(_SECTIONS)}"
        )
    for section_name, allowed in _SECTIONS.items():
        for rule in policy.get(section_name) or []:
            for name, spec in rule.items():
                if name not in _KNOWN_RULES | _TOOL_RULES | {"block_patterns"}:
                    raise ConfigError(f"unknown guardrail rule {name!r}")
                if name not in allowed:
                    raise ConfigError(f"guardrail rule {name!r} not allowed in {section_name!r}")
                _validate_extra_rule(name, spec)
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


def _validate_extra_rule(name: str, spec: Any) -> None:
    """Shape-check the tool-call and tool-output rules."""
    if name == "require_approval" and not (
        isinstance(spec, list) and all(isinstance(t, str) for t in spec)
    ):
        raise ConfigError(f"require_approval must be a LIST of tool names, got {spec!r}")
    if name == "max_calls" and not (
        isinstance(spec, dict)
        and isinstance(spec.get("tool"), str)
        and isinstance(spec.get("per_session"), int)
        and spec["per_session"] >= 1
    ):
        raise ConfigError(f"max_calls needs {{tool, per_session >= 1}}, got {spec!r}")
    if name == "max_amount" and not (
        isinstance(spec, dict)
        and isinstance(spec.get("tool"), str)
        and isinstance(spec.get("field"), str)
        and _finite_number(spec.get("max")) is not None
    ):
        raise ConfigError(f"max_amount needs {{tool, field, max}}, got {spec!r}")
    if name == "block_patterns":
        if not isinstance(spec, list):
            raise ConfigError(f"block_patterns must be a LIST of regexes, got {spec!r}")
        for pattern in spec:
            try:
                re.compile(str(pattern))
            except re.error as e:
                raise ConfigError(f"block_patterns: invalid regex {pattern!r}: {e}") from e


def _finite_number(value: Any) -> float | None:
    """The value as a float, or None unless it is a real, finite number. bool
    is an int subclass and NaN compares False against everything; either
    would let an amount slip past a cap."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


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
        self._tools: list[dict[str, Any]] = policy.get("tools") or []
        self._tool_output: list[dict[str, Any]] = policy.get("tool_output") or []
        # ponytail: per-process counter; move to a shared ledger if one session
        # can hop app instances mid-conversation
        self._tool_counts: dict[tuple[str, str], int] = {}
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
        if step.type == "tool_call":
            return self._check_tool_call(step)
        if step.type == "tool_result":
            return await self._check(self._tool_output, str(step.data.get("content", "")), step)
        return None

    def _check_tool_call(self, step: Step) -> Verdict | None:
        tool = str(step.data.get("name", ""))
        args = step.data.get("args") or {}
        for rule in self._tools:
            for name, spec in rule.items():
                if name == "require_approval" and tool in spec:
                    # the human gets the exact args in the ticket trigger and acts on them
                    return self._violation(f"tool requires human approval: {tool}")
                if name == "max_amount" and spec["tool"] == tool:
                    raw = args.get(spec["field"]) if isinstance(args, dict) else None
                    # a missing/non-numeric/non-finite amount is not provably
                    # under the cap, so it fails closed
                    amount = _finite_number(raw)
                    if amount is None:
                        return self._violation(f"amount limit: {tool}.{spec['field']} missing")
                    if amount > float(spec["max"]):
                        return self._violation(
                            f"amount limit: {tool}.{spec['field']} {amount:g} > {spec['max']:g}"
                        )
        # counted last, so a call blocked above doesn't use up the budget. Keyed
        # on customer_ref when the caller supplies one: session ids come from
        # the channel payload, so rotating them must not reset the budget
        subject = str(step.data.get("customer_ref") or step.session_id)
        for rule in self._tools:
            spec = rule.get("max_calls")
            if spec is not None and spec["tool"] == tool:
                key = (subject, tool)
                if self._tool_counts.get(key, 0) >= spec["per_session"]:
                    return self._violation(
                        f"call limit: {tool} max {spec['per_session']} per session"
                    )
                self._tool_counts[key] = self._tool_counts.get(key, 0) + 1
        return None

    def _violation(self, reason: str) -> Verdict:
        logger.warning("guardrail violation agent=%s reason=%s", self._agent, reason)
        return Verdict(allow=False, reason=reason)

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
            # zero-padded HH:MM compares lexically; start > end means the window spans midnight
            start, end = str(spec["hours"]).split("-")
            now = self._now(ZoneInfo(str(spec["tz"]))).strftime("%H:%M")
            inside = start <= now <= end if start <= end else (now >= start or now <= end)
            if not inside:
                return Verdict(allow=False, reason=f"outside contact window {spec['hours']}")
            return None
        if name == "block_contact_frequency":
            return self._check_contact_frequency(spec, step)
        if name == "block_patterns":
            for pattern in spec:
                if re.search(str(pattern), text):
                    return Verdict(allow=False, reason=f"tool output blocked: {pattern}")
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
