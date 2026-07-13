# Contributing to Zolva

Thanks for helping build the open agent platform for banks and fintechs.

## The short version

1. Fork, branch, and make your change with a failing test first (TDD).
2. All four gates must be green before you open a PR:
   ```bash
   pytest -q && ruff check . && ruff format --check . && mypy
   ```
3. Conventional commits: `feat:`, `fix:`, `test:`, `docs:`, `chore:`.
4. Open the PR. CI runs the same gates plus `bandit` and `pip-audit`.

The full contract (setup commands, conventions, what NOT to do) lives in
[AGENTS.md](AGENTS.md). It binds human and AI contributors alike; if you use an
AI coding agent, point it at that file first.

## Hard rules

- Runtime dependencies are frozen: `pydantic`, `httpx`, `pyyaml`. PRs adding a
  runtime dependency will be declined unless the maintainers agreed first.
- `yaml.safe_load` only. No `eval`, `exec`, or `pickle`, anywhere, ever.
- A bugfix PR includes the failing test that reproduces the bug.
- Test output must be pristine: no warnings, no stray prints.
- Public interfaces are typed; `mypy --strict` stays clean.

## Good first contributions

- Handover backends for ticketing systems (Freshdesk, Zendesk, Salesforce):
  subclass one `HandoverBackend` class, ~50 lines each.
- Bridge adapters for additional LLM providers or internal gateways.
- Eval cohorts and adversarial synthetic personas for common banking flows.

## Security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).

## License

By contributing you agree your work is licensed under Apache-2.0.
