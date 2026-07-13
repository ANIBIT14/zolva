"""zolva CLI: validate, eval, scorecard, triage, export-dataset."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from typing import Any

from zolva.config import ConfigError, load_agents
from zolva.orchestrator import AgentApp


def _load_app(spec: str) -> AgentApp:
    """Import 'module.path:attr' and return the bank's AgentApp instance."""
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise ConfigError(f"--app must be 'module.path:attr', got {spec!r}")
    # console-script entry points don't put CWD on sys.path the way `python x.py` does;
    # a bank runs `zolva eval --app app:app` from its project root, so make that work
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        raise ConfigError(f"could not import {module_name!r} for --app (cwd={cwd}): {e}") from e
    app = getattr(module, attr, None)
    if not isinstance(app, AgentApp):
        raise ConfigError(f"{spec} is not an AgentApp instance")
    return app


def _cmd_validate(args: argparse.Namespace) -> int:
    agents = load_agents(args.config_dir)
    for cfg in agents.values():
        print(
            f"{cfg.name}  {cfg.model.provider}/{cfg.model.name}  "
            f"tools={len(cfg.tools)}  handoffs={cfg.handoffs}"
        )
    print(f"OK: {len(agents)} agent(s) valid")
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    from zolva.bridge import get_adapter
    from zolva.evals import EvalRunner

    app = _load_app(args.app)
    judge = get_adapter(args.judge_provider) if args.judge_provider else None
    runner = EvalRunner(app, judge=judge, judge_model=args.judge_model)
    report = asyncio.run(runner.run(args.evals_dir))
    print(report.summary())
    if args.out:
        with open(args.out, "w") as f:
            json.dump(report.model_dump(), f, indent=2)
        print(f"wrote {args.out}")
    if args.gate and not report.gate_passed:
        return 1  # the CI story: any CI can run a command that exits 1
    return 0


def _cmd_scorecard(args: argparse.Namespace) -> int:
    from zolva.audit import AuditLog, scorecard

    log = AuditLog(args.audit_db)
    if not log.verify():
        print("AUDIT CHAIN BROKEN: log has been tampered with", file=sys.stderr)
        return 1
    print(scorecard(log).summary())
    return 0


def _cmd_triage(args: argparse.Namespace) -> int:
    from zolva.feedback import FeedbackQueue

    q = FeedbackQueue(args.failures_db)
    if args.accept is not None:
        if not args.cohort or not args.expect:
            print("--accept requires --cohort and --expect", file=sys.stderr)
            return 1
        q.accept(args.accept, args.cohort, expect=args.expect)
        print(f"failure {args.accept} promoted to {args.cohort}")
        return 0
    if args.reject is not None:
        q.reject(args.reject)
        print(f"failure {args.reject} rejected")
        return 0
    pending = q.pending()
    for f in pending:
        last_user = next((m.content for m in reversed(f.transcript) if m.role == "user"), "")
        print(f"#{f.id}  [{f.kind}]  agent={f.agent}  note={f.note!r}  last_user={last_user!r}")
    print(f"{len(pending)} pending failure(s)")
    return 0


def _cmd_export_dataset(args: argparse.Namespace) -> int:
    from zolva.feedback import FeedbackQueue

    n = FeedbackQueue(args.failures_db).export_dataset(args.out)
    print(f"wrote {n} accepted failure(s) to {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zolva")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="validate an agent config directory")
    p_validate.add_argument("config_dir")

    p_eval = sub.add_parser("eval", help="run eval cohorts against your app")
    p_eval.add_argument("evals_dir")
    p_eval.add_argument("--app", required=True, help="import path to your AgentApp: module:attr")
    p_eval.add_argument("--gate", action="store_true", help="exit 1 if the worst cohort fails")
    p_eval.add_argument("--judge-provider", default="", help="bridge provider for the judge")
    p_eval.add_argument("--judge-model", default="", help="model name for the judge")
    p_eval.add_argument("--out", default="", help="write full report JSON to this path")

    p_score = sub.add_parser("scorecard", help="verify the audit chain and print SARR")
    p_score.add_argument("audit_db")

    p_triage = sub.add_parser("triage", help="list/promote/reject pending failures")
    p_triage.add_argument("failures_db")
    p_triage.add_argument("--accept", type=int, default=None, metavar="ID")
    p_triage.add_argument("--cohort", default="", help="eval cohort file to promote into")
    p_triage.add_argument("--expect", default="", help="expected behavior for the judge")
    p_triage.add_argument("--reject", type=int, default=None, metavar="ID")

    p_export = sub.add_parser("export-dataset", help="accepted failures as fine-tuning JSONL")
    p_export.add_argument("failures_db")
    p_export.add_argument("out")

    args = parser.parse_args(argv)
    commands: dict[str, Any] = {
        "validate": _cmd_validate,
        "eval": _cmd_eval,
        "scorecard": _cmd_scorecard,
        "triage": _cmd_triage,
        "export-dataset": _cmd_export_dataset,
    }
    try:
        result: int = commands[args.command](args)
        return result
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
