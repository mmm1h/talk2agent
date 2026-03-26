from __future__ import annotations

import argparse
from pathlib import Path

from talk2agent.app import run_app
from talk2agent.config import load_config, write_default_config
from talk2agent.harness import run_harness


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="talk2agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--config", default="config.yaml")

    subparsers.add_parser("harness")

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--config", default="config.yaml")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        write_default_config(Path(args.config))
        return 0
    if args.command == "harness":
        return run_harness()
    if args.command == "start":
        config = load_config(Path(args.config))
        return run_app(config)
    raise AssertionError("unreachable")
