import json
import sys
from pathlib import Path

import pytest

from tests.test_orchestrator import make_cfg
from zolva.audit import AuditLog
from zolva.bridge import LLMResponse
from zolva.bridge.fake import FakeAdapter
from zolva.bus import Step
from zolva.cli import main
from zolva.feedback import FeedbackQueue
from zolva.orchestrator import AgentApp
from zolva.tools import ToolRegistry

AGENT = "collections-agent"


@pytest.fixture()
def app_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A tiny importable module exposing `app`, the way a bank would for `zolva eval --app`."""
    (tmp_path / "bankapp.py").write_text(
        "from zolva.orchestrator import AgentApp\n"
        "from zolva.bridge import LLMResponse\n"
        "from zolva.bridge.fake import FakeAdapter\n"
        "from zolva.config import AgentConfig, ModelConfig\n"
        "from zolva.tools import ToolRegistry\n"
        "cfg = AgentConfig(name='collections-agent', instructions='x',\n"
        "                  model=ModelConfig(provider='test', name='m'))\n"
        "app = AgentApp({'collections-agent': cfg}, registry=ToolRegistry(),\n"
        "               adapter=FakeAdapter(script=[LLMResponse(text='You owe 4200.')]))\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return "bankapp:app"


def test_eval_cli_gate_pass(
    tmp_path: Path, app_module: str, capsys: pytest.CaptureFixture[str]
) -> None:
    evals = tmp_path / "evals"
    evals.mkdir()
    (evals / "dues.yaml").write_text(
        f"cohort: dues\nagent: {AGENT}\ngrader: contains\nmin_pass_rate: 1.0\n"
        'cases:\n  - { input: "what do I owe?", expect: "4200" }\n'
    )
    out = tmp_path / "report.json"
    code = main(["eval", str(evals), "--app", app_module, "--gate", "--out", str(out)])
    assert code == 0
    assert "GATE: PASS" in capsys.readouterr().out
    assert json.loads(out.read_text())["cohorts"][0]["pass_rate"] == 1.0


def test_eval_cli_gate_fail_exits_1(tmp_path: Path, app_module: str) -> None:
    evals = tmp_path / "evals"
    evals.mkdir()
    (evals / "dues.yaml").write_text(
        f"cohort: dues\nagent: {AGENT}\ngrader: exact\nmin_pass_rate: 1.0\n"
        'cases:\n  - { input: "q", expect: "something else entirely" }\n'
    )
    assert main(["eval", str(evals), "--app", app_module, "--gate"]) == 1


def test_eval_cli_unknown_judge_provider(
    tmp_path: Path, app_module: str, capsys: pytest.CaptureFixture[str]
) -> None:
    evals = tmp_path / "evals"
    evals.mkdir()
    (evals / "d.yaml").write_text(
        f"cohort: d\nagent: {AGENT}\ngrader: contains\nmin_pass_rate: 1.0\n"
        'cases:\n  - { input: "q", expect: "4200" }\n'
    )
    code = main(["eval", str(evals), "--app", app_module, "--judge-provider", "nope"])
    assert code == 1
    assert "unknown provider" in capsys.readouterr().err


def test_eval_cli_bad_app_spec(tmp_path: Path) -> None:
    (tmp_path / "e").mkdir()
    assert main(["eval", str(tmp_path / "e"), "--app", "nocolon"]) == 1


def test_eval_cli_agents_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "bankapp3.py").write_text(
        "from zolva.orchestrator import AgentApp\n"
        "from zolva.bridge import LLMResponse\n"
        "from zolva.bridge.fake import FakeAdapter\n"
        "from zolva.config import AgentConfig, ModelConfig\n"
        "from zolva.tools import ToolRegistry\n"
        "cfg = AgentConfig(name='collections-agent', instructions='x',\n"
        "                  model=ModelConfig(provider='test', name='m'))\n"
        "app = AgentApp({'collections-agent': cfg}, registry=ToolRegistry(),\n"
        "               adapter=FakeAdapter(script=[LLMResponse(text='You owe 4200.')]))\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "bankapp3", raising=False)
    config_dir = tmp_path / "config"
    agents = config_dir / "agents"
    agents.mkdir(parents=True)
    (agents / "cx.md").write_text("Collect politely.")
    (agents / "cx.yaml").write_text(
        f"name: {AGENT}\ninstructions: cx.md\n"
        "model: { provider: test, name: m }\n"
        "evals: cohorts\n"
    )
    (agents / "cohorts").mkdir()
    (agents / "cohorts" / "dues.yaml").write_text(
        f"cohort: dues\nagent: {AGENT}\ngrader: contains\nmin_pass_rate: 1.0\n"
        'cases:\n  - { input: "q", expect: "4200" }\n'
    )
    code = main(["eval", "--agents", str(agents), "--app", "bankapp3:app", "--gate"])
    assert code == 0
    assert "GATE: PASS" in capsys.readouterr().out


def test_eval_cli_evals_dir_and_agents_are_mutually_exclusive(
    tmp_path: Path, app_module: str
) -> None:
    (tmp_path / "e").mkdir()
    (tmp_path / "a").mkdir()
    # both given
    assert (
        main(["eval", str(tmp_path / "e"), "--agents", str(tmp_path / "a"), "--app", app_module])
        == 1
    )
    # neither given
    assert main(["eval", "--app", app_module]) == 1


def test_synthetics_cli_gate_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "synthapp.py").write_text(
        "from zolva.orchestrator import AgentApp\n"
        "from zolva.bridge import LLMResponse, register_adapter\n"
        "from zolva.bridge.fake import FakeAdapter\n"
        "from zolva.config import AgentConfig, ModelConfig\n"
        "from zolva.tools import ToolRegistry\n"
        "register_adapter('synthdrv', lambda: FakeAdapter(script=[\n"
        "    LLMResponse(text='what do I owe?'), LLMResponse(text='DONE')]))\n"
        "register_adapter('synthjdg', lambda: FakeAdapter(script=[LLMResponse(text='PASS')]))\n"
        "cfg = AgentConfig(name='collections-agent', instructions='x',\n"
        "                  model=ModelConfig(provider='test', name='m'))\n"
        "app = AgentApp({'collections-agent': cfg}, registry=ToolRegistry(),\n"
        "               adapter=FakeAdapter(script=[LLMResponse(text='You owe 4200.')]))\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "synthapp", raising=False)
    sdir = tmp_path / "synthetics"
    sdir.mkdir()
    (sdir / "dues.yaml").write_text(
        f"agent: {AGENT}\npersona: overdue customer\n"
        'goal: "customer learns their dues"\nmax_turns: 3\n'
    )
    out = tmp_path / "synth-report.json"
    code = main(
        [
            "synthetics",
            str(sdir),
            "--app",
            "synthapp:app",
            "--driver-provider",
            "synthdrv",
            "--judge-provider",
            "synthjdg",
            "--gate",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    stdout = capsys.readouterr().out
    assert "PASS" in stdout
    assert "GATE: PASS" in stdout
    data = json.loads(out.read_text())
    assert len(data) == 1
    assert data[0]["passed"] is True


def test_synthetics_cli_gate_fail_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "synthapp2.py").write_text(
        "from zolva.orchestrator import AgentApp\n"
        "from zolva.bridge import LLMResponse, register_adapter\n"
        "from zolva.bridge.fake import FakeAdapter\n"
        "from zolva.config import AgentConfig, ModelConfig\n"
        "from zolva.tools import ToolRegistry\n"
        "register_adapter('synthdrv2', lambda: FakeAdapter(script=[\n"
        "    LLMResponse(text='what do I owe?'), LLMResponse(text='DONE')]))\n"
        "register_adapter('synthjdg2', lambda: FakeAdapter(script=[LLMResponse(text='FAIL')]))\n"
        "cfg = AgentConfig(name='collections-agent', instructions='x',\n"
        "                  model=ModelConfig(provider='test', name='m'))\n"
        "app = AgentApp({'collections-agent': cfg}, registry=ToolRegistry(),\n"
        "               adapter=FakeAdapter(script=[LLMResponse(text='You owe 4200.')]))\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "synthapp2", raising=False)
    sdir = tmp_path / "synthetics"
    sdir.mkdir()
    (sdir / "dues.yaml").write_text(
        f"agent: {AGENT}\npersona: overdue customer\n"
        'goal: "customer learns their dues"\nmax_turns: 3\n'
    )
    code = main(
        [
            "synthetics",
            str(sdir),
            "--app",
            "synthapp2:app",
            "--driver-provider",
            "synthdrv2",
            "--judge-provider",
            "synthjdg2",
            "--gate",
        ]
    )
    assert code == 1


def test_eval_cli_imports_app_from_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bank runs `zolva eval --app app:app` from its project root; CWD must be importable."""
    (tmp_path / "bankapp2.py").write_text(
        "from zolva.orchestrator import AgentApp\n"
        "from zolva.bridge import LLMResponse\n"
        "from zolva.bridge.fake import FakeAdapter\n"
        "from zolva.config import AgentConfig, ModelConfig\n"
        "from zolva.tools import ToolRegistry\n"
        "cfg = AgentConfig(name='collections-agent', instructions='x',\n"
        "                  model=ModelConfig(provider='test', name='m'))\n"
        "app = AgentApp({'collections-agent': cfg}, registry=ToolRegistry(),\n"
        "               adapter=FakeAdapter(script=[LLMResponse(text='You owe 4200.')]))\n"
    )
    evals = tmp_path / "evals"
    evals.mkdir()
    (evals / "d.yaml").write_text(
        f"cohort: d\nagent: {AGENT}\ngrader: contains\nmin_pass_rate: 1.0\n"
        'cases:\n  - { input: "q", expect: "4200" }\n'
    )
    monkeypatch.chdir(tmp_path)  # NOT on sys.path; the CLI must add it
    monkeypatch.delitem(sys.modules, "bankapp2", raising=False)
    assert main(["eval", str(evals), "--app", "bankapp2:app", "--gate"]) == 0


async def test_scorecard_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app = AgentApp(
        {AGENT: make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[LLMResponse(text="ok")]),
    )
    log = AuditLog(tmp_path / "audit.db")
    log.attach(app)
    await app.run(AGENT, "s1", "hi")
    assert main(["scorecard", str(tmp_path / "audit.db")]) == 0
    assert "SARR" in capsys.readouterr().out


async def test_triage_and_export_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app = AgentApp(
        {AGENT: make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[LLMResponse(text="wrong")]),
    )
    db = tmp_path / "fb.db"
    q = FeedbackQueue(db)
    q.attach(app)
    await app.run(AGENT, "s1", "when is my due date?")
    await q.record("s1", AGENT, "thumbs_down", note="wrong date")
    # list
    assert main(["triage", str(db)]) == 0
    assert "1 pending" in capsys.readouterr().out
    # accept requires cohort+expect
    assert main(["triage", str(db), "--accept", "1"]) == 1
    cohort = tmp_path / "regressions.yaml"
    assert main(["triage", str(db), "--accept", "1", "--cohort", str(cohort), "--expect", "e"]) == 0
    # export
    out = tmp_path / "d.jsonl"
    assert main(["export-dataset", str(db), str(out)]) == 0
    assert "1 accepted" in capsys.readouterr().out


def test_audit_tamper_fails_scorecard(tmp_path: Path) -> None:
    import sqlite3

    log = AuditLog(tmp_path / "a.db")
    log.append(Step(type="user_msg", session_id="s", agent="a", data={"text": "x"}))
    with sqlite3.connect(tmp_path / "a.db") as conn:
        conn.execute("UPDATE audit SET data = '{}'")
    assert main(["scorecard", str(tmp_path / "a.db")]) == 1
