# Zolva

Open-source, self-hosted agent platform for banks and fintechs. Agents are
config, tools are your existing APIs, and guardrails/evals/audit attach to
every step. Runs entirely inside your infrastructure.

**Site:** [zolva.ai](https://zolva.ai) · **Spec:** [`docs/specs/2026-07-12-zolva-design.md`](docs/specs/2026-07-12-zolva-design.md)

## Quickstart

```bash
pip install -e ".[dev]"
zolva validate examples/mockbank/agents
pytest -q
```

Define an agent (`agents/collections.yaml` + `collections.md`), wrap your API:

```python
from zolva import tool, AgentApp

@tool
def get_dues(customer_id: str) -> dict[str, object]:
    """Fetch outstanding dues."""
    return loans_api.dues(customer_id)

app = AgentApp.from_config("agents/")
reply = await app.run("collections-agent", session_id, user_msg)
```

## Plugins (all included)

| Plugin | What it does |
|---|---|
| `zolva.guardrails` | Policy-as-YAML: contact windows, required disclaimers, judge-backed `refuse_topics`/`never` rules. Violations block and escalate; policies validate at startup. |
| `zolva.evals` | Golden datasets per agent; graders `exact`/`contains`/`tool_called`/`judge`; the gate fails on the **worst cohort**, never the average. |
| `zolva.feedback` | Failure queue (escalations auto-captured, thumbs-downs via `record()`); accepted failures become permanent eval cases; `export_dataset()` emits fine-tuning JSONL. |
| `zolva.audit` | Hash-chained append-only log of every step (`verify()` detects tampering) + SARR scorecard. |
| `zolva.synthetics` | Persona LLMs patrol critical paths against the real agent; a judge grades every transcript. |

```python
from zolva import Guardrails, EvalRunner, FeedbackQueue
from zolva.audit import AuditLog, scorecard

Guardrails.from_file("policies/collections.yaml", agent="collections-agent").attach(app.bus)
AuditLog("audit.db").attach(app)
report = await EvalRunner(app).run("evals/")   # report.gate_passed gates your CI
```

Status: v0.1 — core + all five plugins. Spec: [`docs/specs/`](docs/specs/).
