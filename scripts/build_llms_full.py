"""Concatenate the docs an LLM needs into llms-full.txt. Run after any docs change."""

from pathlib import Path

ROOT = Path(__file__).parent.parent
SOURCES = [
    "llms.txt",
    "AGENTS.md",
    "README.md",
    "docs/specs/2026-07-12-zolva-design.md",
]

parts = [f"<!-- {src} -->\n\n{(ROOT / src).read_text()}" for src in SOURCES]
(ROOT / "llms-full.txt").write_text("\n\n---\n\n".join(parts) + "\n")
print(f"wrote llms-full.txt from {len(SOURCES)} sources")
