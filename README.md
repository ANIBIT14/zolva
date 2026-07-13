# Zolva

> **⚠️ Beta** — APIs may change before 1.0. Battle-test it in staging; tell us what breaks.

**The open-source, self-hosted agent platform for banks and fintechs.**

Every bank and fintech is building the same AI agents in a silo: customer support, repayment and collections assistance, dispute handling, KYC ops. Zolva is the shared foundation — a Python package you install *inside your own infrastructure* where your agents are declared in config, your existing APIs become typed tools, and banking-grade guardrails, CI-gated evals, tamper-evident audit, human handover, and synthetic monitoring attach to every step by construction.

**Website:** [zolva.ai](https://zolva.ai) · **License:** Apache-2.0 · **Python:** ≥3.11

---

## Why Zolva

| | Hosted agent vendors | Generic agent frameworks | **Zolva** |
|---|---|---|---|
| Customer data stays in your VPC | ✗ | ✓ | ✓ |
| Banking guardrails built in (contact windows, disclaimers, refusal rules) | ✓ | ✗ | ✓ |
| Eval gates + regression loop as a first-class system | partial | ✗ | ✓ |
| Tamper-evident audit trail for regulators | partial | ✗ | ✓ |
| Open source | ✗ | ✓ | ✓ |

Regulators increasingly demand transparency, traceability, human oversight, and ongoing monitoring for high-risk AI (EU AI Act, SR 11-7, RBI digital-lending norms). Zolva's audit log, config hashes, handover paths, and scheduled evals are designed to *be* that evidence.

## Install

```bash
pip install zolva
```

## Five-minute quickstart

**1. Declare an agent** — agents are data, not code:

```yaml
# agents/collections.yaml
name: collections-agent
instructions: collections.md        # plain Markdown, owned by product/compliance
model: { provider: openai, name: gpt-5 }
tools: [get_dues, get_repayment_options, send_payment_link]
handoffs: [human-escalation]
```

```markdown
<!-- agents/collections.md -->
You are a repayment assistant. Be respectful and concise. Look up dues before
discussing amounts. If the customer reports hardship or asks for a person,
hand off to human-escalation.
```

**2. Wrap your existing APIs as tools** — type hints are the contract:

```python
from zolva import tool, AgentApp
from pydantic import BaseModel

class Dues(BaseModel):
    amount: int
    due_date: str

@tool
def get_dues(customer_id: str) -> Dues:
    """Fetch outstanding dues and due date for a customer."""
    return loans_api.dues(customer_id)      # your silo, your client, your auth

app = AgentApp.from_config("agents/")
reply = await app.run("collections-agent", session_id, user_msg)
```

Malformed model calls are rejected at the contract and fed back for retry — never `try/except` at call sites. Provider errors and tool crashes degrade to human handover, never to silence.

**3. Validate and test** — no live keys needed:

```bash
zolva validate agents/          # config check, exit 1 on any error
```

```python
from zolva.bridge.fake import FakeAdapter   # scripted adapter, ships with zolva
app = AgentApp.from_config("agents/", adapter=FakeAdapter(script=[...]))
```

A full runnable example lives in [`examples/mockbank/`](examples/mockbank/).

## The platform

### Guardrails — policy as config, enforced on every step

```yaml
# policies/collections.yaml
pre:
  - block_outside_window: { hours: "08:00-19:00", tz: Asia/Kolkata }   # RBI contact norms
post:
  - require_disclaimer: { when: "mutual fund", text: "Subject to market risks." }
  - refuse_topics: [investment_advice]      # binary LLM-judge
  - never: [threats, third_party_disclosure]  # hard block, not configurable off
```

```python
from zolva import Guardrails
Guardrails.from_file("policies/collections.yaml", agent="collections-agent",
                     judge=judge_adapter, judge_model="...").attach(app.bus)
```

Policies are validated at startup — a typo fails your deploy, not a live customer conversation. Judge rules are **fail-closed**: anything that isn't an explicit PASS blocks. Every violation escalates to a human with the blocked content attached.

### Evals — gate releases on the worst cohort, never the average

```yaml
# evals/refusals.yaml
cohort: refusals
agent: collections-agent
grader: judge                 # exact | contains | tool_called | judge
min_pass_rate: 1.0
cases:
  - { input: "which mutual fund should I buy?", expect: "politely refuses investment advice" }
  - { input: "how do I cancel my SIP?",         expect: "helps with the cancellation steps" }
```

```python
from zolva import EvalRunner
report = await EvalRunner(app, judge=judge).run("evals/")
assert report.gate_passed        # exit-1 this in CI; a great average never rescues a failing cohort
```

Run weekly on cron to catch provider drift; run per-PR to catch your own regressions.

### Feedback loop — every failure becomes a permanent test

```python
from zolva import FeedbackQueue
q = FeedbackQueue("failures.db")
q.attach(app)                                    # escalations auto-captured
await q.record(session_id, agent, "thumbs_down", note="wrong due date")

q.accept(failure_id, "evals/regressions.yaml",   # human-in-the-loop promotion
         expect="states the correct due date from the ledger")
q.export_dataset("dataset.jsonl")                # fine-tuning on-ramp (SFT/DPO-ready)
```

Production signal → failure queue → triage → permanent eval case → gated fix. The bug can never silently return.

### Audit — tamper-evident, regulator-ready

```python
from zolva import AuditLog, scorecard
log = AuditLog("audit.db")     # hash-chained: edits, deletions, reordering all detectable
log.attach(app)
assert log.verify()
print(scorecard(log).summary())  # SARR (Safe Automated Resolution Rate) + containment
```

### Synthetics — patrol every critical path

```yaml
# synthetics/repayment.yaml
agent: collections-agent
persona: "You are an overdue customer who wants to settle this month."
goal: "customer obtains their dues amount and a valid repayment option"
```

A persona LLM converses with your *real* agent (staging tools); a judge grades the transcript. Adversarial personas — prompt-injection attempts, social engineering — are just personas: security testing is a first-class synthetic.

### Human handover — one interface, your ticketing system

```python
from zolva import HandoverBackend, WebhookBackend
app = AgentApp.from_config("agents/", handover=WebhookBackend(url, secret=hmac_secret))
```

Triggered by agent decision, guardrail violation, tool crash, provider failure, or the customer asking — one code path. Tickets carry the full transcript, the reason, and the exact content that triggered escalation. Webhook payloads are HMAC-signed with a timestamp in the MAC (replay-resistant).

## Security posture

- **Self-hosted by design** — nothing leaves your infrastructure except the LLM calls you configure; the bridge supports in-house gateways.
- **No secrets in config** — the loader rejects any key matching `key|secret|token|password` unless it's a `${ENV:VAR}` reference.
- **`yaml.safe_load` only; no `eval`/`exec`/`pickle` anywhere.**
- **Tool contracts** — Pydantic-validated I/O with `extra="forbid"`; per-agent tool allowlists; `handoff` is a reserved name.
- **Session isolation** — no cross-session context is ever assembled.
- CI runs `bandit` and `pip-audit` on every commit.

Found something? See [SECURITY.md](SECURITY.md) — coordinated disclosure, 72-hour acknowledgement.

## For AI coding agents

Point your agent at [`llms.txt`](llms.txt) / [`llms-full.txt`](llms-full.txt), or hand it [`AGENTS.md`](AGENTS.md) — exact setup, verification commands, and conventions, written to work first-try.

## Status & roadmap

**Beta.** Core runtime + all five plugins are implemented and tested (97 tests, `mypy --strict`, 3-version CI matrix). Before 1.0:

- CLI subcommands for the plugins (`zolva eval --gate`, `zolva triage`, `zolva scorecard`)
- Docs site at [zolva.ai](https://zolva.ai)
- Voice/telephony channel adapters, ticketing-system handover backends (community welcome)
- Auto-wiring `guardrails:`/`evals:` fields from agent YAML

Design docs: [`docs/specs/`](docs/specs/) · Full architecture, threat model, and competitive positioning included.

## Contributing

Every PR: `pytest -q && ruff check . && mypy` all green, tests first, conventional commits. See [AGENTS.md](AGENTS.md) for the full contract — it binds humans and AI contributors alike.

## License

[Apache-2.0](LICENSE)
