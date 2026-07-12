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
