import json
from pathlib import Path

import pytest

from tests.test_orchestrator import CapturingHandover, make_cfg
from zolva.bridge import LLMResponse
from zolva.bridge.fake import FakeAdapter
from zolva.bus import Bus, Step, Verdict
from zolva.config import ConfigError
from zolva.evals import load_cohorts
from zolva.feedback import FeedbackQueue
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


async def test_manual_record_and_pending(tmp_path: Path) -> None:
    app = make_app([LLMResponse(text="wrong due date is tomorrow")])
    q = FeedbackQueue(tmp_path / "fb.db")
    q.attach(app)
    await app.run(AGENT, "s1", "when is my due date?")
    await q.record("s1", AGENT, "thumbs_down", note="wrong due date")
    pending = q.pending()
    assert len(pending) == 1
    assert pending[0].kind == "thumbs_down"
    assert any("due date" in m.content for m in pending[0].transcript)


async def test_escalations_auto_captured(tmp_path: Path) -> None:
    bus = Bus()

    async def block_all(s: Step) -> Verdict | None:
        if s.type == "response":
            return Verdict(allow=False, reason="policy")
        return None

    bus.on(block_all)
    app = make_app([LLMResponse(text="bad reply")], bus=bus)
    q = FeedbackQueue(tmp_path / "fb.db")
    q.attach(app)
    await app.run(AGENT, "s1", "hi")
    pending = q.pending()
    assert len(pending) == 1 and pending[0].kind == "escalation"
    assert pending[0].note == "policy"


async def test_accept_promotes_to_eval_cohort(tmp_path: Path) -> None:
    app = make_app([LLMResponse(text="wrong answer")])
    q = FeedbackQueue(tmp_path / "fb.db")
    q.attach(app)
    await app.run(AGENT, "s1", "what is my due date?")
    await q.record("s1", AGENT, "thumbs_down")
    fid = q.pending()[0].id
    cohort_file = tmp_path / "evals" / "regressions.yaml"
    q.accept(fid, cohort_file, expect="states the correct due date from the ledger")
    # queue updated
    assert q.pending() == [] and len(q.accepted()) == 1
    # the promoted case is a valid, loadable eval cohort
    cohorts = load_cohorts(tmp_path / "evals")
    assert cohorts[0].cases[0].input == "what is my due date?"
    assert cohorts[0].min_pass_rate == 1.0
    # accepting a second failure appends, not overwrites
    await q.record("s1", AGENT, "thumbs_down", note="another")
    q.accept(q.pending()[0].id, cohort_file, expect="also correct")
    assert len(load_cohorts(tmp_path / "evals")[0].cases) == 2


async def test_reject_and_unknown_id(tmp_path: Path) -> None:
    q = FeedbackQueue(tmp_path / "fb.db")
    await q.record("s1", AGENT, "thumbs_down")
    q.reject(q.pending()[0].id)
    assert q.pending() == []
    with pytest.raises(ConfigError, match="no pending failure"):
        q.accept(999, tmp_path / "c.yaml", expect="x")


async def test_export_dataset_jsonl(tmp_path: Path) -> None:
    app = make_app([LLMResponse(text="reply")])
    q = FeedbackQueue(tmp_path / "fb.db")
    q.attach(app)
    await app.run(AGENT, "s1", "question")
    await q.record("s1", AGENT, "thumbs_down", note="n")
    q.accept(q.pending()[0].id, tmp_path / "c.yaml", expect="e")
    out = tmp_path / "dataset.jsonl"
    assert q.export_dataset(out) == 1
    row = json.loads(out.read_text().splitlines()[0])
    assert row["kind"] == "thumbs_down"
    assert row["messages"][0]["role"] == "user"
