# Dashboard demo

A seeded, deterministic dataset for `zolva dashboard`: three agents
(collections, support, disputes), ~600 sessions over 14 days, ~4,000
hash-chained audit rows with tool calls, guardrail blocks, a cross-agent
handoff path, and human handovers.

```bash
pip install "zolva[dashboard]"
python examples/dashboard_demo/seed.py                # writes audit.sqlite here
zolva dashboard examples/dashboard_demo/agents \
  --audit examples/dashboard_demo/audit.sqlite
# open http://127.0.0.1:8600
```

`seed.py` takes an optional session count: `python examples/dashboard_demo/seed.py 2000`.

Everything is written through `AuditLog.append`, so the chain verifies and the
regular CLI works on the same file:

```bash
zolva scorecard examples/dashboard_demo/audit.sqlite
```

To watch the live tail move, keep the dashboard open and append fresh
sessions from a second terminal:

```bash
python examples/dashboard_demo/live.py
```
