"""OpenTelemetry export: every bus step becomes an OTel span, so Zolva drops
into a bank's existing observability stack (Datadog, Grafana, Langfuse, any
OTLP collector) instead of only its own dashboard.

Zolva depends on the OpenTelemetry **API** only and emits through the global
tracer — the idiomatic library split: the bank owns the SDK, exporter and
collector config. With no SDK installed the global tracer is a no-op, so an
`OTelExporter` that is attached but unconfigured costs almost nothing.

By default only metadata leaves the process (step type, agent, session, tool
and model names) — never message bodies — so customer content is not shipped
to a third-party observability backend. The full transcript stays in the audit
log, which is in-VPC by design.
"""

from __future__ import annotations

import logging
from importlib.util import find_spec
from typing import Any

from zolva.bus import Step, Verdict
from zolva.orchestrator import AgentApp

logger = logging.getLogger("zolva.otel")

# scalar keys safe to export as span attributes; message-body keys (text,
# content, input, reply, transcript…) are deliberately excluded
_SAFE_STR_KEYS = frozenset({"name", "model", "provider", "tool", "channel", "reason", "verdict"})
_MAX_ATTR_LEN = 256


class OTelExporter:
    def __init__(self, tracer: Any = None, *, service_name: str = "zolva") -> None:
        if tracer is None:
            if find_spec("opentelemetry") is None:
                raise RuntimeError(
                    'OTel export requires the optional extra: pip install "zolva[otel]" '
                    "(and configure an OpenTelemetry SDK/exporter in your app)"
                )
            from opentelemetry import trace

            tracer = trace.get_tracer(service_name)
        self._tracer = tracer
        self._attached = False

    def attach(self, app: AgentApp) -> None:
        if self._attached:
            return  # idempotent: a second attach must not double-emit every step
        self._attached = True
        app.bus.on(self._observe)

    async def _observe(self, step: Step) -> Verdict | None:
        # NEVER raise: the bus fails hooks closed (a raising hook blocks the
        # conversation). Observability must never take customer traffic down.
        try:
            span = self._tracer.start_span(f"zolva.{step.type}", attributes=self._attributes(step))
            span.end()
        except Exception:
            logger.exception(
                "otel export failed for %s step (session=%s)", step.type, step.session_id
            )
        return None

    def _attributes(self, step: Step) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "zolva.step.type": step.type,
            "gen_ai.agent.name": step.agent,
            "session.id": step.session_id,
        }
        for key, value in step.data.items():
            if isinstance(value, (int, float)):  # bool is an int subclass, included
                attrs[f"zolva.{key}"] = value
            elif isinstance(value, str) and key in _SAFE_STR_KEYS:
                attrs[f"zolva.{key}"] = value[:_MAX_ATTR_LEN]
        return attrs
