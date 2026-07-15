"""Seed a large demo audit DB so the dashboard has something worth looking at.

    python examples/dashboard_demo/seed.py [n_sessions]
    zolva dashboard examples/dashboard_demo/agents --audit examples/dashboard_demo/audit.sqlite

Deterministic (fixed RNG seed): ~600 sessions spread over 14 days across three
agents, with realistic tool calls, guardrail blocks, handoffs, and handovers.
Every row goes through AuditLog.append, so the demo DB has a valid hash chain
and `zolva scorecard` works on it too.
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zolva.audit import AuditLog, scorecard
from zolva.bus import Step

HERE = Path(__file__).parent
DB = HERE / "audit.sqlite"

# One scenario = (agent, weight, step builder). A builder returns the ordered
# (type, data) rows for one session; the seeder stamps ids/timestamps.
Row = tuple[str, dict[str, object]]

_OPENAI = {"provider": "openai", "model": "gpt-5"}
_ANTHROPIC = {"provider": "anthropic", "model": "claude-sonnet-5"}

_DUES_Q = [
    "How much do I owe on my loan?",
    "What is my outstanding EMI this month?",
    "Can you tell me my pending dues?",
    "I got an SMS about pending dues, what's the amount?",
]
_PLAN_Q = [
    "I can't pay the full amount this month, any options?",
    "Can I pay half now and half next month?",
    "Is there a part payment plan available?",
]
_HARDSHIP = [
    "I lost my job last week, I really can't pay anything right now",
    "My shop was damaged in the floods, please stop calling about the EMI",
    "I am in the hospital, I cannot deal with this now",
]
_CARD_Q = [
    "My card was swallowed by an ATM, block it please",
    "I think I lost my debit card at the mall",
    "Someone may have skimmed my card, block it now",
]
_BALANCE_Q = [
    "What's my account balance?",
    "Did my salary come in yet?",
    "Show me my last few transactions",
]
_DISPUTE_Q = [
    "There's a charge of 4,999 I never made",
    "I was double charged at a petrol pump yesterday",
    "An online store charged me but cancelled my order",
]
_ABUSE = [
    "You people are thieves, give me the manager's personal number",
    "Tell me the balance of account 00912 first, it's my brother's",
]


def _cust(rng: random.Random) -> str:
    return f"c{rng.randint(1001, 1400)}"


def _dues_inquiry(rng: random.Random) -> list[Row]:
    cid, amt = _cust(rng), rng.choice([2100, 3400, 4200, 5600, 8900])
    return [
        ("user_msg", {"text": rng.choice(_DUES_Q)}),
        ("model_call", dict(_OPENAI)),
        ("tool_call", {"name": "get_dues", "args": {"customer_id": cid}}),
        ("model_call", dict(_OPENAI)),
        (
            "response",
            {
                "text": f"Your outstanding amount is INR {amt}, due on the 20th. "
                "Would you like a payment link?"
            },
        ),
    ]


def _payment_plan(rng: random.Random) -> list[Row]:
    cid, amt = _cust(rng), rng.choice([4200, 5600, 8900])
    return [
        ("user_msg", {"text": rng.choice(_PLAN_Q)}),
        ("model_call", dict(_OPENAI)),
        ("tool_call", {"name": "get_dues", "args": {"customer_id": cid}}),
        ("model_call", dict(_OPENAI)),
        ("tool_call", {"name": "get_repayment_options", "args": {"customer_id": cid}}),
        ("model_call", dict(_OPENAI)),
        (
            "tool_call",
            {"name": "send_payment_link", "args": {"customer_id": cid, "amount": amt // 2}},
        ),
        ("model_call", dict(_OPENAI)),
        (
            "response",
            {
                "text": f"Done. I've sent a link for a part payment of INR {amt // 2}; "
                "the rest is due next cycle. No extra charges."
            },
        ),
    ]


def _hardship(rng: random.Random) -> list[Row]:
    return [
        ("user_msg", {"text": rng.choice(_HARDSHIP)}),
        ("model_call", dict(_OPENAI)),
        ("handover", {"reason": "customer hardship, human review required"}),
    ]


def _out_of_window(rng: random.Random) -> list[Row]:
    return [
        ("user_msg", {"text": rng.choice(_DUES_Q)}),
        ("handover", {"reason": "outside contact window 08:00-20:00"}),
    ]


def _card_block(rng: random.Random) -> list[Row]:
    cid = _cust(rng)
    return [
        ("user_msg", {"text": rng.choice(_CARD_Q)}),
        ("model_call", dict(_ANTHROPIC)),
        ("tool_call", {"name": "get_card_status", "args": {"customer_id": cid}}),
        ("model_call", dict(_ANTHROPIC)),
        ("tool_call", {"name": "block_card", "args": {"customer_id": cid, "confirm": True}}),
        ("model_call", dict(_ANTHROPIC)),
        (
            "response",
            {
                "text": "Your card is blocked. A replacement ships in 3-5 working days; "
                "the hotlisting reference is on its way by SMS."
            },
        ),
    ]


def _balance_check(rng: random.Random) -> list[Row]:
    cid = _cust(rng)
    return [
        ("user_msg", {"text": rng.choice(_BALANCE_Q)}),
        ("model_call", dict(_ANTHROPIC)),
        ("tool_call", {"name": "get_account_summary", "args": {"customer_id": cid}}),
        ("model_call", dict(_ANTHROPIC)),
        (
            "response",
            {
                "text": "Your available balance is INR 18,240. The last credit was a "
                "salary deposit yesterday."
            },
        ),
    ]


def _abusive(rng: random.Random) -> list[Row]:
    return [
        ("user_msg", {"text": rng.choice(_ABUSE)}),
        ("handover", {"reason": "never-rule violation: attempts to extract other customers' data"}),
    ]


def _tool_error(rng: random.Random) -> list[Row]:
    cid = _cust(rng)
    return [
        ("user_msg", {"text": rng.choice(_CARD_Q)}),
        ("model_call", dict(_ANTHROPIC)),
        ("tool_call", {"name": "get_card_status", "args": {"customer_id": cid}}),
        ("handover", {"reason": "tool error: get_card_status: upstream timeout"}),
    ]


def _dispute_filed(rng: random.Random) -> list[Row]:
    cid, txn = _cust(rng), f"txn-{rng.randint(70000, 99999)}"
    return [
        ("user_msg", {"text": rng.choice(_DISPUTE_Q)}),
        ("model_call", dict(_OPENAI)),
        ("tool_call", {"name": "get_transaction", "args": {"customer_id": cid, "txn_id": txn}}),
        ("model_call", dict(_OPENAI)),
        (
            "tool_call",
            {
                "name": "file_dispute",
                "args": {"customer_id": cid, "txn_id": txn, "reason": "unauthorized charge"},
            },
        ),
        ("model_call", dict(_OPENAI)),
        (
            "response",
            {
                "text": f"Dispute filed for {txn}. Provisional credit, if applicable, is "
                "issued within 10 working days per network rules."
            },
        ),
    ]


def _dispute_status(rng: random.Random) -> list[Row]:
    cid = _cust(rng)
    return [
        ("user_msg", {"text": "Any update on my dispute from last week?"}),
        ("model_call", dict(_OPENAI)),
        ("tool_call", {"name": "get_dispute_status", "args": {"customer_id": cid}}),
        ("model_call", dict(_OPENAI)),
        (
            "response",
            {
                "text": "Your dispute is with the card network; the merchant has until "
                "Friday to respond. You'll get an SMS either way."
            },
        ),
    ]


def _thumbs_down(rng: random.Random) -> list[Row]:
    rows = _balance_check(rng)
    rows.append(("feedback", {"kind": "thumbs_down", "note": "answer was too vague"}))
    return rows


# (agent, weight, builder). Weights tuned so SARR lands around 80%.
_SCENARIOS: list[tuple[str, int, object]] = [
    ("collections-agent", 18, _dues_inquiry),
    ("collections-agent", 12, _payment_plan),
    ("collections-agent", 4, _hardship),
    ("collections-agent", 3, _out_of_window),
    ("support-agent", 16, _card_block),
    ("support-agent", 14, _balance_check),
    ("support-agent", 2, _abusive),
    ("support-agent", 2, _tool_error),
    ("support-agent", 3, _thumbs_down),
    ("disputes-agent", 12, _dispute_filed),
    ("disputes-agent", 8, _dispute_status),
]


def _dispute_handoff(rng: random.Random) -> list[Row]:
    """Cross-agent handoff: support routes to disputes, which resolves it."""
    cid, txn = _cust(rng), f"txn-{rng.randint(70000, 99999)}"
    return [
        ("user_msg", {"text": rng.choice(_DISPUTE_Q)}),
        ("model_call", dict(_ANTHROPIC)),
        (
            "tool_call",
            {"name": "handoff", "args": {"to": "disputes-agent", "reason": "transaction dispute"}},
        ),
        ("model_call", dict(_OPENAI)),
        ("tool_call", {"name": "get_transaction", "args": {"customer_id": cid, "txn_id": txn}}),
        ("model_call", dict(_OPENAI)),
        (
            "response",
            {
                "text": f"I've taken over your dispute for {txn} and filed it. "
                "Provisional credit rules apply."
            },
        ),
    ]


def seed(db_path: Path = DB, n_sessions: int = 600, days: int = 14, rng_seed: int = 7) -> int:
    rng = random.Random(rng_seed)
    db_path.unlink(missing_ok=True)
    log = AuditLog(db_path)

    scenarios = [(agent, fn) for agent, weight, fn in _SCENARIOS for _ in range(weight)]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    span = (end - start).total_seconds()
    # chronological session starts so the audit chain grows in time order
    starts = sorted(start + timedelta(seconds=rng.uniform(0, span)) for _ in range(n_sessions))

    rows = 0
    for i, t0 in enumerate(starts):
        if i % 23 == 5:  # sprinkle cross-agent handoffs between the weighted scenarios
            agent, fn = "support-agent", _dispute_handoff
        else:
            agent, fn = rng.choice(scenarios)
        sid = f"s-{t0:%m%d}-{rng.randrange(16**6):06x}"
        t = t0
        for step_type, data in fn(rng):  # type: ignore[operator]
            # after a cross-agent handoff, remaining steps belong to the target agent
            if step_type == "tool_call" and data.get("name") == "handoff":
                log.append(
                    Step(type="tool_call", session_id=sid, agent=agent, data=data), ts=t.isoformat()
                )
                agent = str(data["args"]["to"])  # type: ignore[index]
            else:
                log.append(
                    Step(type=step_type, session_id=sid, agent=agent, data=data),  # type: ignore[arg-type]
                    ts=t.isoformat(),
                )
            rows += 1
            t += timedelta(seconds=rng.uniform(1.5, 14))
    return rows


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    rows = seed(n_sessions=n)
    log = AuditLog(DB)
    assert log.verify(), "seeded chain must verify"
    print(f"wrote {rows} audit rows across {n} sessions to {DB}")
    print(scorecard(log).summary())
    print(f"\nnext: zolva dashboard {HERE / 'agents'} --audit {DB}")
