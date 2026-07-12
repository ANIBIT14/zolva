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

Status: v0.1 core. Guardrails, evals, feedback loop, audit, synthetics ship as
plugins — see the spec.
