# Implementation Plans

`2026-07-12-core-runtime.md` is the original core implementation plan, kept
for history.

## Executed and removed (2026-07-16)

Three advisor plans were executed the same day they were written; the plan
files were removed after completion per project practice. What shipped:

| Plan | Shipped as |
|------|------------|
| 001 PII redaction | `src/zolva/redaction.py` + `AgentApp.from_config(..., redaction=...)`; provider-bound text masked, sessions/audit/handover keep the true transcript |
| 002 `zolva serve` | `src/zolva/serve.py` + CLI `serve`: HMAC-verified `POST /channels/{channel}/{agent}`, `/healthz`; reference entrypoint behind the `[dashboard]` extra |
| 003 Provider resilience | `post_with_retry` (429/5xx/transport backoff, Retry-After) in both adapters; `ModelConfig.base_url`/`timeout` for in-house gateways; per-gateway connection pools |

## Direction items deferred (with trigger conditions)

- **Close the human loop (ticketing backend + resume path)** — `resume()` is
  declared and never called; do after a first pilot picks its ticketing tool.
- **Customer identity + contact-frequency guardrails** — needs a
  `customer_ref` design decision; plan when a collections pilot is concrete.
- **Postgres storage + incremental audit verify** — pull-driven; start with
  interface extraction when a multi-replica deployment is scheduled.

## Findings considered and rejected / already handled

- Adapter registration, startup tool validation, fail-closed bus, handover
  crash, audit index, validate parity: fixed in v0.3.2.
- Unclosed httpx clients: LOW; revisit with a lifecycle/close API if a
  long-running embedder reports leaks.
- Eval/synthetic session pollution of scorecard/feedback: fix
  opportunistically by filtering `eval-`/`synthetic-` session prefixes.
- Dashboard auth: deliberately out of v1 (reverse-proxy guidance documented).
