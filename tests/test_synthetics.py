from pathlib import Path

import pytest

from tests.test_orchestrator import make_cfg, make_registry
from zolva.bridge import LLMResponse
from zolva.bridge.fake import FakeAdapter
from zolva.config import ConfigError
from zolva.orchestrator import AgentApp
from zolva.synthetics import SyntheticRunner, gate_passed, load_synthetics

AGENT = "collections-agent"


def write_synthetic(dir_path: Path, persona_inline: bool = True) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    if persona_inline:
        (dir_path / "repayment.yaml").write_text(
            f"""
agent: {AGENT}
persona: "You are an overdue customer who wants to settle."
goal: "customer obtains their dues amount"
max_turns: 3
"""
        )
    else:
        (dir_path / "persona.md").write_text("You are an overdue customer.")
        (dir_path / "repayment.yaml").write_text(
            f"agent: {AGENT}\npersona_file: persona.md\ngoal: g\n"
        )


def make_app(script: list[LLMResponse]) -> AgentApp:
    return AgentApp(
        {AGENT: make_cfg(tools=[])}, registry=make_registry(), adapter=FakeAdapter(script=script)
    )


async def test_synthetic_conversation_and_judge_pass(tmp_path: Path) -> None:
    write_synthetic(tmp_path / "synthetics")
    driver = FakeAdapter(script=[LLMResponse(text="How much do I owe?"), LLMResponse(text="DONE")])
    judge = FakeAdapter(script=[LLMResponse(text="PASS")])
    app = make_app([LLMResponse(text="You owe 4200, due 2026-07-20.")])
    runner = SyntheticRunner(app, driver=driver, judge=judge)
    results = await runner.run(tmp_path / "synthetics")
    assert len(results) == 1 and results[0].passed
    assert "CUSTOMER: How much do I owe?" in results[0].transcript
    assert "AGENT: You owe 4200" in results[0].transcript
    assert gate_passed(results)


async def test_judge_fail_gates(tmp_path: Path) -> None:
    write_synthetic(tmp_path / "synthetics")
    driver = FakeAdapter(script=[LLMResponse(text="hello?"), LLMResponse(text="DONE")])
    judge = FakeAdapter(script=[LLMResponse(text="FAIL: never got the amount")])
    app = make_app([LLMResponse(text="I cannot help.")])
    results = await SyntheticRunner(app, driver=driver, judge=judge).run(tmp_path / "synthetics")
    assert not results[0].passed and not gate_passed(results)


def test_persona_file_loaded(tmp_path: Path) -> None:
    write_synthetic(tmp_path / "synthetics", persona_inline=False)
    synths = load_synthetics(tmp_path / "synthetics")
    assert synths[0].persona == "You are an overdue customer."


def test_load_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_synthetics(tmp_path / "missing")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "s.yaml").write_text("agent: a\ngoal: g\n")  # no persona at all
    with pytest.raises(ConfigError, match="persona"):
        load_synthetics(bad)
