import threading

import pytest

from zolva.tools import ToolContractError, ToolRegistry


def test_register_and_call_sync() -> None:
    reg = ToolRegistry()

    @reg.register
    def get_dues(customer_id: str) -> dict[str, int]:
        """Fetch dues for a customer."""
        return {"amount": 4200}

    spec = reg.specs(["get_dues"])[0]
    assert spec.name == "get_dues"
    assert spec.description == "Fetch dues for a customer."
    assert "customer_id" in spec.parameters["properties"]


async def test_call_validates_and_executes() -> None:
    reg = ToolRegistry()

    @reg.register
    async def add(a: int, b: int) -> int:
        return a + b

    assert await reg.call("add", {"a": 2, "b": 3}) == 5


async def test_bad_args_raise_contract_error() -> None:
    reg = ToolRegistry()

    @reg.register
    def add(a: int, b: int) -> int:
        return a + b

    with pytest.raises(ToolContractError):
        await reg.call("add", {"a": "not-an-int-at-all", "c": 1})


async def test_unknown_tool_raises() -> None:
    reg = ToolRegistry()
    with pytest.raises(ToolContractError, match="unknown tool"):
        await reg.call("ghost", {})


def test_unknown_spec_name_raises() -> None:
    reg = ToolRegistry()
    with pytest.raises(ToolContractError, match="unknown tool"):
        reg.specs(["ghost"])


async def test_sync_tool_runs_off_event_loop() -> None:
    reg = ToolRegistry()

    @reg.register
    def on_main_thread() -> bool:
        """Check thread."""
        return threading.current_thread() is threading.main_thread()

    assert await reg.call("on_main_thread", {}) is False


async def test_async_tool_stays_on_loop() -> None:
    reg = ToolRegistry()

    @reg.register
    async def on_main_thread() -> bool:
        """Check thread."""
        return threading.current_thread() is threading.main_thread()

    assert await reg.call("on_main_thread", {}) is True


async def test_sync_tool_exception_propagates() -> None:
    reg = ToolRegistry()

    @reg.register
    def boom() -> None:
        """Raise."""
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await reg.call("boom", {})


def test_default_registry_decorator() -> None:
    from zolva.tools import default_registry, tool

    @tool
    def ping() -> str:
        return "pong"

    assert default_registry.specs(["ping"])[0].name == "ping"
