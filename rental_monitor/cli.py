from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from .core import poll_once, run_forever, test_email


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor official French rental-alert emails.")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--database", type=Path, default=Path(".data/monitor.sqlite3"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create local config and environment templates")
    commands.add_parser("once", help="Check the mailbox once")
    commands.add_parser("run", help="Keep checking the mailbox on the configured interval")
    commands.add_parser("test-email", help="Send a test message using configured SMTP")
    return parser


def _initialize(config_path: Path, env_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    examples = ((root / "config.example.json", config_path), (root / ".env.example", env_path))
    for source, destination in examples:
        if destination.exists():
            raise FileExistsError(f"Refusing to overwrite {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        print(f"Created {destination}")
    print("Edit config.json and .env before starting the monitor.")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        if args.command == "init":
            _initialize(args.config, args.env_file)
        elif args.command == "once":
            poll_once(args.config, args.database, args.env_file)
        elif args.command == "run":
            run_forever(args.config, args.database, args.env_file)
        elif args.command == "test-email":
            test_email(args.config, args.env_file)
            print("Test email sent.")
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        logging.error("%s", error)
        return 1
    except KeyboardInterrupt:
        print("\nMonitor stopped.", file=sys.stderr)
        return 130
