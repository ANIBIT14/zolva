# Instructions for AI agents setting up Zolva

You are setting this up inside a bank/fintech codebase. Follow exactly; verify every step.

## Setup

```bash
python3 --version                 # must be >= 3.11
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Verify the installation (run ALL — do not skip)

```bash
pytest -q                         # expect: all tests pass
ruff check . && ruff format --check .
mypy                              # strict; must be clean
zolva validate examples/mockbank/agents   # expect: "OK: 1 agent(s) valid"
```

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
- Conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`).
- After editing docs, run `python scripts/build_llms_full.py` and commit `llms-full.txt`.
