"""Provider resilience: bounded retry on 429/5xx, gateway config via ModelConfig."""

import asyncio

import httpx
import pytest

from tests.test_orchestrator import make_cfg
from zolva.bridge import BridgeError, LLMResponse, get_adapter, post_with_retry
from zolva.bridge.fake import FakeAdapter
from zolva.bridge.openai import OpenAIAdapter
from zolva.config import AgentConfig, ConfigError, ModelConfig
from zolva.orchestrator import AgentApp
from zolva.tools import ToolRegistry


def counting_transport(responses: list[httpx.Response], seen: list[int]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return responses[min(len(seen) - 1, len(responses) - 1)]

    return httpx.MockTransport(handler)


def recorded_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(d: float) -> None:
        delays.append(d)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return delays


async def test_429_then_200_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    delays = recorded_sleeps(monkeypatch)
    seen: list[int] = []
    client = httpx.AsyncClient(
        base_url="http://t",
        transport=counting_transport(
            [httpx.Response(429), httpx.Response(200, json={"ok": True})], seen
        ),
    )
    r = await post_with_retry(client, "/x", json_body={}, provider="test")
    assert r.status_code == 200 and len(seen) == 2
    assert delays == [0.5]  # first backoff step


async def test_500_exhausts_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded_sleeps(monkeypatch)
    seen: list[int] = []
    client = httpx.AsyncClient(
        base_url="http://t", transport=counting_transport([httpx.Response(500)], seen)
    )
    with pytest.raises(BridgeError, match="after 3 attempts"):
        await post_with_retry(client, "/x", json_body={}, provider="test")
    assert len(seen) == 3


async def test_400_fails_immediately_no_retry() -> None:
    seen: list[int] = []
    client = httpx.AsyncClient(
        base_url="http://t", transport=counting_transport([httpx.Response(400)], seen)
    )
    with pytest.raises(BridgeError, match="test"):
        await post_with_retry(client, "/x", json_body={}, provider="test")
    assert len(seen) == 1


async def test_retry_after_header_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    delays = recorded_sleeps(monkeypatch)
    seen: list[int] = []
    client = httpx.AsyncClient(
        base_url="http://t",
        transport=counting_transport(
            [
                httpx.Response(429, headers={"Retry-After": "2"}),
                httpx.Response(200, json={}),
            ],
            seen,
        ),
    )
    await post_with_retry(client, "/x", json_body={}, provider="test")
    assert delays == [2.0]


async def test_adapter_survives_transient_429(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded_sleeps(monkeypatch)
    seen: list[int] = []
    ok = {"choices": [{"message": {"content": "hello", "tool_calls": None}}]}
    a = OpenAIAdapter(
        api_key="sk-test",
        transport=counting_transport([httpx.Response(429), httpx.Response(200, json=ok)], seen),
    )
    resp = await a.complete(model="m", system="s", messages=[], tools=[])
    assert resp.text == "hello" and len(seen) == 2


def test_model_config_gateway_fields() -> None:
    m = ModelConfig(provider="openai", name="gpt-5", base_url="http://gw.local/v1", timeout=5)
    assert m.base_url == "http://gw.local/v1" and m.timeout == 5.0
    with pytest.raises(Exception, match="extra"):  # extra="forbid" regression guard
        ModelConfig(provider="openai", name="gpt-5", nope=1)  # type: ignore[call-arg]


def test_get_adapter_forwards_gateway_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    adapter = get_adapter("openai", base_url="http://gw.local/v1", timeout=5)
    assert str(adapter._client.base_url).startswith("http://gw.local/v1")  # type: ignore[attr-defined]


def test_orchestrator_pools_per_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    def cfg(name: str, base_url: str | None) -> AgentConfig:
        return make_cfg(
            name=name,
            tools=[],
            model=ModelConfig(provider="openai", name="gpt-5", base_url=base_url),
        )

    app = AgentApp(
        {"a": cfg("a", None), "b": cfg("b", "http://gw.local/v1")},
        registry=ToolRegistry(),
    )
    app._adapter_for(app._agents["a"])
    app._adapter_for(app._agents["b"])
    app._adapter_for(app._agents["a"])  # cache hit, not a third entry
    assert len(app._provider_adapters) == 2


def test_zero_arg_custom_factories_still_work() -> None:
    # a bank-registered factory that accepts no kwargs must not break when
    # the agent config uses only defaults (regression for the kwargs change)
    fake = FakeAdapter(script=[LLMResponse(text="ok")])
    from zolva.bridge import register_adapter

    register_adapter("legacy-test", lambda: fake)
    try:
        assert get_adapter("legacy-test") is fake
        with pytest.raises(TypeError):
            get_adapter("legacy-test", base_url="http://x")  # explicit override -> loud
    finally:
        from zolva.bridge import _ADAPTERS

        _ADAPTERS.pop("legacy-test", None)


def test_config_error_unknown_agent_unchanged() -> None:
    # vision guard: config validation behavior untouched by the new fields
    with pytest.raises(ConfigError):
        AgentApp({"a": make_cfg(tools=["ghost"])}, registry=ToolRegistry())
