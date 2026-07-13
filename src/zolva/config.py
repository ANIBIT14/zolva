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


def load_yaml_dir(dir_path: str | Path, what: str) -> list[tuple[Path, dict[str, Any]]]:
    """Load every *.yaml/*.yml mapping in a directory. Shared by agents/evals/synthetics."""
    root = Path(dir_path)
    if not root.is_dir():
        raise ConfigError(f"{what} dir not found: {root}")
    paths = sorted(p for p in root.iterdir() if p.suffix in {".yaml", ".yml"})
    if not paths:
        raise ConfigError(f"no {what} files found in {root}")
    out: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: top level must be a mapping")
        out.append((path, raw))
    return out


def load_agents(config_dir: str | Path) -> dict[str, AgentConfig]:
    """Load every *.yaml agent in config_dir. instructions: path is relative to the YAML file."""
    agents: dict[str, AgentConfig] = {}
    for path, raw in load_yaml_dir(config_dir, "agent config"):
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
        if cfg.name in agents:
            raise ConfigError(f"duplicate agent name {cfg.name!r} (second definition: {path})")
        agents[cfg.name] = cfg
    for cfg in agents.values():
        for target in cfg.handoffs:
            if target != "human-escalation" and target not in agents:
                raise ConfigError(f"agent {cfg.name!r} hands off to unknown agent {target!r}")
    return agents
