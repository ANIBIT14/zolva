# Zolva Dashboard — design

**Date:** 2026-07-15 · **Status:** implemented (v1)

## Problem

Teams running Zolva have no single place to see their interfaces: which agents
exist, which tools (their APIs) each agent can call, where handoffs go, and the
live flow of queries in and out. The data already exists — agent YAML configs
describe the static topology, and the hash-chained audit log records every bus
step — but only the CLI scorecard surfaces any of it.

## Decision

A read-only local web UI, `zolva dashboard`, served from the package as an
optional extra (`pip install "zolva[dashboard]"` → FastAPI + uvicorn). Chosen
over (a) an OTel/Grafana exporter — can come later, doesn't show config-derived
topology — and (b) a hosted dashboard, which contradicts the self-hosted/VPC
positioning.

Scope decision: **viewer + live tail**. No write paths (triage, eval runs), no
auth, no websockets in v1.

## Architecture

- `src/zolva/dashboard.py` — query functions over the audit DB (SQLite opened
  `mode=ro`, so the viewer can never touch the chain) + `create_app()` FastAPI
  factory + `serve()`. FastAPI imports are function-local so the module's query
  functions work on core installs.
- `src/zolva/dashboard.html` — one self-contained file, inline CSS/JS, no CDN,
  no webfonts; must render air-gapped. Ships inside the wheel.
- CLI: `zolva dashboard <config_dir> --audit <db> [--host] [--port]`, guarded
  by a friendly install hint when the extra is missing.

### Endpoints

| Endpoint | Source | Returns |
|---|---|---|
| `GET /api/topology` | `load_agents()` | agents, models, tools, handoffs, guardrail/eval refs |
| `GET /api/sessions?after_id&limit` | audit | session summaries with outcome (resolved/escalated/active) + `cursor` |
| `GET /api/sessions/{id}/steps` | audit | ordered step transcript, parsed payloads |
| `GET /api/stats` | audit | chain verify, SARR scorecard, step/tool/agent counts, handover reasons, steps/day |

### Live tail

The audit autoincrement id is the cursor. The page polls
`/api/sessions?after_id=<cursor>` every 2.5 s; the server returns only sessions
with rows past that id plus the new max id. Stateless, multi-viewer safe.

### Error handling

Missing DB → empty states, never file creation (read-only promise covered by a
test). Bad config dir → `{"agents": [], "error": ...}`, not a 500. Chain
tamper → red badge via `AuditLog.verify()` on every stats refresh.

## Demo

`examples/dashboard_demo/`: 3 agents + deterministic `seed.py` (~600 sessions /
14 days / ~3.5k rows written through `AuditLog.append` so the chain verifies)
and `live.py` to drive the live tail.

## Testing

`tests/test_dashboard.py`: TestClient over fixture configs + audit DB —
topology, cursor semantics, transcript ordering, stats/scorecard, tamper
detection, empty states, no-write guarantee, CLI wiring, seeder chain validity.

## Later

- OTel exporter plugin for teams with existing Grafana/Datadog.
- Auth story if demand for non-localhost exposure appears (v1: reverse proxy).
- Reimplement the four endpoints over Postgres audit stores when SQLite is
  outgrown.
