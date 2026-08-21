"""Argparse CLI skeleton for agent-worktree Task 01."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .git import GitRepository, GitRepositoryError
from .models import TaskValidationError, load_task_file
from .state import StateStoreError, TaskNotFoundError, TaskStore


def _not_implemented(action: str) -> int:
    print(f"NOT_IMPLEMENTED_IN_TASK_01: {action}", file=sys.stderr)
    return 2


def _task_create(args: argparse.Namespace) -> int:
    try:
        task = load_task_file(Path(args.file))
    except TaskValidationError as exc:
        print(f"TASK_SCHEMA_INVALID: {exc}", file=sys.stderr)
        return 2

    try:
        record = _store_from_cwd().create(task)
    except (GitRepositoryError, StateStoreError) as exc:
        print(f"TASK_CREATE_FAILED: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {"status": "validated", "persisted": True, "task": record.to_dict()},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _task_status(args: argparse.Namespace) -> int:
    try:
        record = _store_from_cwd().get(args.task)
    except TaskNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (GitRepositoryError, StateStoreError) as exc:
        print(f"TASK_STATUS_FAILED: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"TASK_ID: {record.task_id}")
        print(f"STATE: {record.state.value}")
        print(f"BASE_REF: {record.base_ref}")
        print(f"WORKER: {record.worker_id}")
        print(f"CREATED_AT: {record.to_dict()['created_at']}")
        print(f"UPDATED_AT: {record.to_dict()['updated_at']}")
        print(f"VERSION: {record.version}")
        if record.failure_reason is not None:
            print(f"FAILURE_REASON: {record.failure_reason}")
    return 0


def _store_from_cwd() -> TaskStore:
    repository = GitRepository(Path.cwd())
    override = os.environ.get("AGENT_WORKTREE_STATE_PATH")
    db_path = Path(override) if override else None
    return TaskStore(repository.root, db_path=db_path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-worktree",
        description="Persist coding-agent task state before worktree runtime is enabled.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    task = commands.add_parser("task", help="Task operations")
    task_commands = task.add_subparsers(dest="task_command", required=True)

    create = task_commands.add_parser("create", help="Validate, normalize, and persist a task file")
    create.add_argument("--file", required=True, help="YAML task definition")
    create.set_defaults(handler=_task_create)

    status = task_commands.add_parser("status", help="Read a persisted task")
    status.add_argument("--task", required=True, help="Task ID")
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    status.set_defaults(handler=_task_status)

    for name in ("run", "validate", "cleanup"):
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
