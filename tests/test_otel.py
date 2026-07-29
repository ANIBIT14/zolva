"""OTel export: one span per step, metadata only (no message bodies), never raises."""

from types import SimpleNamespace

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from zolva.bus import Bus, Step
from zolva.otel import OTelExporter


def make() -> tuple[InMemorySpanExporter, OTelExporter]:
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    return exp, OTelExporter(tracer=provider.get_tracer("test"))


def step(step_type: str = "tool_call", **data: object) -> Step:
    return Step(type=step_type, session_id="s1", agent="collections", data=data)  # type: ignore[arg-type]


async def test_span_per_step_with_safe_attributes() -> None:
    exp, exporter = make()
    await exporter._observe(step("tool_call", name="get_dues", amount=500, customer_id="c1"))
    spans = exp.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert spans[0].name == "zolva.tool_call"
    assert attrs["zolva.step.type"] == "tool_call"
    assert attrs["gen_ai.agent.name"] == "collections"
    assert attrs["session.id"] == "s1"
    assert attrs["zolva.name"] == "get_dues"  # allowlisted string
    assert attrs["zolva.amount"] == 500  # numeric


async def test_message_bodies_are_not_exported() -> None:
    exp, exporter = make()
    await exporter._observe(step("response", text="your balance is 4200", customer_id="c1"))
    attrs = dict(exp.get_finished_spans()[0].attributes or {})
    assert not any(k.endswith(".text") or k.endswith(".customer_id") for k in attrs)
    assert all("4200" not in str(v) for v in attrs.values())


async def test_exporter_never_raises_on_broken_tracer() -> None:
    def boom(*a: object, **k: object) -> object:
        raise RuntimeError("collector down")

    exporter = OTelExporter(tracer=SimpleNamespace(start_span=boom))
    # bus fails hooks closed; an observability hook must swallow and return None
    assert await exporter._observe(step()) is None


async def test_attach_is_idempotent() -> None:
    exp, exporter = make()
    bus = Bus()
    app = SimpleNamespace(bus=bus)
    exporter.attach(app)
    exporter.attach(app)
    await bus.emit(step("user_msg", text="hi"))
    assert len(exp.get_finished_spans()) == 1
