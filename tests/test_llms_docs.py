from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_llms_txt_exists_and_is_valid() -> None:
    text = (ROOT / "llms.txt").read_text()
    assert text.startswith("# ")  # llmstxt.org: H1 first
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
    assert (ROOT / "llms-full.txt").read_text() == before, (
        "llms-full.txt stale: run scripts/build_llms_full.py"
    )
