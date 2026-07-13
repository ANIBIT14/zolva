from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.test_orchestrator import CapturingHandover, make_cfg
from zolva.bridge import LLMResponse
from zolva.bridge.fake import FakeAdapter
from zolva.bus import Step
from zolva.config import ConfigError
from zolva.guardrails import Guardrails
from zolva.orchestrator import BLOCKED_MESSAGE, AgentApp
from zolva.tools import ToolRegistry

AGENT = "collections-agent"


def step(step_type: str, text: str, agent: str = AGENT) -> Step:
    return Step(type=step_type, session_id="s1", agent=agent, data={"text": text})  # type: ignore[arg-type]


def at(hhmm: str):  # type: ignore[no-untyped-def]
    def _now(tz: ZoneInfo) -> datetime:
        h, m = hhmm.split(":")
        return datetime(2026, 7, 13, int(h), int(m), tzinfo=tz)

    return _now


async def test_contact_window_blocks_outside_hours() -> None:
    policy = {"pre": [{"block_outside_window": {"hours": "08:00-19:00", "tz": "Asia/Kolkata"}}]}
    g = Guardrails(policy, agent=AGENT, now=at("22:30"))
    v = await g._hook(step("user_msg", "pay up"))
    assert v is not None and not v.allow and "contact window" in str(v.reason)
    g_day = Guardrails(policy, agent=AGENT, now=at("10:00"))
    assert await g_day._hook(step("user_msg", "pay up")) is None


async def test_disclaimer_required_when_topic_mentioned() -> None:
    policy = {
        "post": [
            {"require_disclaimer": {"when": "mutual fund", "text": "Subject to market risks."}}
        ]
    }
    g = Guardrails(policy, agent=AGENT)
    v = await g._hook(step("response", "Try our mutual fund plans!"))
    assert v is not None and not v.allow
    ok = await g._hook(step("response", "Try our mutual fund plans! Subject to market risks."))
    assert ok is None
    unrelated = await g._hook(step("response", "Your dues are 4200."))
    assert unrelated is None


async def test_judge_rules_block_on_fail() -> None:
    policy = {"post": [{"never": ["threats"]}]}
    judge = FakeAdapter(script=[LLMResponse(text="FAIL")])
    g = Guardrails(policy, agent=AGENT, judge=judge, judge_model="m")
    v = await g._hook(step("response", "pay or else"))
    assert v is not None and not v.allow and "never-rule violation: threats" in str(v.reason)


async def test_judge_pass_allows() -> None:
    policy = {"post": [{"refuse_topics": ["investment_advice"]}]}
    judge = FakeAdapter(script=[LLMResponse(text="PASS")])
    g = Guardrails(policy, agent=AGENT, judge=judge, judge_model="m")
    assert await g._hook(step("response", "your balance is 4200")) is None


async def test_other_agents_unaffected() -> None:
    policy = {"pre": [{"block_outside_window": {"hours": "08:00-19:00", "tz": "Asia/Kolkata"}}]}
    g = Guardrails(policy, agent=AGENT, now=at("23:00"))
    assert await g._hook(step("user_msg", "hi", agent="cx-agent")) is None


def test_unknown_rule_fails_at_construction() -> None:
    with pytest.raises(ConfigError, match="unknown guardrail rule"):
        Guardrails({"pre": [{"bogus_rule": {}}]}, agent=AGENT)


def test_malformed_window_fails_at_construction() -> None:
    for bad in ("08:00-12:30-19:00", "8:00-19:00", "08:00"):
        with pytest.raises(ConfigError, match="HH:MM-HH:MM"):
            Guardrails(
                {"pre": [{"block_outside_window": {"hours": bad, "tz": "Asia/Kolkata"}}]},
                agent=AGENT,
            )


def test_invalid_when_regex_fails_at_construction() -> None:
    with pytest.raises(ConfigError, match="invalid regex"):
        Guardrails(
            {"post": [{"require_disclaimer": {"when": "[unclosed", "text": "x"}}]}, agent=AGENT
        )


def test_string_topics_rejected() -> None:
    judge = FakeAdapter(script=[])
    with pytest.raises(ConfigError, match="LIST of topics"):
        Guardrails({"post": [{"never": "threats"}]}, agent=AGENT, judge=judge)


def test_topic_rule_without_judge_fails_at_construction() -> None:
    with pytest.raises(ConfigError, match="judge adapter"):
        Guardrails({"post": [{"never": ["threats"]}]}, agent=AGENT)


def test_from_file_rejects_non_mapping(tmp_path: Path) -> None:
    p = tmp_path / "policy.yaml"
    p.write_text("- just\n- a list\n")
    with pytest.raises(ConfigError, match="mapping"):
        Guardrails.from_file(p, agent=AGENT)


async def test_end_to_end_violation_escalates() -> None:
    """Guardrail attaches to the app bus; violation → BLOCKED_MESSAGE + ticket."""
    policy = {"post": [{"require_disclaimer": {"when": "mutual fund", "text": "Market risks."}}]}
    handover = CapturingHandover()
    app = AgentApp(
        {AGENT: make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[LLMResponse(text="Buy our mutual fund!")]),
        handover=handover,
    )
    Guardrails(policy, agent=AGENT).attach(app.bus)
    assert await app.run(AGENT, "s1", "what should I do?") == BLOCKED_MESSAGE
    assert handover.tickets[0].reason == "required disclaimer missing"
    assert handover.tickets[0].trigger == "Buy our mutual fund!"
