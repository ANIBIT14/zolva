"""Fixes from the 2026-07 full-package audit: adapter registration, startup
tool validation, fail-closed bus hooks, surviving handover-backend failure,
audit session index, and `zolva validate` parity with runtime startup."""

import sqlite3
from pathlib import Path

import pytest

from tests.test_orchestrator import CapturingHandover, make_cfg
from zolva.audit import AuditLog
from zolva.bridge import BridgeError, LLMResponse, get_adapter
from zolva.bridge.fake import FakeAdapter
from zolva.bus import Bus, Step, Verdict
from zolva.cli import main
from zolva.config import ConfigError
from zolva.handover import HandoverBackend, HandoverError, HandoverRef, Ticket
from zolva.orchestrator import BLOCKED_MESSAGE, AgentApp
from zolva.tools import ToolRegistry


# --- #1: built-in providers resolve without a manual side-effect import ----


def test_get_adapter_lazily_registers_builtin_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert type(get_adapter("openai")).__name__ == "OpenAIAdapter"
    assert type(get_adapter("anthropic")).__name__ == "AnthropicAdapter"


def test_get_adapter_unknown_provider_still_loud() -> None:
    with pytest.raises(BridgeError, match="unknown provider"):
        get_adapter("no-such-provider")


# --- #2: declared-but-unregistered tool fails at startup, not mid-run ------


def test_unregistered_declared_tool_fails_at_construction() -> None:
    with pytest.raises(ConfigError, match="collections-agent.*not_registered"):
        AgentApp(
            {"collections-agent": make_cfg(tools=["not_registered"])},
            registry=ToolRegistry(),
            adapter=FakeAdapter(script=[]),
        )


# --- #3: a crashing bus hook blocks the step instead of crashing the run ---


async def test_bus_hook_exception_fails_closed() -> None:
    bus = Bus()

    async def broken_hook(step: Step) -> Verdict | None:
        raise sqlite3.OperationalError("disk I/O error")

    bus.on(broken_hook)
    verdict = await bus.emit(Step(type="user_msg", session_id="s1", agent="a", data={}))
    assert not verdict.allow
    assert "safety hook failure" in (verdict.reason or "")


async def test_broken_audit_hook_degrades_to_handover_not_crash() -> None:
    bus = Bus()

    async def broken_hook(step: Step) -> Verdict | None:
        raise RuntimeError("audit backend down")

    bus.on(broken_hook)
    handover = CapturingHandover()
    app = AgentApp(
        {"collections-agent": make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[LLMResponse(text="hi")]),
        bus=bus,
        handover=handover,
    )
    reply = await app.run("collections-agent", "s1", "hello")
    assert reply == BLOCKED_MESSAGE
    assert len(handover.tickets) == 1  # escalation still delivered


# --- #4: a down ticketing backend never silences the customer --------------


class DownBackend(HandoverBackend):
    async def escalate(self, ticket: Ticket) -> HandoverRef:
        raise HandoverError("webhook unreachable")


async def test_escalation_backend_failure_still_returns_blocked_message() -> None:
    app = AgentApp(
        {"collections-agent": make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[]),  # exhausted script -> BridgeError -> escalate
        handover=DownBackend(),
    )
    assert await app.run("collections-agent", "s1", "hi") == BLOCKED_MESSAGE


# --- #14: audit log is indexed by session ----------------------------------


def test_audit_has_session_index(tmp_path: Path) -> None:
    AuditLog(tmp_path / "a.db")
    with sqlite3.connect(tmp_path / "a.db") as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_audit_session" in names


# --- #7: `zolva validate` runs the same checks as runtime startup ----------


def _write_agent(d: Path, *, guardrails: str | None = None, evals: str | None = None) -> None:
    lines = [
        "name: collections-agent",
        "instructions: collections.md",
        "model: { provider: openai, name: gpt-5 }",
    ]
    if guardrails:
        lines.append(f"guardrails: {guardrails}")
    if evals:
        lines.append(f"evals: {evals}")
    (d / "collections.yaml").write_text("\n".join(lines) + "\n")
    (d / "collections.md").write_text("Be polite.")


def test_validate_rejects_bad_policy_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_agent(tmp_path, guardrails="policies/policy.yaml")
    (tmp_path / "policies").mkdir(exist_ok=True)
    (tmp_path / "policies/policy.yaml").write_text(
        'pre:\n  - block_outside_window: { start: "08:00", end: "20:00", tz: "UTC" }\n'
    )
    assert main(["validate", str(tmp_path)]) == 1
    assert "block_outside_window needs {hours, tz}" in capsys.readouterr().err


def test_validate_accepts_judge_rules_without_adapter(tmp_path: Path) -> None:
    _write_agent(tmp_path, guardrails="policies/policy.yaml")
    (tmp_path / "policies").mkdir(exist_ok=True)
    (tmp_path / "policies/policy.yaml").write_text('post:\n  - never: ["threats"]\n')
    assert main(["validate", str(tmp_path)]) == 0


def test_validate_parses_declared_eval_cohorts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_agent(tmp_path, evals="evals/cohort.yaml")
    (tmp_path / "evals").mkdir(exist_ok=True)
    (tmp_path / "evals/cohort.yaml").write_text(
        "cohort: dues\nagent: collections-agent\ngrader: exact\n"
        'cases:\n  - { input: "dues?", expect: "4200" }\n'
    )
    assert main(["validate", str(tmp_path)]) == 0
    assert "1 eval cohort(s) parsed" in capsys.readouterr().out


def test_validate_rejects_cohort_agent_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_agent(tmp_path, evals="evals/cohort.yaml")
    (tmp_path / "evals").mkdir(exist_ok=True)
    (tmp_path / "evals/cohort.yaml").write_text(
        "cohort: dues\nagent: other-agent\ngrader: exact\n"
        'cases:\n  - { input: "dues?", expect: "4200" }\n'
    )
    assert main(["validate", str(tmp_path)]) == 1
    assert "other-agent" in capsys.readouterr().err
