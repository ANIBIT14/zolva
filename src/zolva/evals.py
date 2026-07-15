"""Evals plugin: golden datasets per agent, CI-gated on the WORST cohort, not the average.

One YAML file per cohort. Four graders: exact, contains (regex), tool_called
(observed via the bus), judge (binary LLM-as-judge with a reference answer -
bias mitigation per the spec).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict

from zolva._judge import judge_passes
from zolva.bridge import LLMAdapter
from zolva.bus import Step, Verdict
from zolva.config import ConfigError, load_agents, load_yaml_dir
from zolva.orchestrator import AgentApp

_JUDGE_SYSTEM = (
    "You grade an assistant reply against an expectation. Answer with exactly one "
    "word: PASS if the reply satisfies the expectation, FAIL otherwise."
)


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: str
    expect: str = ""  # reference for exact/contains/judge
    expect_tool: str = ""  # for tool_called


class Cohort(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cohort: str
    agent: str
    grader: Literal["exact", "contains", "tool_called", "judge"]
    min_pass_rate: float = 0.95
    cases: list[EvalCase]


class CaseResult(BaseModel):
    input: str
    passed: bool
    response: str


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
        app.bus.on(self._observe)

    async def _observe(self, step: Step) -> Verdict | None:
        if step.type == "tool_call":
            self._tool_calls.setdefault(step.session_id, []).append(str(step.data.get("name")))
        return None

    async def run(self, evals_dir: str | Path) -> EvalReport:
        return await self.run_cohorts(load_cohorts(evals_dir))

    async def run_cohorts(self, cohorts: list[Cohort]) -> EvalReport:
        self._tool_calls.clear()  # stale observations from a prior run must not grade this one
        if any(c.grader == "judge" for c in cohorts) and self._judge is None:
            raise ConfigError("evals: judge grader requires a judge adapter")  # fail before running
        cohort_results = []
        for cohort in cohorts:
            results = []
            for i, case in enumerate(cohort.cases):
                session_id = f"eval-{cohort.cohort}-{i}-{uuid4().hex[:8]}"
                response = await self._app.run(cohort.agent, session_id, case.input)
                passed = await self._grade(cohort.grader, case, response, session_id)
                results.append(CaseResult(input=case.input, passed=passed, response=response))
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

    async def _grade(self, grader: str, case: EvalCase, response: str, session_id: str) -> bool:
        if grader == "exact":
            return response == case.expect
        if grader == "contains":
            try:
                return re.search(case.expect, response, re.IGNORECASE) is not None
            except re.error as e:
                raise ConfigError(f"invalid regex in contains case {case.input!r}: {e}") from e
        if grader == "tool_called":
            return case.expect_tool in self._tool_calls.get(session_id, [])
        # judge: binary with reference answer
        if self._judge is None:
            raise ConfigError("evals: judge grader requires a judge adapter")
        return await judge_passes(
            self._judge,
            model=self._judge_model,
            system=_JUDGE_SYSTEM,
            content=f"Expectation: {case.expect}\n\nAssistant reply:\n{response}",
        )


def report_to_json(report: EvalReport) -> dict[str, Any]:
    return report.model_dump()
