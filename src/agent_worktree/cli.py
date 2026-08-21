"""Argparse CLI skeleton for agent-worktree Task 01."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .models import TaskValidationError, load_task_file


def _not_implemented(action: str) -> int:
    print(f"NOT_IMPLEMENTED_IN_TASK_01: {action}", file=sys.stderr)
    return 2


def _task_create(args: argparse.Namespace) -> int:
    try:
        task = load_task_file(Path(args.file))
    except TaskValidationError as exc:
        print(f"TASK_SCHEMA_INVALID: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {"status": "validated", "task": task.to_dict()},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-worktree",
        description="Validate coding-agent tasks before worktree runtime is enabled.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    task = commands.add_parser("task", help="Task operations")
    task_commands = task.add_subparsers(dest="task_command", required=True)

    create = task_commands.add_parser("create", help="Validate and normalize a task file")
    create.add_argument("--file", required=True, help="YAML task definition")
    create.set_defaults(handler=_task_create)

    for name in ("run", "status", "validate", "cleanup"):
        command = task_commands.add_parser(name, help=f"{name.title()} a task")
        command.add_argument("--task", required=True, help="Task ID")
        command.set_defaults(handler=lambda _args, action=f"task {name}": _not_implemented(action))

    recover = commands.add_parser("recover", help="Recover stale runtime state")
    modes = recover.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    recover.set_defaults(handler=lambda _args: _not_implemented("recover"))

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return int(args.handler(args))
