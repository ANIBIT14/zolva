"""Tool registry: plain functions with Pydantic-enforced I/O contracts."""

from __future__ import annotations

import inspect
from typing import Any, Callable

from pydantic import BaseModel, ValidationError, create_model


class ToolContractError(Exception):
    """Tool call violated its contract (unknown tool or invalid args)."""


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for arguments


class _Tool:
    def __init__(self, fn: Callable[..., Any]) -> None:
        self.fn = fn
        sig = inspect.signature(fn)
        fields: dict[str, Any] = {}
        for pname, param in sig.parameters.items():
            annotation = (
                param.annotation if param.annotation is not inspect.Parameter.empty else Any
            )
            default = param.default if param.default is not inspect.Parameter.empty else ...
            fields[pname] = (annotation, default)
        self.params_model: type[BaseModel] = create_model(f"{fn.__name__}_params", **fields)
        self.spec = ToolSpec(
            name=fn.__name__,
            description=inspect.getdoc(fn) or fn.__name__,
            parameters=self.params_model.model_json_schema(),
        )


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, _Tool] = {}

    def register(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        self._tools[fn.__name__] = _Tool(fn)
        return fn

    def _get(self, name: str) -> _Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolContractError(f"unknown tool {name!r}") from None

    def specs(self, names: list[str]) -> list[ToolSpec]:
        return [self._get(n).spec for n in names]

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        t = self._get(name)
        try:
            params = t.params_model(**args)
        except (ValidationError, TypeError) as e:
            raise ToolContractError(f"{name}: invalid arguments: {e}") from e
        # getattr, not model_dump(): a deep dump would turn Pydantic-typed params into dicts
        result = t.fn(**{k: getattr(params, k) for k in t.params_model.model_fields})
        if inspect.isawaitable(result):
            result = await result
        return result


default_registry = ToolRegistry()
tool = default_registry.register
