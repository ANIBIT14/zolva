"""Compliance evidence pack: article mapping, tamper-detection, gate, self-seal."""

import hashlib
import json
from pathlib import Path

from zolva.audit import AuditLog, InMemoryAuditStore
from zolva.bus import Step
from zolva.cli import main
from zolva.compliance import build_report, config_hash
from zolva.config import AgentConfig, ModelConfig
from zolva.evals import CohortResult, EvalReport


def step(sid: str, step_type: str = "user_msg", **data: object) -> Step:
    return Step(type=step_type, session_id=sid, agent="collections", data=data or {"text": "hi"})  # type: ignore[arg-type]


def agent(
    name: str = "collections", instructions: str = "be nice", handoffs: list[str] | None = None
) -> AgentConfig:
    return AgentConfig(
        name=name,
        instructions=instructions,
        model=ModelConfig(provider="openai", name="gpt-5"),
        handoffs=handoffs or [],
    )


def populated_log() -> AuditLog:
    log = AuditLog(InMemoryAuditStore())
    log.append(step("s1"))
    log.append(step("s1", "response", text="ok"))  # resolved session
    log.append(step("s2"))
    log.append(step("s2", "handover", reason="hardship"))  # escalated session
    return log


def _control(report: object, article_prefix: str) -> object:
    return next(c for c in report.controls if c.article.startswith(article_prefix))  # type: ignore[attr-defined]


def test_empty_log_fails_art12_and_not_ready() -> None:
    report = build_report(AuditLog(InMemoryAuditStore()))
    assert _control(report, "EU AI Act Art. 12").status == "fail"
    assert report.regulator_ready is False


def test_full_pack_is_regulator_ready() -> None:
    agents = {"collections": agent(handoffs=["human-escalation"])}
    ev = EvalReport(
        cohorts=[CohortResult(cohort="refusals", pass_rate=1.0, min_pass_rate=1.0, results=[])]
    )
    report = build_report(populated_log(), agents=agents, eval_report=ev)
    assert all(c.status == "pass" for c in report.controls)
    assert report.regulator_ready is True
    assert report.chain_verified is True
    assert _control(report, "EU AI Act Art. 14").evidence["escalations_recorded"] == 1
    assert report.bundle_sha256  # sealed


def test_tamper_breaks_art12_and_readiness() -> None:
    log = populated_log()
    store = log._store
    assert isinstance(store, InMemoryAuditStore)
    rid, ts, sid, ag, typ, _data, prev, digest = store._rows[1]
    store._rows[1] = (rid, ts, sid, ag, typ, '{"text": "FORGED"}', prev, digest)
    report = build_report(log, agents={"collections": agent(handoffs=["human-escalation"])})
    assert report.chain_verified is False
    assert _control(report, "EU AI Act Art. 12").status == "fail"
    assert report.regulator_ready is False


def test_missing_agents_and_evals_are_not_configured() -> None:
    report = build_report(populated_log())
    assert _control(report, "EU AI Act Art. 13").status == "not_configured"
    assert _control(report, "EU AI Act Art. 15").status == "not_configured"
    # a not_configured control holds readiness back rather than passing vacuously
    assert report.regulator_ready is False


def test_config_hash_pins_the_definition() -> None:
    assert config_hash(agent(instructions="v1")) == config_hash(agent(instructions="v1"))
    assert config_hash(agent(instructions="v1")) != config_hash(agent(instructions="v2"))


def test_bundle_self_seal_recomputes() -> None:
    report = build_report(populated_log())
    dumped = report.model_dump()
    dumped["bundle_sha256"] = ""
    assert (
        report.bundle_sha256
        == hashlib.sha256(json.dumps(dumped, sort_keys=True).encode()).hexdigest()
    )


def test_cli_compliance_gate(tmp_path: Path, capsys: object) -> None:
    db = tmp_path / "audit.sqlite"
    log = AuditLog(db)
    log.append(step("s1"))
    log.append(step("s1", "response", text="ok"))
    assert main(["compliance", str(db)]) == 0  # no gate: reports, exits 0
    assert main(["compliance", str(db), "--gate"]) == 1  # not ready (no agents/evals)
    out = tmp_path / "pack.json"
    assert main(["compliance", str(db), "--out", str(out)]) == 0
    bundle = json.loads(out.read_text())
    assert bundle["audit_head_hash"] and "controls" in bundle
