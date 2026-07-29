"""Compliance evidence pack: map the artifacts Zolva already produces to the
specific regulatory controls a bank must evidence.

Nothing new is instrumented. The audit hash-chain (Art. 12 record-keeping), the
config hashes (Art. 13 traceable system definition), handover events (Art. 14
human oversight), and eval gates + the SARR scorecard (Art. 15 / SR 11-7
accuracy and ongoing monitoring) are read back and asserted against named
controls, then emitted as one signed-by-anchor JSON bundle. The audit chain's
head hash is the tamper-evidence anchor; `bundle_sha256` self-seals the report.

This is packaging of existing evidence, not a compliance guarantee: a passing
report says the controls are wired and exercised, the bank's own mapping and
sign-off still decide adequacy.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel

from zolva.audit import (
    NON_PRODUCTION_SESSION_PREFIXES,
    AuditLog,
    AuditRow,
    Scorecard,
    scorecard,
)
from zolva.config import AgentConfig
from zolva.evals import EvalReport

Status = Literal["pass", "fail", "not_configured"]


class Control(BaseModel):
    article: str  # e.g. "EU AI Act Art. 12"
    name: str
    status: Status
    detail: str
    evidence: dict[str, object] = {}


class ComplianceReport(BaseModel):
    generated_at: str
    chain_verified: bool
    audit_head_hash: str
    regulator_ready: bool
    controls: list[Control]
    bundle_sha256: str = ""  # self-seal over the report minus this field

    def summary(self) -> str:
        mark = {"pass": "PASS", "fail": "FAIL", "not_configured": "N/A "}  # nosec B105: status labels
        lines = [f"{mark[c.status]}  {c.article:20s} {c.name}" for c in self.controls]
        lines.append(f"chain_verified={self.chain_verified}  head={self.audit_head_hash[:12]}")
        lines.append(f"REGULATOR-READY: {'YES' if self.regulator_ready else 'NO'}")
        return "\n".join(lines)


def config_hash(cfg: AgentConfig) -> str:
    """Deterministic digest of the exact agent definition in force (instructions,
    model, tools, handoffs, policy refs). Any change to what the agent is or does
    changes this hash — that is the Art. 13 traceability evidence."""
    return hashlib.sha256(cfg.model_dump_json().encode()).hexdigest()


def _art12(rows: list[AuditRow], chain_ok: bool) -> Control:
    if not rows:
        return Control(
            article="EU AI Act Art. 12",
            name="Record-keeping & traceability",
            status="fail",
            detail="audit log is empty; no interaction records to evidence",
        )
    timestamps = sorted(r[1] for r in rows)
    sessions = len({r[2] for r in rows})
    return Control(
        article="EU AI Act Art. 12",
        name="Record-keeping & traceability",
        status="pass" if chain_ok else "fail",
        detail=(
            "hash-chained append-only audit log; every step timestamped and "
            + ("chain intact" if chain_ok else "CHAIN BROKEN — records tampered")
        ),
        evidence={
            "records": len(rows),
            "sessions": sessions,
            "first_record": timestamps[0],
            "last_record": timestamps[-1],
            "tamper_evident": True,
        },
    )


def _art13(agents: dict[str, AgentConfig] | None) -> Control:
    if not agents:
        return Control(
            article="EU AI Act Art. 13",
            name="Transparency & traceable system definition",
            status="not_configured",
            detail="no agent config supplied (--agents); cannot hash the deployed definition",
        )
    hashes = {
        name: {"config_sha256": config_hash(cfg), "model": f"{cfg.model.provider}/{cfg.model.name}"}
        for name, cfg in sorted(agents.items())
    }
    return Control(
        article="EU AI Act Art. 13",
        name="Transparency & traceable system definition",
        status="pass",
        detail="each agent's instructions, model and tool grants are hashed and pinned",
        evidence={"agents": hashes},
    )


def _art14(by_session: dict[str, set[str]], agents: dict[str, AgentConfig] | None) -> Control:
    # count production sessions that escalated; eval/synthetic runs against a
    # live app must not inflate the human-oversight evidence (same exclusion
    # the SARR scorecard uses)
    escalated = sorted(
        sid
        for sid, types in by_session.items()
        if "handover" in types and not sid.startswith(NON_PRODUCTION_SESSION_PREFIXES)
    )
    with_handoffs = sorted(n for n, c in (agents or {}).items() if c.handoffs)
    # oversight is real if the path is declared (agents can escalate) or exercised
    configured = bool(with_handoffs) or bool(escalated)
    return Control(
        article="EU AI Act Art. 14",
        name="Human oversight",
        status="pass" if configured else "not_configured",
        detail=(
            "human-handover path present; escalations are logged and auditable"
            if configured
            else "no handoffs declared and no escalations recorded"
        ),
        evidence={"escalations_recorded": len(escalated), "agents_with_handoffs": with_handoffs},
    )


def _art15(card: Scorecard, eval_report: EvalReport | None) -> Control:
    ev: dict[str, object] = {"sarr": round(card.sarr, 4), "containment": round(card.containment, 4)}
    if eval_report is None:
        return Control(
            article="EU AI Act Art. 15",
            name="Accuracy, robustness & ongoing monitoring",
            status="not_configured",
            detail="no eval report supplied (--eval-report); provide a gated run as evidence",
            evidence=ev,
        )
    ev["eval_gate_passed"] = eval_report.gate_passed
    ev["cohorts"] = {
        c.cohort: {"pass_rate": round(c.pass_rate, 4), "gate": c.min_pass_rate, "passed": c.passed}
        for c in eval_report.cohorts
    }
    return Control(
        article="EU AI Act Art. 15",
        name="Accuracy, robustness & ongoing monitoring",
        status="pass" if eval_report.gate_passed else "fail",
        detail=(
            "CI-gated eval cohorts (worst-cohort gate) plus the live SARR scorecard"
            if eval_report.gate_passed
            else "an eval cohort is below its gate — model quality regression"
        ),
        evidence=ev,
    )


def build_report(
    audit: AuditLog,
    *,
    agents: dict[str, AgentConfig] | None = None,
    eval_report: EvalReport | None = None,
) -> ComplianceReport:
    """Assemble the article-mapped evidence bundle from live artifacts.

    `regulator_ready` requires the chain to verify and every control to be
    PASS — a `not_configured` control (missing agents or eval evidence) holds
    the report back rather than passing vacuously.
    """
    chain_ok = audit.verify()
    rows = audit.records()  # read the log once; the helpers derive from it
    by_session = audit.step_types_by_session()
    head = rows[-1][7] if rows else ""
    controls = [
        _art12(rows, chain_ok),
        _art13(agents),
        _art14(by_session, agents),
        _art15(scorecard(audit), eval_report),
    ]
    ready = chain_ok and all(c.status == "pass" for c in controls)
    report = ComplianceReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        chain_verified=chain_ok,
        audit_head_hash=head,
        regulator_ready=ready,
        controls=controls,
    )
    # self-seal: sha256 over the canonical report with bundle_sha256 blank
    report.bundle_sha256 = hashlib.sha256(
        json.dumps(report.model_dump(), sort_keys=True).encode()
    ).hexdigest()
    return report
