"""zolva CLI: validate, eval, synthetics, scorecard, dashboard, triage, export-dataset."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from typing import Any

from zolva.bridge import BridgeError
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
    from pathlib import Path

    from zolva.evals import load_cohorts_from_agents
    from zolva.guardrails import validate_policy_file

    agents = load_agents(args.config_dir)
    for cfg in agents.values():
        # same checks the runtime performs at startup: a config that passes
        # `zolva validate` in CI must not fail AgentApp.from_config later
        if cfg.guardrails:
            policy_path = Path(args.config_dir) / cfg.guardrails
            if not policy_path.is_file():
                raise ConfigError(f"agent {cfg.name!r}: policy file not found: {policy_path}")
            validate_policy_file(policy_path)
        print(
            f"{cfg.name}  {cfg.model.provider}/{cfg.model.name}  "
            f"tools={len(cfg.tools)}  handoffs={cfg.handoffs}"
        )
    cohorts = load_cohorts_from_agents(args.config_dir, required=False)
    print(f"OK: {len(agents)} agent(s) valid, {len(cohorts)} eval cohort(s) parsed")
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    from zolva.bridge import get_adapter
    from zolva.evals import EvalRunner, load_cohorts_from_agents

    if bool(args.evals_dir) == bool(args.agents):
        print("eval: pass exactly one of evals_dir or --agents", file=sys.stderr)
        return 1

    app = _load_app(args.app)
    judge = get_adapter(args.judge_provider) if args.judge_provider else None
    runner = EvalRunner(app, judge=judge, judge_model=args.judge_model)
    if args.agents:
        report = asyncio.run(runner.run_cohorts(load_cohorts_from_agents(args.agents)))
    else:
        report = asyncio.run(runner.run(args.evals_dir))
    print(report.summary())
    if args.out:
        with open(args.out, "w") as f:
            json.dump(report.model_dump(), f, indent=2)
        print(f"wrote {args.out}")
    if args.gate and not report.gate_passed:
        return 1  # the CI story: any CI can run a command that exits 1
    return 0


def _cmd_synthetics(args: argparse.Namespace) -> int:
    from zolva.bridge import get_adapter
    from zolva.synthetics import SyntheticRunner, gate_passed, results_to_json

    app = _load_app(args.app)
    runner = SyntheticRunner(
        app,
        driver=get_adapter(args.driver_provider),
        judge=get_adapter(args.judge_provider),
        driver_model=args.driver_model,
        judge_model=args.judge_model,
    )
    results = asyncio.run(runner.run(args.synthetics_dir))
    for r in results:
        print(f"{r.name:24s} {'PASS' if r.passed else 'FAIL'}")
    print(f"GATE: {'PASS' if gate_passed(results) else 'FAIL'}")
    if args.out:
        with open(args.out, "w") as f:
            json.dump(results_to_json(results), f, indent=2)
        print(f"wrote {args.out}")
    if args.gate and not gate_passed(results):
        return 1
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


def _cmd_dashboard(args: argparse.Namespace) -> int:
    from importlib.util import find_spec

    if find_spec("fastapi") is None or find_spec("uvicorn") is None:
        print(
            'dashboard requires the optional extra: pip install "zolva[dashboard]"',
            file=sys.stderr,
        )
        return 1
    from zolva import dashboard

    dashboard.serve(args.config_dir, args.audit, host=args.host, port=args.port)
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
    p_eval.add_argument("evals_dir", nargs="?", default="")
    p_eval.add_argument(
        "--agents",
        default="",
        help="agent config dir; run the cohorts its YAMLs declare via evals:",
    )
    p_eval.add_argument("--app", required=True, help="import path to your AgentApp: module:attr")
    p_eval.add_argument("--gate", action="store_true", help="exit 1 if the worst cohort fails")
    p_eval.add_argument("--judge-provider", default="", help="bridge provider for the judge")
    p_eval.add_argument("--judge-model", default="", help="model name for the judge")
    p_eval.add_argument("--out", default="", help="write full report JSON to this path")

    p_synth = sub.add_parser("synthetics", help="run synthetic conversations against your app")
    p_synth.add_argument("synthetics_dir")
    p_synth.add_argument("--app", required=True, help="import path to your AgentApp: module:attr")
    p_synth.add_argument("--driver-provider", required=True, help="bridge provider for the driver")
    p_synth.add_argument("--driver-model", default="", help="model name for the driver")
    p_synth.add_argument("--judge-provider", required=True, help="bridge provider for the judge")
    p_synth.add_argument("--judge-model", default="", help="model name for the judge")
    p_synth.add_argument("--gate", action="store_true", help="exit 1 if any synthetic fails")
    p_synth.add_argument("--out", default="", help="write full results JSON to this path")

    p_score = sub.add_parser("scorecard", help="verify the audit chain and print SARR")
    p_score.add_argument("audit_db")

    p_dash = sub.add_parser("dashboard", help="serve the local read-only dashboard UI")
    p_dash.add_argument("config_dir", nargs="?", default=None, help="agent config dir (topology)")
    p_dash.add_argument("--audit", default="audit.sqlite", help="audit DB path (opened read-only)")
    p_dash.add_argument("--host", default="127.0.0.1", help="bind address (default localhost)")
    p_dash.add_argument("--port", type=int, default=8600)

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
        "synthetics": _cmd_synthetics,
        "scorecard": _cmd_scorecard,
        "dashboard": _cmd_dashboard,
        "triage": _cmd_triage,
        "export-dataset": _cmd_export_dataset,
    }
    try:
        result: int = commands[args.command](args)
        return result
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    except BridgeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
