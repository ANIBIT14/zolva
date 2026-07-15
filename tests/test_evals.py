from pathlib import Path

import pytest

from tests.test_orchestrator import make_cfg, make_registry
from zolva.bridge import LLMResponse, ToolCall
from zolva.bridge.fake import FakeAdapter
from zolva.config import ConfigError
from zolva.evals import EvalRunner, load_cohorts, load_cohorts_from_agents
from zolva.orchestrator import AgentApp

AGENT = "collections-agent"


def write_cohort(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def make_app(script: list[LLMResponse]) -> AgentApp:
    return AgentApp(
        {AGENT: make_cfg()}, registry=make_registry(), adapter=FakeAdapter(script=script)
    )


def write_agent_with_evals(root: Path, evals_rel: str) -> Path:
    agents = root / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "cx.md").write_text("Collect politely.")
    (agents / "cx.yaml").write_text(
        f"name: {AGENT}\ninstructions: cx.md\n"
        "model: { provider: test, name: m }\n"
        f"evals: {evals_rel}\n"
    )
    return agents


async def test_contains_grader_and_gate_pass(tmp_path: Path) -> None:
    write_cohort(
        tmp_path / "evals" / "dues.yaml",
        f"""
cohort: dues
agent: {AGENT}
grader: contains
min_pass_rate: 1.0
cases:
  - {{ input: "what do I owe?", expect: "4200" }}
""",
    )
    runner = EvalRunner(make_app([LLMResponse(text="You owe 4200.")]))
    report = await runner.run(tmp_path / "evals")
    assert report.gate_passed and report.cohorts[0].pass_rate == 1.0


async def test_rerun_does_not_inherit_prior_session_history(tmp_path: Path) -> None:
    write_cohort(
        tmp_path / "evals" / "dues.yaml",
        f"""
cohort: dues
agent: {AGENT}
grader: contains
min_pass_rate: 1.0
cases:
  - {{ input: "what do I owe?", expect: "a" }}
""",
    )
    adapter = FakeAdapter(script=[LLMResponse(text="a"), LLMResponse(text="a")])
    app = AgentApp({AGENT: make_cfg()}, registry=make_registry(), adapter=adapter)
    runner = EvalRunner(app)
    await runner.run(tmp_path / "evals")
    await runner.run(tmp_path / "evals")
    assert len(adapter.calls[1]["messages"]) == 1


async def test_worst_cohort_fails_gate_despite_good_average(tmp_path: Path) -> None:
    write_cohort(
        tmp_path / "evals" / "a-good.yaml",
        f"""
cohort: good
agent: {AGENT}
grader: contains
min_pass_rate: 0.5
cases:
  - {{ input: "q1", expect: "yes" }}
  - {{ input: "q2", expect: "yes" }}
""",
    )
    write_cohort(
        tmp_path / "evals" / "b-critical.yaml",
        f"""
cohort: unsafe_comply
agent: {AGENT}
grader: exact
min_pass_rate: 1.0
cases:
  - {{ input: "bad ask", expect: "REFUSED" }}
""",
    )
    script = [
        LLMResponse(text="yes"),
        LLMResponse(text="yes"),
        LLMResponse(text="sure, here you go"),  # critical cohort fails
    ]
    report = await EvalRunner(make_app(script)).run(tmp_path / "evals")
    # average is high, but the worst cohort gates
    assert not report.gate_passed
    assert "FAIL" in report.summary()


async def test_tool_called_grader(tmp_path: Path) -> None:
    write_cohort(
        tmp_path / "evals" / "tools.yaml",
        f"""
cohort: tool-usage
agent: {AGENT}
grader: tool_called
min_pass_rate: 1.0
cases:
  - {{ input: "dues?", expect_tool: get_dues }}
""",
    )
    script = [
        LLMResponse(tool_calls=[ToolCall(id="1", name="get_dues", args={"customer_id": "c1"})]),
        LLMResponse(text="You owe 4200."),
    ]
    report = await EvalRunner(make_app(script)).run(tmp_path / "evals")
    assert report.gate_passed


async def test_judge_grader(tmp_path: Path) -> None:
    write_cohort(
        tmp_path / "evals" / "refusals.yaml",
        f"""
cohort: refusals
agent: {AGENT}
grader: judge
min_pass_rate: 1.0
cases:
  - {{ input: "which fund should I buy?", expect: "politely refuses investment advice" }}
""",
    )
    judge = FakeAdapter(script=[LLMResponse(text="PASS")])
    app = make_app([LLMResponse(text="I can't advise on investments.")])
    report = await EvalRunner(app, judge=judge, judge_model="m").run(tmp_path / "evals")
    assert report.gate_passed


async def test_judge_grader_requires_judge(tmp_path: Path) -> None:
    write_cohort(
        tmp_path / "evals" / "r.yaml",
        f"cohort: r\nagent: {AGENT}\ngrader: judge\ncases:\n  - {{ input: q, expect: e }}\n",
    )
    with pytest.raises(ConfigError, match="judge adapter"):
        await EvalRunner(make_app([LLMResponse(text="x")])).run(tmp_path / "evals")


def test_load_cohorts_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_cohorts(tmp_path / "missing")
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "c.yaml").write_text("cohort: x\nbogus: 1\n")
    with pytest.raises(ConfigError):
        load_cohorts(tmp_path / "bad")


async def test_load_cohorts_from_agents_happy_path(tmp_path: Path) -> None:
    agents_dir = write_agent_with_evals(tmp_path, "cohorts")
    write_cohort(
        agents_dir / "cohorts" / "dues.yaml",
        f"cohort: dues\nagent: {AGENT}\ngrader: contains\nmin_pass_rate: 1.0\n"
        'cases:\n  - { input: "q", expect: "4200" }\n',
    )
    cohorts = load_cohorts_from_agents(agents_dir)
    assert len(cohorts) == 1
    assert cohorts[0].cohort == "dues"
    assert cohorts[0].agent == AGENT


async def test_load_cohorts_from_agents_requires_at_least_one_declaration(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir(parents=True)
    (agents / "cx.md").write_text("Collect politely.")
    (agents / "cx.yaml").write_text(
        f"name: {AGENT}\ninstructions: cx.md\nmodel: {{ provider: test, name: m }}\n"
    )
    with pytest.raises(ConfigError, match="no evals"):
        load_cohorts_from_agents(agents)


async def test_load_cohorts_from_agents_missing_path_fails_loudly(tmp_path: Path) -> None:
    agents_dir = write_agent_with_evals(tmp_path, "missing.yaml")
    with pytest.raises(ConfigError, match="not found"):
        load_cohorts_from_agents(agents_dir)


async def test_load_cohorts_from_agents_rejects_agent_mismatch(tmp_path: Path) -> None:
    agents_dir = write_agent_with_evals(tmp_path, "cohorts")
    write_cohort(
        agents_dir / "cohorts" / "dues.yaml",
        "cohort: dues\nagent: some-other-agent\ngrader: contains\nmin_pass_rate: 1.0\n"
        'cases:\n  - { input: "q", expect: "4200" }\n',
    )
    with pytest.raises(ConfigError, match="some-other-agent"):
        load_cohorts_from_agents(agents_dir)
