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

## Formerly deferred, shipped 2026-07-16

- **Human loop**: `AgentApp.resume()` + `resume` bus step + HMAC-verified
  `POST /sessions/{agent}/resume` in `zolva serve`. A specific ticketing
  integration (Slack/Zendesk) still waits for the first pilot's choice;
  `HandoverBackend.resume()` remains the outbound notification seam.
- **Customer identity + contact caps**: `customer_ref` threads through
  `run()`/channels into user_msg + response steps; new guardrail
  `block_contact_frequency: {max_contacts, window_hours, ledger}` counts
  responses per customer across sessions in a sqlite ledger.
- **Audit storage + verify scaling**: `AuditStore` protocol (4 methods)
  with SQLite + in-memory implementations; `verify(incremental=True)`
  checkpointing with boundary re-hash and stay-red-once-broken semantics;
  dashboard uses incremental + periodic full cadence. A Postgres backend is
  a protocol implementation away, deliberately not shipped untested (no
  Postgres server available in this environment).

## Findings considered and rejected / already handled

- Adapter registration, startup tool validation, fail-closed bus, handover
  crash, audit index, validate parity: fixed in v0.3.2.
- Fixed post-0.4.0: aclose() lifecycle on every httpx owner plus
  AgentApp.aclose()/ChannelHub.aclose(); scorecard and feedback auto-capture
  now skip `eval-`/`synthetic-` sessions; `export_dataset(--redaction)`
  masks PII in training exports; a blocked channel step escalates to
  handover instead of replying silently.
- Dashboard auth: deliberately out of v1 (reverse-proxy guidance documented).
- Still open by design: session summarization for month-long threads
  (trigger: first long-thread pilot), ticketing connector (trigger: pilot's
  tool choice), Postgres AuditStore (trigger: multi-replica deployment +
  a real server to test against).
