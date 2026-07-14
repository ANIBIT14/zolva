# Security Policy

Zolva is built for banks and fintechs; security reports get priority over everything else.

## Reporting a vulnerability

**Do not open a public issue.** Use GitHub's private vulnerability reporting:
[github.com/ANIBIT14/zolva/security/advisories/new](https://github.com/ANIBIT14/zolva/security/advisories/new)
("Report a vulnerability" under the repo's Security tab). Include:

- A description of the issue and the affected component (config loader, tool registry, bridge, orchestrator, handover, CLI)
- Reproduction steps or a proof-of-concept
- The version/commit you tested against

You will get an acknowledgement within 72 hours and a remediation timeline within 7 days. We ask for coordinated disclosure: give us 90 days (or an agreed timeline) before publishing.

## Scope notes

- Zolva is self-hosted: the runtime never sends data anywhere except the LLM provider the operator configures. Reports about provider-side handling are out of scope.
- Prompt-injection findings against the orchestrator's tool-result handling, guardrail bypasses, and secrets handling in the config loader are all firmly in scope.

## Supported versions

Pre-1.0: only the latest release receives security fixes.
