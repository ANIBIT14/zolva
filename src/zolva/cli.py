"""zolva CLI. v0.1: validate. Plugins add subcommands (eval, triage, scorecard) later."""

from __future__ import annotations

import argparse
import sys

from zolva.config import ConfigError, load_agents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zolva")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="validate an agent config directory")
    validate.add_argument("config_dir")
    args = parser.parse_args(argv)

    if args.command == "validate":
        try:
            agents = load_agents(args.config_dir)
        except ConfigError as e:
            print(f"config error: {e}", file=sys.stderr)
            return 1
        for cfg in agents.values():
            print(
                f"{cfg.name}  {cfg.model.provider}/{cfg.model.name}  "
                f"tools={len(cfg.tools)}  handoffs={cfg.handoffs}"
            )
        print(f"OK: {len(agents)} agent(s) valid")
        return 0
    return 1
