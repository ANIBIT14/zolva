import sqlite3
from pathlib import Path

from tests.test_orchestrator import CapturingHandover, make_cfg
from zolva.audit import AuditLog, scorecard
from zolva.bridge import LLMResponse
from zolva.bridge.fake import FakeAdapter
from zolva.bus import Bus, Step, Verdict
from zolva.orchestrator import AgentApp
from zolva.tools import ToolRegistry

AGENT = "collections-agent"


def make_app(script: list[LLMResponse], bus: Bus | None = None) -> AgentApp:
    return AgentApp(
        {AGENT: make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=script),
        bus=bus,
        handover=CapturingHandover(),
    )


async def test_every_step_lands_in_audit_and_chain_verifies(tmp_path: Path) -> None:
    app = make_app([LLMResponse(text="You owe 4200.")])
    log = AuditLog(tmp_path / "audit.db")
    log.attach(app)
    await app.run(AGENT, "s1", "dues?")
    with sqlite3.connect(tmp_path / "audit.db") as conn:
        types = [r[0] for r in conn.execute("SELECT type FROM audit ORDER BY id")]
    assert types == ["user_msg", "model_call", "model_result", "response"]
    assert log.verify()


async def test_tampering_detected(tmp_path: Path) -> None:
    app = make_app([LLMResponse(text="ok")])
    log = AuditLog(tmp_path / "audit.db")
    log.attach(app)
    await app.run(AGENT, "s1", "hi")
    assert log.verify()
    with sqlite3.connect(tmp_path / "audit.db") as conn:
        conn.execute('UPDATE audit SET data = \'{"text": "FORGED"}\' WHERE id = 1')
    assert not log.verify()


async def test_deletion_detected(tmp_path: Path) -> None:
    app = make_app([LLMResponse(text="ok")])
    log = AuditLog(tmp_path / "audit.db")
    log.attach(app)
    await app.run(AGENT, "s1", "hi")
    with sqlite3.connect(tmp_path / "audit.db") as conn:
        conn.execute("DELETE FROM audit WHERE id = 2")
    assert not log.verify()


async def test_tail_truncation_detected_with_anchor(tmp_path: Path) -> None:
    """Deleting the newest rows leaves a self-consistent chain; only an
    externally kept head hash (e.g. from the last compliance pack) catches it."""
    db = tmp_path / "audit.db"
    log = AuditLog(db)
    for i in range(3):
        log.append(Step(type="user_msg", session_id="s1", agent=AGENT, data={"i": i}))
    anchor = log.records()[-1][7]
    assert log.verify(anchor=anchor)
    log.append(Step(type="user_msg", session_id="s1", agent=AGENT, data={"i": 3}))
    assert log.verify(anchor=anchor)  # growth past the anchor is fine
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM audit WHERE id >= 3")
    assert log.verify()  # the chain alone can't see a cut tail
    assert not log.verify(anchor=anchor)


async def test_scorecard_sarr_and_containment(tmp_path: Path) -> None:
    bus = Bus()

    async def block_fund_talk(s: Step) -> Verdict | None:
        if s.type == "response" and "fund" in str(s.data.get("text", "")):
            return Verdict(allow=False, reason="policy")
        return None

    bus.on(block_fund_talk)
    app = make_app([LLMResponse(text="You owe 4200."), LLMResponse(text="Buy this fund!")], bus=bus)
    log = AuditLog(tmp_path / "audit.db")
    log.attach(app)
    await app.run(AGENT, "good-session", "dues?")
    await app.run(AGENT, "bad-session", "advice?")
    card = scorecard(log)
    assert card.sessions == 2
    assert card.resolved == 1 and card.escalated == 1
    assert card.sarr == 0.5 and card.containment == 0.5
    assert "SARR=50.0%" in card.summary()


async def test_blocked_steps_are_audited_regardless_of_attach_order(tmp_path: Path) -> None:
    """Guardrails usually attach first (from_config); the audit must still see
    the step they block, not only the handover that follows."""
    app = make_app([LLMResponse(text="secret")])

    async def block_response(step: Step) -> Verdict | None:
        return Verdict(allow=False, reason="policy") if step.type == "response" else None

    app.bus.on(block_response)  # attached BEFORE the audit
    log = AuditLog(tmp_path / "audit.db")
    log.attach(app)
    await app.run(AGENT, "s1", "hi")
    types = [r[4] for r in log.records()]
    assert "response" in types and types[-1] == "handover"
