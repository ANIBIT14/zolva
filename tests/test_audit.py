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
    assert types == ["user_msg", "model_call", "response"]
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
