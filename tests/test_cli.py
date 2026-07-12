from pathlib import Path

import pytest

from tests.test_config import make_agent_dir
from zolva.cli import main


def test_validate_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(make_agent_dir(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "collections-agent" in out and "openai/gpt-5" in out


def test_validate_bad_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "agents"
    bad.mkdir()
    (bad / "a.yaml").write_text(
        "name: a\ninstructions: missing.md\nmodel: {provider: p, name: n}\n"
    )
    assert main(["validate", str(bad)]) == 1
    assert "not found" in capsys.readouterr().err
