"""Evals plugin: golden datasets per agent, CI-gated on the WORST cohort, not the average.

One YAML file per cohort. Five graders: exact, contains (regex), tool_called
(observed via the bus), handoff (session escalated to a human, observed via the
bus), judge (binary LLM-as-judge with a reference answer - bias mitigation per
the spec). A case's `input` may be a list of turns: they run in one session and
the final reply is graded, so recovery and escalation across a conversation are
evaluable, not just single turns.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from zolva._judge import judge_result
from zolva.bridge import LLMAdapter
from zolva.bus import Step, Verdict
from zolva.config import ConfigError, load_agents, load_yaml_dir
from zolva.orchestrator import AgentApp

logger = logging.getLogger("zolva.evals")

# Strict is the default: in banking, a missed bad reply costs more than a
# re-reviewed good one. Reasoning-before-verdict keeps scores consistent and
# leaves an auditable trail in the report.
_JUDGE_SYSTEM_STRICT = (
    "You grade an assistant reply against an expectation, given the assistant's "
    "system prompt and the conversation. Be strict: if there is ANY doubt that "
    "the final reply fully satisfies the expectation, it fails. Answer with one "
    "line of reasoning, then exactly PASS or FAIL alone on the final line."
)
_JUDGE_SYSTEM_LENIENT = (
    "You grade an assistant reply against an expectation, given the assistant's "
    "system prompt and the conversation. Be lenient: minor imperfections are "
    "acceptable when the final reply substantially satisfies the expectation. "
    "Answer with one line of reasoning, then exactly PASS or FAIL alone on the "
    "final line."
)


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: str | list[str]  # a list is a multi-turn conversation in one session
    expect: str = ""  # reference for exact/contains/judge
    expect_tool: str = ""  # for tool_called

    @field_validator("input")
    @classmethod
    def _at_least_one_turn(cls, v: str | list[str]) -> str | list[str]:
        if isinstance(v, list) and not v:
            raise ValueError("input: at least one turn required")
        return v

    @property
    def turns(self) -> list[str]:
        return [self.input] if isinstance(self.input, str) else self.input


class Cohort(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cohort: str
    agent: str
    grader: Literal["exact", "contains", "tool_called", "handoff", "judge"]
    strictness: Literal["strict", "lenient"] = "strict"  # judge grader only
    min_pass_rate: float = 0.95
    cases: list[EvalCase]


class CaseResult(BaseModel):
    input: str | list[str]
    passed: bool
    response: str
    judge_output: str = ""  # judge grader: reasoning + verdict, for debugging


class CohortResult(BaseModel):
    cohort: str
    pass_rate: float
    min_pass_rate: float
    results: list[CaseResult]

    @property
    def passed(self) -> bool:
        return self.pass_rate >= self.min_pass_rate


class EvalReport(BaseModel):
    cohorts: list[CohortResult]

    @property
    def gate_passed(self) -> bool:
        """Worst cohort decides, a great average never rescues a failing cohort."""
        return all(c.passed for c in self.cohorts)

    def summary(self) -> str:
        lines = [
            f"{c.cohort:24s} {c.pass_rate:6.1%}  (gate {c.min_pass_rate:.0%})  "
            f"{'PASS' if c.passed else 'FAIL'}"
            for c in self.cohorts
        ]
        lines.append(f"GATE: {'PASS' if self.gate_passed else 'FAIL (worst cohort)'}")
        return "\n".join(lines)


def load_cohorts(evals_dir: str | Path) -> list[Cohort]:
    cohorts = []
    for path, raw in load_yaml_dir(evals_dir, "cohort"):
        try:
            cohorts.append(Cohort(**raw))
        except Exception as e:
            raise ConfigError(f"{path}: {e}") from e
    return cohorts


def load_cohorts_from_agents(config_dir: str | Path, *, required: bool = True) -> list[Cohort]:
    """Collect every cohort declared via `evals:` in agent YAML (paths relative
    to config_dir; a file loads one cohort, a directory loads all).
    `required=False` is the CI-validate path: parse and cross-check whatever is
    declared, but an agent set with no evals is not an error there."""
    cohorts: list[Cohort] = []
    for cfg in load_agents(config_dir).values():
        if not cfg.evals:
            continue
        p = Path(config_dir) / cfg.evals
        if p.is_dir():
            declared = load_cohorts(p)
        else:
            if not p.is_file():
                raise ConfigError(f"agent {cfg.name!r}: evals path not found: {p}")
            raw = yaml.safe_load(p.read_text())
            if not isinstance(raw, dict):
                raise ConfigError(f"{p}: top level must be a mapping")
            try:
                declared = [Cohort(**raw)]
            except Exception as e:
                raise ConfigError(f"{p}: {e}") from e
        for cohort in declared:
            if cohort.agent != cfg.name:
                raise ConfigError(
                    f"{cfg.evals}: cohort {cohort.cohort!r} is for agent "
                    f"{cohort.agent!r}, declared by {cfg.name!r}"
                )
        cohorts.extend(declared)
    if not cohorts and required:
        # a vacuously green --gate is worse than a loud failure
        raise ConfigError(f"no evals: declared by any agent in {config_dir}")
    return cohorts


class EvalRunner:
    def __init__(
        self, app: AgentApp, *, judge: LLMAdapter | None = None, judge_model: str = ""
    ) -> None:
        self._app = app
        self._judge = judge
        self._judge_model = judge_model
        self._tool_calls: dict[str, list[str]] = {}
        self._handovers: set[str] = set()
        app.bus.on(self._observe)

    async def _observe(self, step: Step) -> Verdict | None:
        if step.type == "tool_call":
            self._tool_calls.setdefault(step.session_id, []).append(str(step.data.get("name")))
        elif step.type == "handover":
            self._handovers.add(step.session_id)
        return None

    async def run(self, evals_dir: str | Path) -> EvalReport:
        return await self.run_cohorts(load_cohorts(evals_dir))

    async def run_cohorts(self, cohorts: list[Cohort]) -> EvalReport:
        # stale observations from a prior run must not grade this one
        self._tool_calls.clear()
        self._handovers.clear()
        if any(c.grader == "judge" for c in cohorts) and self._judge is None:
            raise ConfigError("evals: judge grader requires a judge adapter")  # fail before running
        cohort_results = []
        for cohort in cohorts:
            if cohort.grader == "judge":
                # ponytail: name-equality heuristic; provider-aware family check if it misses
                if (
                    self._judge_model
                    and self._judge_model == self._app.agent_config(cohort.agent).model.name
                ):
                    logger.warning(
                        "cohort %r: judge model %r is the agent's own model; use a "
                        "different model family so judge and agent blind spots stay "
                        "independent",
                        cohort.cohort,
                        self._judge_model,
                    )
            results = []
            for i, case in enumerate(cohort.cases):
                session_id = f"eval-{cohort.cohort}-{i}-{uuid4().hex[:8]}"
                transcript: list[tuple[str, str]] = []
                response = ""
                for turn in case.turns:
                    response = await self._app.run(cohort.agent, session_id, turn)
                    transcript.append((turn, response))
                passed, judge_output = await self._grade(cohort, case, transcript, session_id)
                results.append(
                    CaseResult(
                        input=case.input,
                        passed=passed,
                        response=response,
                        judge_output=judge_output,
                    )
                )
            rate = sum(r.passed for r in results) / len(results) if results else 0.0
            cohort_results.append(
                CohortResult(
                    cohort=cohort.cohort,
                    pass_rate=rate,
                    min_pass_rate=cohort.min_pass_rate,
                    results=results,
                )
            )
        return EvalReport(cohorts=cohort_results)

    async def _grade(
        self,
        cohort: Cohort,
        case: EvalCase,
        transcript: list[tuple[str, str]],
        session_id: str,
    ) -> tuple[bool, str]:
        """(passed, judge_output). Non-judge graders score the final reply;
        tool_called/handoff observe the whole session."""
        response = transcript[-1][1]
        if cohort.grader == "exact":
            return response == case.expect, ""
        if cohort.grader == "contains":
            try:
                return re.search(case.expect, response, re.IGNORECASE) is not None, ""
            except re.error as e:
                raise ConfigError(f"invalid regex in contains case {case.input!r}: {e}") from e
        if cohort.grader == "tool_called":
            return case.expect_tool in self._tool_calls.get(session_id, []), ""
        if cohort.grader == "handoff":
            return session_id in self._handovers, ""
        # judge: binary with reference answer, given the full context the agent
        # had - system prompt and conversation - so it grades the answer, not
        # how authoritative the reply sounds
        if self._judge is None:
            raise ConfigError("evals: judge grader requires a judge adapter")
        cfg = self._app.agent_config(cohort.agent)
        convo = "\n\n".join(f"User: {u}\nAssistant: {a}" for u, a in transcript)
        system = _JUDGE_SYSTEM_STRICT if cohort.strictness == "strict" else _JUDGE_SYSTEM_LENIENT
        return await judge_result(
            self._judge,
            model=self._judge_model,
            system=system,
            content=(
                f"Assistant system prompt:\n{cfg.instructions}\n\n"
                f"Conversation:\n{convo}\n\n"
                f"Expectation: {case.expect}"
            ),
        )


def report_to_json(report: EvalReport) -> dict[str, Any]:
    return report.model_dump()
