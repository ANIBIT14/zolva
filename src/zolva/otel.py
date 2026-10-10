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
        # OTel GenAI semantic conventions (still "Development" status upstream,
        # so every gen_ai.* name lives here, in one place, to track renames)
        attrs["gen_ai.conversation.id"] = step.session_id
        if step.type in ("model_call", "model_result"):
            attrs["gen_ai.operation.name"] = "chat"
            attrs["gen_ai.provider.name"] = str(step.data.get("provider", ""))[:_MAX_ATTR_LEN]
            attrs["gen_ai.request.model"] = str(step.data.get("model", ""))[:_MAX_ATTR_LEN]
        elif step.type in ("tool_call", "tool_result"):
            attrs["gen_ai.operation.name"] = "execute_tool"
            attrs["gen_ai.tool.name"] = str(step.data.get("name", ""))[:_MAX_ATTR_LEN]
        for key in ("input_tokens", "output_tokens"):
            if isinstance(step.data.get(key), int):
                attrs[f"gen_ai.usage.{key}"] = step.data[key]
        for key, value in step.data.items():
            if isinstance(value, (int, float)):  # bool is an int subclass, included
                attrs[f"zolva.{key}"] = value
            elif isinstance(value, str) and key in _SAFE_STR_KEYS:
                attrs[f"zolva.{key}"] = value[:_MAX_ATTR_LEN]
        return attrs
