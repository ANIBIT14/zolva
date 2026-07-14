from pathlib import Path

import pytest

from tests.test_orchestrator import CapturingHandover
from zolva.bridge import LLMResponse
from zolva.bridge.fake import FakeAdapter
from zolva.config import ConfigError
from zolva.orchestrator import BLOCKED_MESSAGE, AgentApp


def write_config(root: Path, policy: bool = True) -> Path:
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "cx.md").write_text("Help customers.")
    (agents / "cx.yaml").write_text(
        "name: cx-agent\ninstructions: cx.md\n"
        "model: { provider: test, name: m }\n"
        "guardrails: policies/cx.yaml\n"
    )
    if policy:
        (agents / "policies").mkdir()
        (agents / "policies" / "cx.yaml").write_text(
            'post:\n  - require_disclaimer: { when: "mutual fund", text: "Market risks." }\n'
        )
    return agents


async def test_guardrails_auto_attached_from_agent_yaml(tmp_path: Path) -> None:
    agents_dir = write_config(tmp_path)
    handover = CapturingHandover()
    app = AgentApp.from_config(
        agents_dir,
        adapter=FakeAdapter(script=[LLMResponse(text="Buy our mutual fund!")]),
        handover=handover,
    )
    assert await app.run("cx-agent", "s1", "advice?") == BLOCKED_MESSAGE
    assert handover.tickets[0].reason == "required disclaimer missing"


def test_missing_policy_file_fails_startup(tmp_path: Path) -> None:
    agents_dir = write_config(tmp_path, policy=False)
    with pytest.raises(ConfigError, match="policy file not found"):
        AgentApp.from_config(agents_dir)


def test_missing_evals_path_fails_startup(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir(parents=True)
    (agents / "cx.md").write_text("Help customers.")
    (agents / "cx.yaml").write_text(
        "name: cx-agent\ninstructions: cx.md\n"
        "model: { provider: test, name: m }\n"
        "evals: missing.yaml\n"
    )
    with pytest.raises(ConfigError, match="evals"):
        AgentApp.from_config(agents)
