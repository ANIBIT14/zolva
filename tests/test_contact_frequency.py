"""Customer identity + per-customer contact caps across sessions."""

import sqlite3
from pathlib import Path

import pytest

from tests.test_orchestrator import CapturingHandover, make_cfg
from zolva.bridge import LLMResponse
from zolva.bridge.fake import FakeAdapter
from zolva.bus import Bus, Step, Verdict
from zolva.channels import ChannelHub, FakeChannel
from zolva.config import ConfigError
from zolva.guardrails import Guardrails, validate_policy
from zolva.orchestrator import BLOCKED_MESSAGE, AgentApp
from zolva.tools import ToolRegistry

AGENT = "collections-agent"


def freq_policy(ledger: Path, max_contacts: int = 2, window_hours: float = 24) -> dict:  # type: ignore[type-arg]
    return {
        "post": [
            {
                "block_contact_frequency": {
                    "max_contacts": max_contacts,
                    "window_hours": window_hours,
                    "ledger": str(ledger),
                }
            }
        ]
    }


def make_app(n_replies: int, guard: Guardrails) -> tuple[AgentApp, CapturingHandover]:
    handover = CapturingHandover()
    app = AgentApp(
        {AGENT: make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[LLMResponse(text="Please pay soon.")] * n_replies),
        handover=handover,
    )
    guard.attach(app.bus)
    return app, handover


async def test_customer_ref_rides_on_user_msg_and_response_steps() -> None:
    seen: list[Step] = []
    bus = Bus()

    async def observe(step: Step) -> Verdict | None:
        seen.append(step)
        return None

    bus.on(observe)
    app = AgentApp(
        {AGENT: make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[LLMResponse(text="hi")]),
        bus=bus,
    )
    await app.run(AGENT, "s1", "hello", customer_ref="c-777")
    by_type = {s.type: s for s in seen}
    assert by_type["user_msg"].data["customer_ref"] == "c-777"
    assert by_type["response"].data["customer_ref"] == "c-777"
    # without a ref, the key is absent, not null
    await app.run(AGENT, "s2", "hello")
    assert "customer_ref" not in [s for s in seen if s.session_id == "s2"][0].data


async def test_cap_blocks_across_sessions_and_escalates(tmp_path: Path) -> None:
    guard = Guardrails(freq_policy(tmp_path / "contacts.sqlite", max_contacts=2), agent=AGENT)
    app, handover = make_app(3, guard)
    # two contacts to the same customer across DIFFERENT sessions: allowed
    assert await app.run(AGENT, "s1", "dues?", customer_ref="c-1") == "Please pay soon."
    assert await app.run(AGENT, "s2", "dues?", customer_ref="c-1") == "Please pay soon."
    # third within the window: blocked -> handover, never silence
    reply = await app.run(AGENT, "s3", "dues?", customer_ref="c-1")
    assert reply == BLOCKED_MESSAGE
    assert len(handover.tickets) == 1
    assert "contact frequency cap" in handover.tickets[0].reason


async def test_other_customers_unaffected_by_someones_cap(tmp_path: Path) -> None:
    guard = Guardrails(freq_policy(tmp_path / "contacts.sqlite", max_contacts=1), agent=AGENT)
    app, _ = make_app(2, guard)
    assert await app.run(AGENT, "s1", "dues?", customer_ref="c-1") == "Please pay soon."
    assert await app.run(AGENT, "s2", "dues?", customer_ref="c-2") == "Please pay soon."


async def test_no_customer_ref_skips_the_rule(tmp_path: Path) -> None:
    guard = Guardrails(freq_policy(tmp_path / "contacts.sqlite", max_contacts=1), agent=AGENT)
    app, handover = make_app(3, guard)
    for i in range(3):  # anonymous traffic is never capped
        assert await app.run(AGENT, f"s{i}", "dues?") == "Please pay soon."
    assert handover.tickets == []


async def test_contacts_outside_window_do_not_count(tmp_path: Path) -> None:
    ledger = tmp_path / "contacts.sqlite"
    guard = Guardrails(freq_policy(ledger, max_contacts=1, window_hours=1), agent=AGENT)
    app, _ = make_app(2, guard)
    assert await app.run(AGENT, "s1", "dues?", customer_ref="c-1") == "Please pay soon."
    # age the recorded contact beyond the window
    with sqlite3.connect(ledger) as conn:
        conn.execute("UPDATE contacts SET ts = '2020-01-01T00:00:00+00:00'")
    assert await app.run(AGENT, "s2", "dues?", customer_ref="c-1") == "Please pay soon."


async def test_channel_meta_carries_customer_ref(tmp_path: Path) -> None:
    guard = Guardrails(freq_policy(tmp_path / "contacts.sqlite", max_contacts=1), agent=AGENT)
    app, handover = make_app(2, guard)
    channel = FakeChannel()
    hub = ChannelHub(app, channels={"whatsapp": channel}, agents={AGENT: ["whatsapp"]})
    payload = {"session_id": "919876", "text": "dues?", "customer_ref": "c-9"}
    assert await hub.dispatch("whatsapp", AGENT, payload) == "Please pay soon."
    # same customer on a second inbound: capped, even though sessions differ
    payload2 = {"session_id": "other-session", "text": "dues?", "customer_ref": "c-9"}
    assert await hub.dispatch("whatsapp", AGENT, payload2) == BLOCKED_MESSAGE
    assert len(handover.tickets) == 1


def test_validate_policy_shapes(tmp_path: Path) -> None:
    validate_policy(freq_policy(tmp_path / "l.sqlite"))  # good shape passes
    for bad in [
        {
            "post": [
                {"block_contact_frequency": {"max_contacts": 0, "window_hours": 24, "ledger": "l"}}
            ]
        },
        {
            "post": [
                {"block_contact_frequency": {"max_contacts": 2, "window_hours": 0, "ledger": "l"}}
            ]
        },
        {"post": [{"block_contact_frequency": {"max_contacts": 2, "window_hours": 24}}]},
        {"post": [{"block_contact_frequency": "nope"}]},
    ]:
        with pytest.raises(ConfigError, match="block_contact_frequency"):
            validate_policy(bad)


def test_ledger_path_resolves_relative_to_policy_file(tmp_path: Path) -> None:
    (tmp_path / "policies").mkdir()
    policy = tmp_path / "policies" / "caps.yaml"
    policy.write_text(
        "post:\n"
        "  - block_contact_frequency: { max_contacts: 1, window_hours: 24, ledger: caps.sqlite }\n"
    )
    guard = Guardrails.from_file(policy, agent=AGENT)
    assert guard._base_dir == policy.parent
