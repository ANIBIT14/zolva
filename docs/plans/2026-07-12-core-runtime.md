# Zolva Core Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `zolva` v0.1 core: declarative agents from YAML/MD config, typed tool registry, vendor-neutral LLM bridge (OpenAI + Anthropic + Fake), middleware bus, session stores, human handover, orchestrator loop with handoffs, `zolva validate` CLI, and the mockbank example proving it end-to-end.

**Architecture:** Small core, plugin-ready. Everything observable/blockable flows through a middleware `Bus` (the plugin attachment point). Tools are plain Python functions with Pydantic-enforced contracts. Agents are data (YAML + Markdown), never code. Spec: `docs/specs/2026-07-12-bank-agent-platform-design.md`.

**Tech Stack:** Python ≥3.11, pydantic v2, httpx, PyYAML, pytest + pytest-asyncio, ruff, mypy --strict, hatchling.

## Global Constraints

- Python `>=3.11`; CI matrix 3.11/3.12/3.13.
- Runtime deps ONLY: `pydantic>=2.7`, `httpx>=0.27`, `pyyaml>=6.0`. No new deps without spec change.
- `mypy --strict` clean; `ruff check` + `ruff format --check` clean — every task ends with both.
- YAML loaded with `yaml.safe_load` ONLY. No `eval`/`exec`/`pickle` anywhere.
- No secrets in config files: only `${ENV:VAR}` references (Task 2 enforces).
- All public models are pydantic `BaseModel` with `extra="forbid"` on config-facing models.
- Every commit message: conventional commits (`feat:`, `test:`, `chore:`).
- TDD: failing test first, minimal implementation, green, commit.

## File Structure

```
zolva/
├── pyproject.toml
├── .github/workflows/ci.yml
├── src/zolva/
│   ├── __init__.py          # public API re-exports
│   ├── config.py            # ModelConfig, AgentConfig, load_agents, ConfigError
│   ├── tools.py             # @tool, ToolRegistry, ToolSpec, ToolContractError
│   ├── bus.py               # Step, Verdict, Bus
│   ├── sessions.py          # SessionStore, InMemorySessionStore, SqliteSessionStore
│   ├── handover.py          # Ticket, HandoverRef, HandoverBackend, LogBackend, WebhookBackend
│   ├── orchestrator.py      # AgentApp
│   ├── cli.py               # zolva validate
│   └── bridge/
│       ├── __init__.py      # Message, ToolCall, LLMResponse, LLMAdapter, adapter registry, BridgeError
│       ├── fake.py          # FakeAdapter (shipped — banks use it in their own tests)
│       ├── openai.py        # OpenAIAdapter
│       └── anthropic.py     # AnthropicAdapter
├── tests/                   # mirrors src: test_config.py, test_tools.py, ...
└── examples/mockbank/
    ├── agents/collections.yaml, collections.md
    ├── bank.py              # mock loans API + @tool functions
    └── demo.py              # scripted end-to-end run (also the integration test's engine)
```

---

### Task 1: Package scaffold + quality gates

**Files:**
- Create: `pyproject.toml`, `src/zolva/__init__.py`, `tests/test_package.py`, `.github/workflows/ci.yml`, `.gitignore`

**Interfaces:**
- Produces: importable `zolva` package with `__version__: str`; `pytest`, `ruff`, `mypy` all runnable and green.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_package.py
import zolva


def test_version() -> None:
    assert zolva.__version__ == "0.1.0"
```

- [ ] **Step 2: Run it — expect fail**

Run: `cd ~/Work/zolva && pytest tests/test_package.py -v` → FAIL (`ModuleNotFoundError: zolva`)

- [ ] **Step 3: Create the scaffold**

```toml
# pyproject.toml
[project]
name = "zolva"
version = "0.1.0"
description = "Open-source, self-hosted agent platform for banks and fintechs"
requires-python = ">=3.11"
license = "Apache-2.0"
dependencies = ["pydantic>=2.7", "httpx>=0.27", "pyyaml>=6.0"]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "ruff>=0.4", "mypy>=1.10", "types-PyYAML"]

[project.scripts]
zolva = "zolva.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/zolva"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.mypy]
strict = true
packages = ["zolva"]
mypy_path = "src"

[tool.ruff]
line-length = 100
src = ["src", "tests"]
```

```python
# src/zolva/__init__.py
"""Zolva: self-hosted agent platform for banks and fintechs."""

__version__ = "0.1.0"
```

```gitignore
# .gitignore
__pycache__/
*.egg-info/
.venv/
dist/
.mypy_cache/
.ruff_cache/
.pytest_cache/
```

```yaml
# .github/workflows/ci.yml
name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python: ["3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "${{ matrix.python }}" }
      - run: pip install -e ".[dev]"
      - run: ruff check . && ruff format --check .
      - run: mypy
      - run: pytest -q
```

- [ ] **Step 4: Install and verify green**

Run: `python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`
Run: `pytest -q && ruff check . && mypy` → all PASS (1 test)

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "chore: package scaffold with ruff/mypy/pytest/CI gates"
```

---

### Task 2: Config models + loader

**Files:**
- Create: `src/zolva/config.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `class ConfigError(Exception)`
  - `class ModelConfig(BaseModel)`: `provider: str`, `name: str`
  - `class AgentConfig(BaseModel)`: `name: str`, `instructions: str` (resolved MD content), `model: ModelConfig`, `tools: list[str] = []`, `handoffs: list[str] = []`, `guardrails: str | None = None`, `evals: str | None = None`
  - `def load_agents(config_dir: str | Path) -> dict[str, AgentConfig]` — `instructions:` in YAML is a path **relative to the YAML file**; `${ENV:VAR}` strings resolved from env; inline credentials rejected; unknown YAML keys rejected; handoff targets must exist or be `"human-escalation"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
from pathlib import Path

import pytest

from zolva.config import AgentConfig, ConfigError, load_agents


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def make_agent_dir(tmp_path: Path) -> Path:
    write(tmp_path / "agents" / "collections.md", "You are a collections agent.")
    write(
        tmp_path / "agents" / "collections.yaml",
        """
name: collections-agent
instructions: collections.md
model: { provider: openai, name: gpt-5 }
tools: [get_dues]
handoffs: [human-escalation]
""",
    )
    return tmp_path / "agents"


def test_loads_agent_with_resolved_instructions(tmp_path: Path) -> None:
    agents = load_agents(make_agent_dir(tmp_path))
    cfg = agents["collections-agent"]
    assert isinstance(cfg, AgentConfig)
    assert cfg.instructions == "You are a collections agent."
    assert cfg.model.provider == "openai"
    assert cfg.tools == ["get_dues"]


def test_env_ref_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_MODEL", "gpt-5")
    write(tmp_path / "a" / "a.md", "x")
    write(
        tmp_path / "a" / "a.yaml",
        'name: a\ninstructions: a.md\nmodel: { provider: openai, name: "${ENV:MY_MODEL}" }\n',
    )
    assert load_agents(tmp_path / "a")["a"].model.name == "gpt-5"


def test_inline_credential_rejected(tmp_path: Path) -> None:
    write(tmp_path / "a" / "a.md", "x")
    write(
        tmp_path / "a" / "a.yaml",
        "name: a\ninstructions: a.md\napi_key: sk-123\nmodel: { provider: openai, name: g }\n",
    )
    with pytest.raises(ConfigError, match="inline credential"):
        load_agents(tmp_path / "a")


def test_unknown_key_rejected(tmp_path: Path) -> None:
    write(tmp_path / "a" / "a.md", "x")
    write(
        tmp_path / "a" / "a.yaml",
        "name: a\ninstructions: a.md\nbogus: 1\nmodel: { provider: openai, name: g }\n",
    )
    with pytest.raises(ConfigError):
        load_agents(tmp_path / "a")


def test_unknown_handoff_target_rejected(tmp_path: Path) -> None:
    write(tmp_path / "a" / "a.md", "x")
    write(
        tmp_path / "a" / "a.yaml",
        "name: a\ninstructions: a.md\nhandoffs: [ghost]\nmodel: { provider: openai, name: g }\n",
    )
    with pytest.raises(ConfigError, match="unknown agent"):
        load_agents(tmp_path / "a")


def test_missing_env_var_is_clear_error(tmp_path: Path) -> None:
    write(tmp_path / "a" / "a.md", "x")
    write(
        tmp_path / "a" / "a.yaml",
        'name: a\ninstructions: a.md\nmodel: { provider: openai, name: "${ENV:NOPE_UNSET}" }\n',
    )
    with pytest.raises(ConfigError, match="NOPE_UNSET"):
        load_agents(tmp_path / "a")
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest tests/test_config.py -v` → FAIL (`ModuleNotFoundError: zolva.config`)

- [ ] **Step 3: Implement**

```python
# src/zolva/config.py
"""Agent configuration: YAML + Markdown instructions, validated and secret-safe."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

_ENV_REF = re.compile(r"^\$\{ENV:([A-Za-z0-9_]+)\}$")
_SECRET_KEY = re.compile(r"key|secret|token|password", re.IGNORECASE)


class ConfigError(Exception):
    """Invalid agent configuration."""


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    name: str


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    instructions: str  # resolved markdown content, not a path
    model: ModelConfig
    tools: list[str] = []
    handoffs: list[str] = []
    guardrails: str | None = None
    evals: str | None = None


def _resolve(value: Any, key: str = "") -> Any:
    """Resolve ${ENV:VAR} references; reject inline credentials at trust boundary."""
    if isinstance(value, dict):
        return {k: _resolve(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, key) for v in value]
    if isinstance(value, str):
        m = _ENV_REF.match(value)
        if m:
            var = m.group(1)
            if var not in os.environ:
                raise ConfigError(f"env var {var} not set (referenced as ${{ENV:{var}}})")
            return os.environ[var]
        if _SECRET_KEY.search(key):
            raise ConfigError(f"inline credential in config key {key!r}; use ${{ENV:VAR}} instead")
    return value


def load_agents(config_dir: str | Path) -> dict[str, AgentConfig]:
    """Load every *.yaml agent in config_dir. instructions: path is relative to the YAML file."""
    root = Path(config_dir)
    agents: dict[str, AgentConfig] = {}
    for path in sorted(root.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: top level must be a mapping")
        raw = _resolve(raw)
        ins = raw.get("instructions")
        if not isinstance(ins, str):
            raise ConfigError(f"{path}: 'instructions' must be a path string")
        ins_path = path.parent / ins
        if not ins_path.is_file():
            raise ConfigError(f"{path}: instructions file not found: {ins_path}")
        raw["instructions"] = ins_path.read_text()
        try:
            cfg = AgentConfig(**raw)
        except ValidationError as e:
            raise ConfigError(f"{path}: {e}") from e
        agents[cfg.name] = cfg
    for cfg in agents.values():
        for target in cfg.handoffs:
            if target != "human-escalation" and target not in agents:
                raise ConfigError(f"agent {cfg.name!r} hands off to unknown agent {target!r}")
    return agents
```

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_config.py -v && ruff check . && mypy` → all PASS

- [ ] **Step 5: Commit**

```bash
git add src/zolva/config.py tests/test_config.py
git commit -m "feat: agent config loader with env-ref secrets and validation"
```

---

### Task 3: Tool registry with Pydantic contracts

**Files:**
- Create: `src/zolva/tools.py`, `tests/test_tools.py`

**Interfaces:**
- Produces:
  - `class ToolContractError(Exception)`
  - `class ToolSpec(BaseModel)`: `name: str`, `description: str`, `parameters: dict[str, Any]` (JSON Schema)
  - `class ToolRegistry`: `.register(fn: Callable[..., Any]) -> Callable[..., Any]`, `.specs(names: list[str]) -> list[ToolSpec]` (raises `ToolContractError` for unknown name), `async .call(name: str, args: dict[str, Any]) -> Any` (validates args, awaits async fns, raises `ToolContractError` on unknown tool / bad args)
  - `default_registry: ToolRegistry`; `def tool(fn)` — decorator registering into `default_registry`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tools.py
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


def test_default_registry_decorator() -> None:
    from zolva.tools import default_registry, tool

    @tool
    def ping() -> str:
        return "pong"

    assert default_registry.specs(["ping"])[0].name == "ping"
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest tests/test_tools.py -v` → FAIL (`ModuleNotFoundError: zolva.tools`)

- [ ] **Step 3: Implement**

```python
# src/zolva/tools.py
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
        hints = inspect.signature(fn)
        fields: dict[str, Any] = {}
        for pname, param in hints.parameters.items():
            annotation = param.annotation if param.annotation is not inspect.Parameter.empty else Any
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
        result = t.fn(**params.model_dump())
        if inspect.isawaitable(result):
            result = await result
        return result


default_registry = ToolRegistry()
tool = default_registry.register
```

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_tools.py -v && ruff check . && mypy` → all PASS

- [ ] **Step 5: Commit**

```bash
git add src/zolva/tools.py tests/test_tools.py
git commit -m "feat: tool registry with Pydantic contracts (schema-aware resolver)"
```

---

### Task 4: Bridge types + FakeAdapter + adapter registry

**Files:**
- Create: `src/zolva/bridge/__init__.py`, `src/zolva/bridge/fake.py`, `tests/test_bridge.py`

**Interfaces:**
- Produces:
  - `class BridgeError(Exception)`
  - `class ToolCall(BaseModel)`: `id: str`, `name: str`, `args: dict[str, Any]`
  - `class Message(BaseModel)`: `role: Literal["system","user","assistant","tool"]`, `content: str`, `tool_call_id: str | None = None`, `tool_calls: list[ToolCall] = []`
  - `class LLMResponse(BaseModel)`: `text: str = ""`, `tool_calls: list[ToolCall] = []`
  - `class LLMAdapter(Protocol)`: `async def complete(self, *, model: str, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse`
  - `def register_adapter(provider: str, factory: Callable[[], LLMAdapter]) -> None`; `def get_adapter(provider: str) -> LLMAdapter` (raises `BridgeError` for unknown provider)
  - `class FakeAdapter` (in `zolva.bridge.fake`): `FakeAdapter(script: list[LLMResponse])` — returns scripted responses in order; records every `complete()` call in `.calls: list[dict[str, Any]]`; raises `BridgeError` when script exhausted.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bridge.py
import pytest

from zolva.bridge import (
    BridgeError,
    LLMResponse,
    Message,
    ToolCall,
    get_adapter,
    register_adapter,
)
from zolva.bridge.fake import FakeAdapter


async def test_fake_adapter_plays_script_and_records() -> None:
    fake = FakeAdapter(
        script=[LLMResponse(tool_calls=[ToolCall(id="1", name="get_dues", args={"customer_id": "c1"})])]
    )
    resp = await fake.complete(
        model="m", system="s", messages=[Message(role="user", content="hi")], tools=[]
    )
    assert resp.tool_calls[0].name == "get_dues"
    assert fake.calls[0]["model"] == "m"


async def test_fake_adapter_exhausted_script_raises() -> None:
    fake = FakeAdapter(script=[])
    with pytest.raises(BridgeError, match="script exhausted"):
        await fake.complete(model="m", system="s", messages=[], tools=[])


def test_adapter_registry_roundtrip() -> None:
    fake = FakeAdapter(script=[])
    register_adapter("test-provider", lambda: fake)
    assert get_adapter("test-provider") is fake


def test_unknown_provider_raises() -> None:
    with pytest.raises(BridgeError, match="unknown provider"):
        get_adapter("nope-provider")
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest tests/test_bridge.py -v` → FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement**

```python
# src/zolva/bridge/__init__.py
"""Vendor-neutral LLM bridge: one Protocol, one adapter per provider."""

from __future__ import annotations

from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel

from zolva.tools import ToolSpec


class BridgeError(Exception):
    """LLM provider or adapter failure."""


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict[str, Any]


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = []


class LLMResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = []


class LLMAdapter(Protocol):
    async def complete(
        self, *, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse: ...


_ADAPTERS: dict[str, Callable[[], LLMAdapter]] = {}


def register_adapter(provider: str, factory: Callable[[], LLMAdapter]) -> None:
    _ADAPTERS[provider] = factory


def get_adapter(provider: str) -> LLMAdapter:
    try:
        return _ADAPTERS[provider]()
    except KeyError:
        raise BridgeError(f"unknown provider {provider!r}") from None
```

```python
# src/zolva/bridge/fake.py
"""Scripted adapter for tests and offline development. Shipped on purpose."""

from __future__ import annotations

from typing import Any

from zolva.bridge import BridgeError, LLMResponse, Message
from zolva.tools import ToolSpec


class FakeAdapter:
    def __init__(self, script: list[LLMResponse]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, *, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse:
        self.calls.append(
            {"model": model, "system": system, "messages": messages, "tools": tools}
        )
        if not self._script:
            raise BridgeError("FakeAdapter script exhausted")
        return self._script.pop(0)
```

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_bridge.py -v && ruff check . && mypy` → all PASS

- [ ] **Step 5: Commit**

```bash
git add src/zolva/bridge/ tests/test_bridge.py
git commit -m "feat: LLM bridge types, adapter registry, FakeAdapter"
```

---

### Task 5: OpenAI adapter

**Files:**
- Create: `src/zolva/bridge/openai.py`, `tests/test_openai_adapter.py`

**Interfaces:**
- Consumes: `Message`, `ToolCall`, `LLMResponse`, `BridgeError`, `ToolSpec` from Task 4/3.
- Produces: `class OpenAIAdapter`: `__init__(self, api_key: str | None = None, base_url: str = "https://api.openai.com/v1", transport: httpx.AsyncBaseTransport | None = None)` — key falls back to `OPENAI_API_KEY` env (raises `BridgeError` if absent); implements `LLMAdapter.complete`. Registered as provider `"openai"` on import via `register_adapter`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_openai_adapter.py
import json
from typing import Any

import httpx
import pytest

from zolva.bridge import BridgeError, Message
from zolva.bridge.openai import OpenAIAdapter
from zolva.tools import ToolSpec

TOOL = ToolSpec(name="get_dues", description="d", parameters={"type": "object", "properties": {}})


def transport(payload: dict[str, Any], capture: dict[str, Any]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        capture["body"] = json.loads(request.content)
        capture["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


async def test_sends_wire_format_and_parses_text() -> None:
    cap: dict[str, Any] = {}
    payload = {"choices": [{"message": {"content": "hello", "tool_calls": None}}]}
    a = OpenAIAdapter(api_key="sk-test", transport=transport(payload, cap))
    resp = await a.complete(
        model="gpt-5", system="be nice", messages=[Message(role="user", content="hi")], tools=[TOOL]
    )
    assert resp.text == "hello" and resp.tool_calls == []
    assert cap["auth"] == "Bearer sk-test"
    assert cap["body"]["messages"][0] == {"role": "system", "content": "be nice"}
    assert cap["body"]["tools"][0]["function"]["name"] == "get_dues"


async def test_parses_tool_calls() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "get_dues", "arguments": '{"customer_id": "c1"}'},
                        }
                    ],
                }
            }
        ]
    }
    a = OpenAIAdapter(api_key="k", transport=transport(payload, {}))
    resp = await a.complete(model="m", system="s", messages=[], tools=[TOOL])
    assert resp.tool_calls[0].name == "get_dues"
    assert resp.tool_calls[0].args == {"customer_id": "c1"}


async def test_http_error_wrapped_as_bridge_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    a = OpenAIAdapter(api_key="k", transport=httpx.MockTransport(handler))
    with pytest.raises(BridgeError):
        await a.complete(model="m", system="s", messages=[], tools=[])


def test_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(BridgeError, match="OPENAI_API_KEY"):
        OpenAIAdapter()
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest tests/test_openai_adapter.py -v` → FAIL

- [ ] **Step 3: Implement**

```python
# src/zolva/bridge/openai.py
"""OpenAI chat-completions adapter."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from zolva.bridge import BridgeError, LLMResponse, Message, ToolCall, register_adapter
from zolva.tools import ToolSpec


class OpenAIAdapter:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise BridgeError("OPENAI_API_KEY not set and no api_key given")
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {key}"},
            transport=transport,
            timeout=60.0,
        )

    def _wire_messages(self, system: str, messages: list[Message]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            item: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.role == "tool":
                item["tool_call_id"] = m.tool_call_id
            if m.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
                    }
                    for tc in m.tool_calls
                ]
            wire.append(item)
        return wire

    async def complete(
        self, *, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse:
        body: dict[str, Any] = {"model": model, "messages": self._wire_messages(system, messages)}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in tools
            ]
        try:
            r = await self._client.post("/chat/completions", json=body)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise BridgeError(f"openai: {e}") from e
        msg = r.json()["choices"][0]["message"]
        calls = [
            ToolCall(id=tc["id"], name=tc["function"]["name"], args=json.loads(tc["function"]["arguments"]))
            for tc in (msg.get("tool_calls") or [])
        ]
        return LLMResponse(text=msg.get("content") or "", tool_calls=calls)


register_adapter("openai", OpenAIAdapter)
```

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_openai_adapter.py -v && ruff check . && mypy` → all PASS

- [ ] **Step 5: Commit**

```bash
git add src/zolva/bridge/openai.py tests/test_openai_adapter.py
git commit -m "feat: OpenAI adapter"
```

---

### Task 6: Anthropic adapter

**Files:**
- Create: `src/zolva/bridge/anthropic.py`, `tests/test_anthropic_adapter.py`

**Interfaces:**
- Consumes: Task 4 types.
- Produces: `class AnthropicAdapter`: `__init__(self, api_key: str | None = None, base_url: str = "https://api.anthropic.com", transport: httpx.AsyncBaseTransport | None = None)` — key falls back to `ANTHROPIC_API_KEY`; registered as provider `"anthropic"`. Mapping: `tool` role messages become `user` messages with a `tool_result` content block; assistant `tool_calls` become `tool_use` blocks.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_anthropic_adapter.py
import json
from typing import Any

import httpx
import pytest

from zolva.bridge import BridgeError, Message, ToolCall
from zolva.bridge.anthropic import AnthropicAdapter
from zolva.tools import ToolSpec

TOOL = ToolSpec(name="get_dues", description="d", parameters={"type": "object", "properties": {}})


def transport(payload: dict[str, Any], capture: dict[str, Any]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        capture["body"] = json.loads(request.content)
        capture["key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


async def test_wire_format_and_text_parse() -> None:
    cap: dict[str, Any] = {}
    payload = {"content": [{"type": "text", "text": "hello"}]}
    a = AnthropicAdapter(api_key="ak", transport=transport(payload, cap))
    resp = await a.complete(
        model="claude-fable-5", system="s", messages=[Message(role="user", content="hi")], tools=[TOOL]
    )
    assert resp.text == "hello"
    assert cap["key"] == "ak"
    assert cap["body"]["system"] == "s"
    assert cap["body"]["tools"][0]["input_schema"] == TOOL.parameters


async def test_tool_use_parse_and_tool_result_mapping() -> None:
    cap: dict[str, Any] = {}
    payload = {
        "content": [{"type": "tool_use", "id": "tu_1", "name": "get_dues", "input": {"customer_id": "c1"}}]
    }
    a = AnthropicAdapter(api_key="ak", transport=transport(payload, cap))
    history = [
        Message(role="user", content="dues?"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="tu_0", name="get_dues", args={"customer_id": "c1"})],
        ),
        Message(role="tool", content='{"amount": 4200}', tool_call_id="tu_0"),
    ]
    resp = await a.complete(model="m", system="s", messages=history, tools=[TOOL])
    assert resp.tool_calls[0].id == "tu_1"
    wire = cap["body"]["messages"]
    assert wire[1]["content"][0]["type"] == "tool_use"
    assert wire[2]["role"] == "user"
    assert wire[2]["content"][0]["type"] == "tool_result"
    assert wire[2]["content"][0]["tool_use_id"] == "tu_0"


def test_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(BridgeError, match="ANTHROPIC_API_KEY"):
        AnthropicAdapter()
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest tests/test_anthropic_adapter.py -v` → FAIL

- [ ] **Step 3: Implement**

```python
# src/zolva/bridge/anthropic.py
"""Anthropic messages adapter."""

from __future__ import annotations

import os
from typing import Any

import httpx

from zolva.bridge import BridgeError, LLMResponse, Message, ToolCall, register_adapter
from zolva.tools import ToolSpec


class AnthropicAdapter:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise BridgeError("ANTHROPIC_API_KEY not set and no api_key given")
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            transport=transport,
            timeout=60.0,
        )

    def _wire_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool":
                wire.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                        ],
                    }
                )
            elif m.role == "assistant" and m.tool_calls:
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                blocks += [
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args}
                    for tc in m.tool_calls
                ]
                wire.append({"role": "assistant", "content": blocks})
            else:
                wire.append({"role": m.role, "content": m.content})
        return wire

    async def complete(
        self, *, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": 4096,
            "system": system,
            "messages": self._wire_messages(messages),
        }
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        try:
            r = await self._client.post("/v1/messages", json=body)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise BridgeError(f"anthropic: {e}") from e
        text, calls = "", []
        for block in r.json()["content"]:
            if block["type"] == "text":
                text += block["text"]
            elif block["type"] == "tool_use":
                calls.append(ToolCall(id=block["id"], name=block["name"], args=block["input"]))
        return LLMResponse(text=text, tool_calls=calls)


register_adapter("anthropic", AnthropicAdapter)
```

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_anthropic_adapter.py -v && ruff check . && mypy` → all PASS

- [ ] **Step 5: Commit**

```bash
git add src/zolva/bridge/anthropic.py tests/test_anthropic_adapter.py
git commit -m "feat: Anthropic adapter"
```

---

### Task 7: Middleware bus

**Files:**
- Create: `src/zolva/bus.py`, `tests/test_bus.py`

**Interfaces:**
- Produces:
  - `StepType = Literal["user_msg", "model_call", "tool_call", "response", "handover", "feedback"]`
  - `class Step(BaseModel)`: `type: StepType`, `session_id: str`, `agent: str`, `data: dict[str, Any]`
  - `class Verdict(BaseModel)`: `allow: bool = True`, `reason: str | None = None`
  - `Hook = Callable[[Step], Awaitable[Verdict | None]]`
  - `class Bus`: `.on(hook: Hook) -> None`; `async .emit(step: Step) -> Verdict` — runs hooks in registration order; first `allow=False` verdict short-circuits and is returned; otherwise returns `Verdict()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bus.py
from zolva.bus import Bus, Step, Verdict


def step() -> Step:
    return Step(type="response", session_id="s1", agent="a", data={"text": "hi"})


async def test_no_hooks_allows() -> None:
    assert (await Bus().emit(step())).allow is True


async def test_blocking_hook_short_circuits() -> None:
    bus = Bus()
    seen: list[str] = []

    async def blocker(s: Step) -> Verdict:
        seen.append("blocker")
        return Verdict(allow=False, reason="policy")

    async def never_runs(s: Step) -> None:
        seen.append("never")
        return None

    bus.on(blocker)
    bus.on(never_runs)
    v = await bus.emit(step())
    assert v.allow is False and v.reason == "policy"
    assert seen == ["blocker"]


async def test_observing_hook_sees_all_steps() -> None:
    bus = Bus()
    log: list[Step] = []

    async def observer(s: Step) -> None:
        log.append(s)
        return None

    bus.on(observer)
    await bus.emit(step())
    await bus.emit(step())
    assert len(log) == 2
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest tests/test_bus.py -v` → FAIL

- [ ] **Step 3: Implement**

```python
# src/zolva/bus.py
"""Middleware bus: every orchestrator step flows through here. Plugins attach as hooks."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel

StepType = Literal["user_msg", "model_call", "tool_call", "response", "handover", "feedback"]


class Step(BaseModel):
    type: StepType
    session_id: str
    agent: str
    data: dict[str, Any]


class Verdict(BaseModel):
    allow: bool = True
    reason: str | None = None


Hook = Callable[[Step], Awaitable[Verdict | None]]


class Bus:
    def __init__(self) -> None:
        self._hooks: list[Hook] = []

    def on(self, hook: Hook) -> None:
        self._hooks.append(hook)

    async def emit(self, step: Step) -> Verdict:
        for hook in self._hooks:
            verdict = await hook(step)
            if verdict is not None and not verdict.allow:
                return verdict
        return Verdict()
```

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_bus.py -v && ruff check . && mypy` → all PASS

- [ ] **Step 5: Commit**

```bash
git add src/zolva/bus.py tests/test_bus.py
git commit -m "feat: middleware bus with blocking verdicts"
```

---

### Task 8: Session stores

**Files:**
- Create: `src/zolva/sessions.py`, `tests/test_sessions.py`

**Interfaces:**
- Consumes: `Message` from Task 4.
- Produces:
  - `class SessionStore(Protocol)`: `async def history(self, session_id: str) -> list[Message]`; `async def append(self, session_id: str, messages: list[Message]) -> None`
  - `class InMemorySessionStore` and `class SqliteSessionStore(path: str | Path)` implementing it. SQLite uses stdlib `sqlite3`, table `messages(session_id TEXT, seq INTEGER, payload TEXT)`, ordered by `seq`. Sessions strictly isolated by `session_id`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sessions.py
from pathlib import Path

from zolva.bridge import Message
from zolva.sessions import InMemorySessionStore, SqliteSessionStore


async def test_inmemory_roundtrip_and_isolation() -> None:
    store = InMemorySessionStore()
    await store.append("s1", [Message(role="user", content="a")])
    await store.append("s2", [Message(role="user", content="OTHER")])
    await store.append("s1", [Message(role="assistant", content="b")])
    hist = await store.history("s1")
    assert [m.content for m in hist] == ["a", "b"]
    assert await store.history("unknown") == []


async def test_sqlite_roundtrip_persists(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    store = SqliteSessionStore(db)
    await store.append("s1", [Message(role="user", content="a"), Message(role="assistant", content="b")])
    reopened = SqliteSessionStore(db)
    hist = await reopened.history("s1")
    assert [m.content for m in hist] == ["a", "b"]
    assert hist[1].role == "assistant"


async def test_sqlite_preserves_tool_calls(tmp_path: Path) -> None:
    from zolva.bridge import ToolCall

    store = SqliteSessionStore(tmp_path / "s.db")
    msg = Message(role="assistant", content="", tool_calls=[ToolCall(id="1", name="t", args={"x": 1})])
    await store.append("s1", [msg])
    hist = await store.history("s1")
    assert hist[0].tool_calls[0].args == {"x": 1}
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest tests/test_sessions.py -v` → FAIL

- [ ] **Step 3: Implement**

```python
# src/zolva/sessions.py
"""Session storage. Isolation per session_id is a security property, not a convenience."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol

from zolva.bridge import Message


class SessionStore(Protocol):
    async def history(self, session_id: str) -> list[Message]: ...

    async def append(self, session_id: str, messages: list[Message]) -> None: ...


class InMemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, list[Message]] = {}

    async def history(self, session_id: str) -> list[Message]:
        return list(self._sessions.get(session_id, []))

    async def append(self, session_id: str, messages: list[Message]) -> None:
        self._sessions.setdefault(session_id, []).extend(messages)


class SqliteSessionStore:
    # ponytail: sync sqlite3 behind async methods; swap for aiosqlite if contention ever measured
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS messages "
                "(session_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY (session_id, seq))"
            )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    async def history(self, session_id: str) -> list[Message]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
            ).fetchall()
        return [Message.model_validate_json(r[0]) for r in rows]

    async def append(self, session_id: str, messages: list[Message]) -> None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) FROM messages WHERE session_id = ?", (session_id,)
            ).fetchone()
            next_seq = int(row[0]) + 1
            conn.executemany(
                "INSERT INTO messages VALUES (?, ?, ?)",
                [
                    (session_id, next_seq + i, m.model_dump_json())
                    for i, m in enumerate(messages)
                ],
            )
```

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_sessions.py -v && ruff check . && mypy` → all PASS

- [ ] **Step 5: Commit**

```bash
git add src/zolva/sessions.py tests/test_sessions.py
git commit -m "feat: in-memory and SQLite session stores"
```

---

### Task 9: Human handover

**Files:**
- Create: `src/zolva/handover.py`, `tests/test_handover.py`

**Interfaces:**
- Consumes: `Message` from Task 4.
- Produces:
  - `class Ticket(BaseModel)`: `session_id: str`, `agent: str`, `reason: str`, `transcript: list[Message]`, `summary: str = ""`
  - `class HandoverRef(BaseModel)`: `id: str`, `backend: str`
  - `class HandoverBackend(ABC)`: `async def escalate(self, ticket: Ticket) -> HandoverRef` (abstract); `async def resume(self, ref: HandoverRef, resolution: str) -> None` (default no-op)
  - `class LogBackend(HandoverBackend)` — logs the ticket via `logging`, returns `HandoverRef(id="log-<uuid4>", backend="log")`
  - `class WebhookBackend(HandoverBackend)`: `__init__(self, url: str, secret: str, transport: httpx.AsyncBaseTransport | None = None)` — POSTs `ticket.model_dump_json()` with header `X-Zolva-Signature` = hex HMAC-SHA256 of the body using `secret`; expects `{"id": "..."}` back; raises `HandoverError` on HTTP failure.
  - `class HandoverError(Exception)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_handover.py
import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest

from zolva.bridge import Message
from zolva.handover import HandoverError, LogBackend, Ticket, WebhookBackend

TICKET = Ticket(
    session_id="s1",
    agent="collections-agent",
    reason="guardrail: never rule",
    transcript=[Message(role="user", content="hi")],
)


async def test_log_backend_returns_ref() -> None:
    ref = await LogBackend().escalate(TICKET)
    assert ref.backend == "log" and ref.id.startswith("log-")


async def test_webhook_posts_signed_payload() -> None:
    cap: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["body"] = request.content
        cap["sig"] = request.headers["x-zolva-signature"]
        return httpx.Response(200, json={"id": "T-42"})

    b = WebhookBackend("https://desk.bank.internal/hook", secret="s3cr3t",
                       transport=httpx.MockTransport(handler))
    ref = await b.escalate(TICKET)
    assert ref.id == "T-42" and ref.backend == "webhook"
    expected = hmac.new(b"s3cr3t", cap["body"], hashlib.sha256).hexdigest()
    assert cap["sig"] == expected
    assert json.loads(cap["body"])["session_id"] == "s1"


async def test_webhook_http_failure_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    b = WebhookBackend("https://x", secret="s", transport=httpx.MockTransport(handler))
    with pytest.raises(HandoverError):
        await b.escalate(TICKET)
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest tests/test_handover.py -v` → FAIL

- [ ] **Step 3: Implement**

```python
# src/zolva/handover.py
"""Human handover: one interface, pluggable backends."""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from abc import ABC, abstractmethod

import httpx
from pydantic import BaseModel

from zolva.bridge import Message

logger = logging.getLogger("zolva.handover")


class HandoverError(Exception):
    """Escalation could not be delivered."""


class Ticket(BaseModel):
    session_id: str
    agent: str
    reason: str
    transcript: list[Message]
    summary: str = ""


class HandoverRef(BaseModel):
    id: str
    backend: str


class HandoverBackend(ABC):
    @abstractmethod
    async def escalate(self, ticket: Ticket) -> HandoverRef: ...

    async def resume(self, ref: HandoverRef, resolution: str) -> None:
        return None


class LogBackend(HandoverBackend):
    async def escalate(self, ticket: Ticket) -> HandoverRef:
        logger.warning("HANDOVER session=%s agent=%s reason=%s", ticket.session_id, ticket.agent, ticket.reason)
        return HandoverRef(id=f"log-{uuid.uuid4()}", backend="log")


class WebhookBackend(HandoverBackend):
    def __init__(
        self, url: str, secret: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._url = url
        self._secret = secret.encode()
        self._client = httpx.AsyncClient(transport=transport, timeout=30.0)

    async def escalate(self, ticket: Ticket) -> HandoverRef:
        body = ticket.model_dump_json().encode()
        sig = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        try:
            r = await self._client.post(
                self._url,
                content=body,
                headers={"Content-Type": "application/json", "X-Zolva-Signature": sig},
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise HandoverError(f"webhook escalation failed: {e}") from e
        return HandoverRef(id=str(r.json()["id"]), backend="webhook")
```

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_handover.py -v && ruff check . && mypy` → all PASS

- [ ] **Step 5: Commit**

```bash
git add src/zolva/handover.py tests/test_handover.py
git commit -m "feat: handover interface with Log and HMAC-signed Webhook backends"
```

---

### Task 10: Orchestrator core loop

**Files:**
- Create: `src/zolva/orchestrator.py`, `tests/test_orchestrator.py`
- Modify: `src/zolva/__init__.py`

**Interfaces:**
- Consumes: everything from Tasks 2–9 (exact names as listed in those tasks).
- Produces:
  - `BLOCKED_MESSAGE: str = "I can't help with that here — I've connected you with a human teammate."`
  - `MAX_TURNS: int = 10`
  - `class AgentApp`:
    - `__init__(self, agents: dict[str, AgentConfig], *, registry: ToolRegistry | None = None, handover: HandoverBackend | None = None, sessions: SessionStore | None = None, bus: Bus | None = None, adapter: LLMAdapter | None = None)` — defaults: `default_registry`, `LogBackend()`, `InMemorySessionStore()`, `Bus()`; `adapter=None` means resolve per-agent via `get_adapter(cfg.model.provider)`.
    - `@classmethod def from_config(cls, config_dir: str | Path, **kwargs: Any) -> AgentApp`
    - `async def run(self, agent_name: str, session_id: str, user_msg: str) -> str`
  - Behavior: emits `user_msg` step (blockable) → appends user message → loop ≤ `MAX_TURNS`: adapter.complete → tool calls each emit blockable `tool_call` step, `ToolContractError` is fed back to the model as a `TOOL_ERROR: ...` tool message (model self-corrects within the turn budget) → final text emits blockable `response` step → appended + returned. Any block or turn-exhaustion → `_escalate` → `Ticket` to handover backend + `handover` step emitted → returns `BLOCKED_MESSAGE`.
  - `zolva.__init__` re-exports: `tool`, `default_registry`, `ToolRegistry`, `AgentApp`, `AgentConfig`, `load_agents`, `Bus`, `Step`, `Verdict`, `HandoverBackend`, `LogBackend`, `WebhookBackend`, `Ticket`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_orchestrator.py
from zolva.bridge import LLMResponse, ToolCall
from zolva.bridge.fake import FakeAdapter
from zolva.bus import Bus, Step, Verdict
from zolva.config import AgentConfig, ModelConfig
from zolva.handover import HandoverBackend, HandoverRef, Ticket
from zolva.orchestrator import BLOCKED_MESSAGE, AgentApp
from zolva.tools import ToolRegistry


def make_cfg(**kw: object) -> AgentConfig:
    base: dict[str, object] = {
        "name": "collections-agent",
        "instructions": "Collect politely.",
        "model": ModelConfig(provider="test", name="m"),
        "tools": ["get_dues"],
    }
    base.update(kw)
    return AgentConfig.model_validate(base)


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.register
    def get_dues(customer_id: str) -> dict[str, int]:
        """Dues."""
        return {"amount": 4200}

    return reg


class CapturingHandover(HandoverBackend):
    def __init__(self) -> None:
        self.tickets: list[Ticket] = []

    async def escalate(self, ticket: Ticket) -> HandoverRef:
        self.tickets.append(ticket)
        return HandoverRef(id="cap-1", backend="cap")


async def test_plain_reply() -> None:
    app = AgentApp(
        {"collections-agent": make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[LLMResponse(text="Hello!")]),
    )
    assert await app.run("collections-agent", "s1", "hi") == "Hello!"


async def test_tool_call_roundtrip() -> None:
    fake = FakeAdapter(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="1", name="get_dues", args={"customer_id": "c1"})]),
            LLMResponse(text="You owe 4200."),
        ]
    )
    app = AgentApp({"collections-agent": make_cfg()}, registry=make_registry(), adapter=fake)
    assert await app.run("collections-agent", "s1", "dues?") == "You owe 4200."
    # second model call saw the tool result
    tool_msgs = [m for m in fake.calls[1]["messages"] if m.role == "tool"]
    assert "4200" in tool_msgs[0].content


async def test_contract_error_fed_back_to_model() -> None:
    fake = FakeAdapter(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="1", name="get_dues", args={"wrong": True})]),
            LLMResponse(text="Sorry, retrying."),
        ]
    )
    app = AgentApp({"collections-agent": make_cfg()}, registry=make_registry(), adapter=fake)
    await app.run("collections-agent", "s1", "dues?")
    tool_msgs = [m for m in fake.calls[1]["messages"] if m.role == "tool"]
    assert tool_msgs[0].content.startswith("TOOL_ERROR:")


async def test_blocked_response_escalates() -> None:
    bus = Bus()

    async def block_responses(s: Step) -> Verdict | None:
        if s.type == "response":
            return Verdict(allow=False, reason="policy violation")
        return None

    bus.on(block_responses)
    handover = CapturingHandover()
    app = AgentApp(
        {"collections-agent": make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[LLMResponse(text="Buy this fund!")]),
        bus=bus,
        handover=handover,
    )
    result = await app.run("collections-agent", "s1", "advice?")
    assert result == BLOCKED_MESSAGE
    assert handover.tickets[0].reason == "policy violation"


async def test_max_turns_escalates() -> None:
    looping = [
        LLMResponse(tool_calls=[ToolCall(id=str(i), name="get_dues", args={"customer_id": "c"})])
        for i in range(20)
    ]
    handover = CapturingHandover()
    app = AgentApp(
        {"collections-agent": make_cfg()},
        registry=make_registry(),
        adapter=FakeAdapter(script=looping),
        handover=handover,
    )
    assert await app.run("collections-agent", "s1", "dues?") == BLOCKED_MESSAGE
    assert "max turns" in handover.tickets[0].reason
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest tests/test_orchestrator.py -v` → FAIL

- [ ] **Step 3: Implement**

```python
# src/zolva/orchestrator.py
"""The agent loop. Every observable step flows through the Bus so plugins can see or block it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zolva.bridge import LLMAdapter, Message, get_adapter
from zolva.bus import Bus, Step
from zolva.config import AgentConfig, load_agents
from zolva.handover import HandoverBackend, LogBackend, Ticket
from zolva.sessions import InMemorySessionStore, SessionStore
from zolva.tools import ToolContractError, ToolRegistry, default_registry

BLOCKED_MESSAGE = "I can't help with that here — I've connected you with a human teammate."
MAX_TURNS = 10


class AgentApp:
    def __init__(
        self,
        agents: dict[str, AgentConfig],
        *,
        registry: ToolRegistry | None = None,
        handover: HandoverBackend | None = None,
        sessions: SessionStore | None = None,
        bus: Bus | None = None,
        adapter: LLMAdapter | None = None,
    ) -> None:
        self._agents = agents
        self._registry = registry if registry is not None else default_registry
        self._handover = handover if handover is not None else LogBackend()
        self._sessions: SessionStore = sessions if sessions is not None else InMemorySessionStore()
        self.bus = bus if bus is not None else Bus()
        self._adapter = adapter

    @classmethod
    def from_config(cls, config_dir: str | Path, **kwargs: Any) -> AgentApp:
        return cls(load_agents(config_dir), **kwargs)

    def _adapter_for(self, cfg: AgentConfig) -> LLMAdapter:
        return self._adapter if self._adapter is not None else get_adapter(cfg.model.provider)

    async def run(self, agent_name: str, session_id: str, user_msg: str) -> str:
        cfg = self._agents[agent_name]
        verdict = await self.bus.emit(
            Step(type="user_msg", session_id=session_id, agent=cfg.name, data={"text": user_msg})
        )
        if not verdict.allow:
            return await self._escalate(cfg, session_id, verdict.reason or "blocked")
        await self._sessions.append(session_id, [Message(role="user", content=user_msg)])

        for _ in range(MAX_TURNS):
            history = await self._sessions.history(session_id)
            response = await self._adapter_for(cfg).complete(
                model=cfg.model.name,
                system=cfg.instructions,
                messages=history,
                tools=self._registry.specs(cfg.tools),
            )
            if response.tool_calls:
                await self._sessions.append(
                    session_id,
                    [Message(role="assistant", content=response.text, tool_calls=response.tool_calls)],
                )
                for tc in response.tool_calls:
                    verdict = await self.bus.emit(
                        Step(
                            type="tool_call",
                            session_id=session_id,
                            agent=cfg.name,
                            data={"name": tc.name, "args": tc.args},
                        )
                    )
                    if not verdict.allow:
                        return await self._escalate(cfg, session_id, verdict.reason or "blocked")
                    try:
                        result = await self._registry.call(tc.name, tc.args)
                        content = json.dumps(result, default=str)
                    except ToolContractError as e:
                        content = f"TOOL_ERROR: {e}"  # fed back; model retries within MAX_TURNS
                    await self._sessions.append(
                        session_id, [Message(role="tool", content=content, tool_call_id=tc.id)]
                    )
                continue

            verdict = await self.bus.emit(
                Step(type="response", session_id=session_id, agent=cfg.name, data={"text": response.text})
            )
            if not verdict.allow:
                return await self._escalate(cfg, session_id, verdict.reason or "blocked")
            await self._sessions.append(session_id, [Message(role="assistant", content=response.text)])
            return response.text

        return await self._escalate(cfg, session_id, "max turns exceeded")

    async def _escalate(self, cfg: AgentConfig, session_id: str, reason: str) -> str:
        ticket = Ticket(
            session_id=session_id,
            agent=cfg.name,
            reason=reason,
            transcript=await self._sessions.history(session_id),
        )
        await self.bus.emit(
            Step(type="handover", session_id=session_id, agent=cfg.name, data={"reason": reason})
        )
        await self._handover.escalate(ticket)
        return BLOCKED_MESSAGE
```

```python
# src/zolva/__init__.py
"""Zolva: self-hosted agent platform for banks and fintechs."""

from zolva.bus import Bus, Step, Verdict
from zolva.config import AgentConfig, ConfigError, load_agents
from zolva.handover import HandoverBackend, LogBackend, Ticket, WebhookBackend
from zolva.orchestrator import BLOCKED_MESSAGE, AgentApp
from zolva.tools import ToolRegistry, default_registry, tool

__version__ = "0.1.0"

__all__ = [
    "BLOCKED_MESSAGE",
    "AgentApp",
    "AgentConfig",
    "Bus",
    "ConfigError",
    "HandoverBackend",
    "LogBackend",
    "Step",
    "Ticket",
    "ToolRegistry",
    "Verdict",
    "WebhookBackend",
    "default_registry",
    "load_agents",
    "tool",
]
```

- [ ] **Step 4: Verify green (full suite — this integrates everything)**

Run: `pytest -q && ruff check . && mypy` → all PASS

- [ ] **Step 5: Commit**

```bash
git add src/zolva/orchestrator.py src/zolva/__init__.py tests/test_orchestrator.py
git commit -m "feat: orchestrator loop with bus verdicts, contract retry, escalation"
```

---

### Task 11: Handoffs (agent→agent and human)

**Files:**
- Modify: `src/zolva/orchestrator.py`
- Test: `tests/test_handoffs.py`

**Interfaces:**
- Consumes: Task 10's `AgentApp.run` loop.
- Produces: when `cfg.handoffs` is non-empty, a built-in tool spec named `handoff` (parameters: `to: str`, `reason: str`) is appended to the tools sent to the model. When the model calls it: `to == "human-escalation"` → `_escalate(cfg, session_id, reason)`; `to == <agent name>` → validate it's in `cfg.handoffs`, append tool result `"handed off to <to>"`, switch `cfg` to the target agent, continue the loop (context carried — same session history). Invalid target → `TOOL_ERROR:` fed back.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_handoffs.py
from zolva.bridge import LLMResponse, ToolCall
from zolva.bridge.fake import FakeAdapter
from zolva.config import AgentConfig, ModelConfig
from zolva.orchestrator import BLOCKED_MESSAGE, AgentApp
from zolva.tools import ToolRegistry

from tests.test_orchestrator import CapturingHandover


def cfg(name: str, handoffs: list[str]) -> AgentConfig:
    return AgentConfig(
        name=name,
        instructions=f"You are {name}.",
        model=ModelConfig(provider="test", name="m"),
        handoffs=handoffs,
    )


AGENTS = {
    "collections-agent": cfg("collections-agent", ["hardship-agent", "human-escalation"]),
    "hardship-agent": cfg("hardship-agent", []),
}


async def test_handoff_tool_offered_only_when_configured() -> None:
    fake = FakeAdapter(script=[LLMResponse(text="ok")])
    app = AgentApp(AGENTS, registry=ToolRegistry(), adapter=fake)
    await app.run("hardship-agent", "s1", "hi")
    assert all(t.name != "handoff" for t in fake.calls[0]["tools"])


async def test_agent_to_agent_handoff_switches_and_carries_context() -> None:
    fake = FakeAdapter(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="1", name="handoff", args={"to": "hardship-agent", "reason": "hardship claim"})]),
            LLMResponse(text="Hardship plan: ..."),
        ]
    )
    app = AgentApp(AGENTS, registry=ToolRegistry(), adapter=fake)
    result = await app.run("collections-agent", "s1", "I lost my job")
    assert result == "Hardship plan: ..."
    assert fake.calls[1]["system"] == "You are hardship-agent."
    assert any("lost my job" in m.content for m in fake.calls[1]["messages"])


async def test_handoff_to_human_escalates() -> None:
    handover = CapturingHandover()
    fake = FakeAdapter(
        script=[LLMResponse(tool_calls=[ToolCall(id="1", name="handoff", args={"to": "human-escalation", "reason": "user asked"})])]
    )
    app = AgentApp(AGENTS, registry=ToolRegistry(), adapter=fake, handover=handover)
    assert await app.run("collections-agent", "s1", "human please") == BLOCKED_MESSAGE
    assert handover.tickets[0].reason == "user asked"


async def test_invalid_handoff_target_fed_back_as_error() -> None:
    fake = FakeAdapter(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="1", name="handoff", args={"to": "ghost", "reason": "x"})]),
            LLMResponse(text="ok, staying"),
        ]
    )
    app = AgentApp(AGENTS, registry=ToolRegistry(), adapter=fake)
    assert await app.run("collections-agent", "s1", "hi") == "ok, staying"
    tool_msgs = [m for m in fake.calls[1]["messages"] if m.role == "tool"]
    assert tool_msgs[0].content.startswith("TOOL_ERROR:")
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest tests/test_handoffs.py -v` → FAIL

- [ ] **Step 3: Implement — modify `orchestrator.py`**

Add after the imports:

```python
from zolva.tools import ToolSpec

_HANDOFF_SPEC_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {"type": "string", "description": "Target agent name or 'human-escalation'"},
        "reason": {"type": "string"},
    },
    "required": ["to", "reason"],
}


def _handoff_spec(cfg: AgentConfig) -> ToolSpec:
    return ToolSpec(
        name="handoff",
        description=f"Hand this conversation to one of: {', '.join(cfg.handoffs)}",
        parameters=_HANDOFF_SPEC_PARAMS,
    )
```

In `run()`, replace `tools=self._registry.specs(cfg.tools)` with:

```python
            tools = self._registry.specs(cfg.tools)
            if cfg.handoffs:
                tools = [*tools, _handoff_spec(cfg)]
            response = await self._adapter_for(cfg).complete(
                model=cfg.model.name,
                system=cfg.instructions,
                messages=history,
                tools=tools,
            )
```

Inside the `for tc in response.tool_calls:` loop, insert BEFORE the bus emit:

```python
                    if tc.name == "handoff":
                        target = str(tc.args.get("to", ""))
                        reason = str(tc.args.get("reason", ""))
                        if target == "human-escalation":
                            return await self._escalate(cfg, session_id, reason or "agent handoff")
                        if target in cfg.handoffs and target in self._agents:
                            await self._sessions.append(
                                session_id,
                                [Message(role="tool", content=f"handed off to {target}", tool_call_id=tc.id)],
                            )
                            cfg = self._agents[target]
                            continue
                        await self._sessions.append(
                            session_id,
                            [Message(role="tool", content=f"TOOL_ERROR: invalid handoff target {target!r}", tool_call_id=tc.id)],
                        )
                        continue
```

- [ ] **Step 4: Verify green (full suite)**

Run: `pytest -q && ruff check . && mypy` → all PASS

- [ ] **Step 5: Commit**

```bash
git add src/zolva/orchestrator.py tests/test_handoffs.py
git commit -m "feat: typed handoffs between agents and to human escalation"
```

---

### Task 12: CLI — `zolva validate`

**Files:**
- Create: `src/zolva/cli.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `load_agents`, `ConfigError` from Task 2.
- Produces: `def main(argv: list[str] | None = None) -> int` — `zolva validate <config_dir>` prints one line per agent (`name  provider/model  tools=N  handoffs=[...]`), returns 0; on `ConfigError` prints to stderr, returns 1. (Entry point already wired in Task 1's pyproject.)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli.py
from pathlib import Path

import pytest

from zolva.cli import main
from tests.test_config import make_agent_dir


def test_validate_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(make_agent_dir(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "collections-agent" in out and "openai/gpt-5" in out


def test_validate_bad_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "agents"
    bad.mkdir()
    (bad / "a.yaml").write_text("name: a\ninstructions: missing.md\nmodel: {provider: p, name: n}\n")
    assert main(["validate", str(bad)]) == 1
    assert "not found" in capsys.readouterr().err
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest tests/test_cli.py -v` → FAIL

- [ ] **Step 3: Implement**

```python
# src/zolva/cli.py
"""zolva CLI. v0.1: validate. Plugins add subcommands (eval, triage, scorecard) later."""

from __future__ import annotations

import argparse
import sys

from zolva.config import ConfigError, load_agents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zolva")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="validate an agent config directory")
    validate.add_argument("config_dir")
    args = parser.parse_args(argv)

    if args.command == "validate":
        try:
            agents = load_agents(args.config_dir)
        except ConfigError as e:
            print(f"config error: {e}", file=sys.stderr)
            return 1
        for cfg in agents.values():
            print(
                f"{cfg.name}  {cfg.model.provider}/{cfg.model.name}  "
                f"tools={len(cfg.tools)}  handoffs={cfg.handoffs}"
            )
        print(f"OK: {len(agents)} agent(s) valid")
        return 0
    return 1
```

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_cli.py -v && ruff check . && mypy` → all PASS
Run: `zolva validate examples/mockbank/agents 2>&1 || true` → (dir doesn't exist yet — fine, Task 13 creates it)

- [ ] **Step 5: Commit**

```bash
git add src/zolva/cli.py tests/test_cli.py
git commit -m "feat: zolva validate CLI"
```

---

### Task 13: Mockbank example + end-to-end integration test + README

**Files:**
- Create: `examples/mockbank/agents/collections.yaml`, `examples/mockbank/agents/collections.md`, `examples/mockbank/bank.py`, `tests/test_mockbank_e2e.py`, `README.md`

**Interfaces:**
- Consumes: the entire public API (`zolva.__init__` exports from Task 10).
- Produces: a runnable example proving config → tools → orchestrator → handoff end-to-end; the repo's living integration test and the README quickstart's source of truth.

- [ ] **Step 1: Write the example**

```yaml
# examples/mockbank/agents/collections.yaml
name: collections-agent
instructions: collections.md
model: { provider: openai, name: gpt-5 }
tools: [get_dues, get_repayment_options, send_payment_link]
handoffs: [human-escalation]
```

```markdown
<!-- examples/mockbank/agents/collections.md -->
You are a repayment assistant for MockBank. Be respectful and concise.
Look up the customer's dues before discussing amounts. Offer repayment options
when the customer cannot pay in full. Send a payment link only after the
customer agrees to an amount. If the customer reports financial hardship or
asks for a person, hand off to human-escalation.
```

```python
# examples/mockbank/bank.py
"""MockBank: an in-memory 'core banking system' exposing tools the agent may use."""

from __future__ import annotations

from zolva import tool

_LOANS: dict[str, dict[str, object]] = {
    "c1": {"dues": 4200, "due_date": "2026-07-20", "options": [4200, 2100, 1400]},
}


@tool
def get_dues(customer_id: str) -> dict[str, object]:
    """Fetch outstanding dues and due date for a customer."""
    loan = _LOANS[customer_id]
    return {"amount": loan["dues"], "due_date": loan["due_date"]}


@tool
def get_repayment_options(customer_id: str) -> list[object]:
    """Fetch available repayment amounts (full and part payments)."""
    return list(_LOANS[customer_id]["options"])  # type: ignore[arg-type]


@tool
def send_payment_link(customer_id: str, amount: int) -> dict[str, str]:
    """Send a payment link for the agreed amount. Irreversible customer contact."""
    return {"status": "sent", "link": f"https://pay.mockbank.example/{customer_id}/{amount}"}
```

- [ ] **Step 2: Write the end-to-end test (this is the failing test for the example)**

```python
# tests/test_mockbank_e2e.py
"""End-to-end: real config dir, real tools, scripted model. The repo's dogfood gate seed."""

from pathlib import Path

import examples.mockbank.bank  # noqa: F401  (registers tools into default_registry)
from zolva import AgentApp
from zolva.bridge import LLMResponse, ToolCall
from zolva.bridge.fake import FakeAdapter

AGENTS_DIR = Path(__file__).parent.parent / "examples" / "mockbank" / "agents"


async def test_collections_flow_end_to_end() -> None:
    fake = FakeAdapter(
        script=[
            LLMResponse(tool_calls=[ToolCall(id="1", name="get_dues", args={"customer_id": "c1"})]),
            LLMResponse(text="You owe ₹4200, due 2026-07-20. Pay in full or in parts?"),
            LLMResponse(tool_calls=[ToolCall(id="2", name="send_payment_link", args={"customer_id": "c1", "amount": 2100})]),
            LLMResponse(text="Done — sent a link for ₹2100."),
        ]
    )
    app = AgentApp.from_config(AGENTS_DIR, adapter=fake)
    r1 = await app.run("collections-agent", "sess-1", "what do I owe?")
    assert "4200" in r1
    r2 = await app.run("collections-agent", "sess-1", "I'll pay 2100 now")
    assert "2100" in r2


async def test_cli_validates_example() -> None:
    from zolva.cli import main

    assert main(["validate", str(AGENTS_DIR)]) == 0
```

- [ ] **Step 3: Run — expect fail, then create missing `__init__.py` files**

Run: `pytest tests/test_mockbank_e2e.py -v` → FAIL (import error)
Create empty `examples/__init__.py` and `examples/mockbank/__init__.py` so the test can import the example.

- [ ] **Step 4: Verify green (full suite)**

Run: `pytest -q && ruff check . && mypy` → all PASS
Run: `zolva validate examples/mockbank/agents` → prints `collections-agent  openai/gpt-5  tools=3  handoffs=['human-escalation']` and `OK: 1 agent(s) valid`

- [ ] **Step 5: Write README quickstart**

```markdown
# Zolva (working name)

Open-source, self-hosted agent platform for banks and fintechs. Agents are
config, tools are your existing APIs, and guardrails/evals/audit attach to
every step. Spec: `docs/specs/2026-07-12-bank-agent-platform-design.md`.

## Quickstart

​```bash
pip install -e ".[dev]"
zolva validate examples/mockbank/agents
pytest -q
​```

Define an agent (`agents/collections.yaml` + `collections.md`), wrap your API:

​```python
from zolva import tool, AgentApp

@tool
def get_dues(customer_id: str) -> dict[str, object]:
    """Fetch outstanding dues."""
    return loans_api.dues(customer_id)

app = AgentApp.from_config("agents/")
reply = await app.run("collections-agent", session_id, user_msg)
​```

Status: v0.1 core. Guardrails, evals, feedback loop, audit, synthetics ship as
plugins — see the spec.
```

(Remove the zero-width characters around the fences when writing the real file — they exist only to nest the code blocks in this plan.)

- [ ] **Step 6: Commit**

```bash
git add examples/ tests/test_mockbank_e2e.py README.md
git commit -m "feat: mockbank example, e2e test, README quickstart"
```

---

### Task 14: AI-agent onboarding — llms.txt, llms-full.txt, AGENTS.md

Adopting banks will point their own AI coding agents (Claude Code, Cursor, Copilot) at this repo and say "set this up." This task makes that work first-try.

**Files:**
- Create: `llms.txt`, `AGENTS.md`, `scripts/build_llms_full.py`, `tests/test_llms_docs.py`
- Generate: `llms-full.txt` (committed, rebuilt by the script)

**Interfaces:**
- Consumes: README (Task 13), spec, plan — all existing docs.
- Produces: repo-root `llms.txt` (llmstxt.org format: H1 + summary + linked sections), `llms-full.txt` (concatenation of README, AGENTS.md, spec, and public API surface), `AGENTS.md` (exact setup/verify commands + conventions for AI agents).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llms_docs.py
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_llms_txt_exists_and_is_valid() -> None:
    text = (ROOT / "llms.txt").read_text()
    assert text.startswith("# ")          # llmstxt.org: H1 first
    assert "## Docs" in text
    assert "AGENTS.md" in text


def test_agents_md_has_setup_and_verify_commands() -> None:
    text = (ROOT / "AGENTS.md").read_text()
    for cmd in ['pip install -e ".[dev]"', "pytest -q", "ruff check .", "mypy", "zolva validate"]:
        assert cmd in text, f"missing command: {cmd}"


def test_llms_full_is_fresh() -> None:
    import subprocess
    import sys

    before = (ROOT / "llms-full.txt").read_text()
    subprocess.run([sys.executable, "scripts/build_llms_full.py"], cwd=ROOT, check=True)
    assert (ROOT / "llms-full.txt").read_text() == before, "llms-full.txt stale: run scripts/build_llms_full.py"
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest tests/test_llms_docs.py -v` → FAIL (files missing)

- [ ] **Step 3: Write the files**

```markdown
<!-- llms.txt -->
# Zolva

> Open-source, self-hosted agent platform for banks and fintechs. Agents are
> declared in YAML + Markdown config; tools are the bank's own APIs registered
> as typed Python functions; guardrails, evals, feedback loop, audit, and
> synthetics attach to every step via a middleware bus. pip-installable, runs
> entirely inside the bank's infrastructure. Apache-2.0.

Install: `pip install zolva` (dev: `pip install -e ".[dev]"`). Python >=3.11.
Entry points: `zolva.AgentApp.from_config("agents/")`, `@zolva.tool`, CLI `zolva validate <dir>`.

## Docs

- [AGENTS.md](AGENTS.md): exact setup, verify, and convention instructions for AI coding agents
- [README.md](README.md): quickstart and public API
- [docs/specs/2026-07-12-bank-agent-platform-design.md](docs/specs/2026-07-12-bank-agent-platform-design.md): full architecture, security model, competitive positioning
- [examples/mockbank/](examples/mockbank/): runnable end-to-end example (config + tools + orchestrator)

## Optional

- [docs/plans/2026-07-12-core-runtime.md](docs/plans/2026-07-12-core-runtime.md): core implementation plan with every interface signature
```

```markdown
<!-- AGENTS.md -->
# Instructions for AI agents setting up Zolva

You are setting this up inside a bank/fintech codebase. Follow exactly; verify every step.

## Setup

​```bash
python3 --version                 # must be >= 3.11
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
​```

## Verify the installation (run ALL — do not skip)

​```bash
pytest -q                         # expect: all tests pass
ruff check . && ruff format --check .
mypy                              # strict; must be clean
zolva validate examples/mockbank/agents   # expect: "OK: 1 agent(s) valid"
​```

If any command fails, STOP and report the output. Do not work around failures.

## Creating the bank's first agent

1. Copy `examples/mockbank/agents/` to `agents/` in the host project.
2. Edit the YAML: `name`, `model.provider` (`openai` | `anthropic`), `model.name`, `tools`, `handoffs`.
3. Write instructions in the sibling `.md` file — plain Markdown, owned by product/compliance.
4. Register tools by decorating the bank's existing API client functions with `@zolva.tool`.
   Type hints are the contract: annotate every parameter and the return type.
5. Provider keys come from env (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`).
   NEVER write credentials into YAML — the loader rejects keys matching key/secret/token/password
   unless they are `${ENV:VAR}` references.
6. Verify: `zolva validate agents/` then test with `zolva.bridge.fake.FakeAdapter` before any live key.

## Conventions (for agents contributing code)

- TDD: failing test first. Every PR: `pytest -q && ruff check . && mypy` all green.
- Runtime deps are frozen: pydantic, httpx, pyyaml. Do not add dependencies.
- YAML via `yaml.safe_load` only. No `eval`/`exec`/`pickle`.
- Conventional commits (`feat:`, `test:`, `chore:`).
- After editing docs, run `python scripts/build_llms_full.py` and commit `llms-full.txt`.
​```
```

(Remove the zero-width characters around the inner fences when writing the real files.)

```python
# scripts/build_llms_full.py
"""Concatenate the docs an LLM needs into llms-full.txt. Run after any docs change."""

from pathlib import Path

ROOT = Path(__file__).parent.parent
SOURCES = [
    "llms.txt",
    "AGENTS.md",
    "README.md",
    "docs/specs/2026-07-12-bank-agent-platform-design.md",
]

parts = [f"<!-- {src} -->\n\n{(ROOT / src).read_text()}" for src in SOURCES]
(ROOT / "llms-full.txt").write_text("\n\n---\n\n".join(parts) + "\n")
print(f"wrote llms-full.txt from {len(SOURCES)} sources")
```

Run: `python scripts/build_llms_full.py` → generates `llms-full.txt`.

- [ ] **Step 4: Verify green**

Run: `pytest tests/test_llms_docs.py -v && pytest -q && ruff check . && mypy` → all PASS

- [ ] **Step 5: Commit**

```bash
git add llms.txt llms-full.txt AGENTS.md scripts/ tests/test_llms_docs.py
git commit -m "feat: AI-agent onboarding docs (llms.txt, llms-full.txt, AGENTS.md)"
```

---

## Done criteria (whole plan)

- `pytest -q` green (≈35 tests), `ruff check .` + `ruff format --check .` clean, `mypy` (strict) clean.
- `zolva validate examples/mockbank/agents` exits 0.
- CI workflow green on 3.11/3.12/3.13.
- Every public interface listed in a task's **Produces** block exists with that exact signature — plugin plans (guardrails, evals, feedback, audit, synthetics) build against them.
