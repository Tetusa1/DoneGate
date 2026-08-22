"""Argparse CLI for agent-worktree task state, validation, and recovery."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .git import GitRepository, GitRepositoryError
from .models import TaskValidationError, load_task_file
from .recovery import CleanupError, CleanupOrchestrator, RecoveryOrchestrator
from .state import (
    StateStoreError,
    TaskNotFoundError,
    TaskStore,
    ValidationAlreadyRunningError,
)
from .validate import CompletionValidator


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


def _task_validate(args: argparse.Namespace) -> int:
    try:
        report = CompletionValidator(_store_from_cwd()).validate(args.task)
    except ValidationAlreadyRunningError as exc:
        if args.json:
            print(json.dumps({"status": "blocked", "error": str(exc)}, sort_keys=True))
        else:
            print(str(exc), file=sys.stderr)
        return 2
    except TaskNotFoundError as exc:
        if args.json:
            print(json.dumps({"status": "blocked", "error": str(exc)}, sort_keys=True))
        else:
            print(str(exc), file=sys.stderr)
        return 2
    except (GitRepositoryError, StateStoreError) as exc:
        if args.json:
            print(json.dumps({"status": "blocked", "error": str(exc)}, sort_keys=True))
        else:
            print(f"TASK_VALIDATE_FAILED: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"TASK_ID: {report.task_id}")
        print(f"VALIDATION_ID: {report.validation_id}")
        print(f"STATUS: {report.status.value}")
        print(f"EXECUTION: {report.execution_id or 'missing'}")
        print(f"COMMIT: {report.verified_commit or 'missing'}")
        print(
            "CHANGED_FILES: "
            + (", ".join(report.actual_changed_files) if report.actual_changed_files else "-")
        )
        check_text = ", ".join(
            f"{check.name}={check.status.value}" for check in report.required_check_results
        )
        print(f"CHECKS: {check_text or '-'}")
        print(
            "BLOCKERS: "
            + (", ".join(report.blocking_reasons) if report.blocking_reasons else "-")
        )
    return 0 if report.status.value == "passed" else 1


def _task_cleanup(args: argparse.Namespace) -> int:
    try:
        result = CleanupOrchestrator(_store_from_cwd()).cleanup(
            args.task,
            remove_branch=args.remove_branch,
        )
    except TaskNotFoundError as exc:
        payload = {"status": "blocked", "error": str(exc)}
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(str(exc), file=sys.stderr)
        return 2
    except (CleanupError, GitRepositoryError, StateStoreError) as exc:
        payload = {"status": "blocked", "error": str(exc)}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"TASK_CLEANUP_BLOCKED: {exc}", file=sys.stderr)
        return 2

    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"TASK: {result.task_id}")
        print(f"STATE_BEFORE: {result.state_before.value}")
        print(f"WORKTREE: {result.worktree or '-'}")
        print(f"LEASES: {', '.join(result.leases) or '-'}")
        print(f"BRANCH: {result.branch or '-'}")
        print(f"RESULT: {result.result}")
        print(f"STATE_AFTER: {result.state_after.value}")
        print(f"BLOCKERS: {', '.join(result.blockers) or '-'}")
        print(f"ACTIONS: {', '.join(result.actions) or '-'}")
    return 0


def _recover(args: argparse.Namespace) -> int:
    mode = "apply" if args.apply else "dry-run"
    try:
        report = RecoveryOrchestrator(_store_from_cwd()).recover(mode)
    except (GitRepositoryError, StateStoreError) as exc:
        if args.json:
            print(json.dumps({"status": "blocked", "error": str(exc)}, sort_keys=True))
        else:
            print(f"RECOVERY_FAILED: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"RECOVERY_ID: {report.recovery_id}")
        print(f"MODE: {report.mode}")
        print(f"FINDINGS: {len(report.findings)}")
        print(f"ACTIONS: {len(report.actions)}")
        print(f"SKIPPED: {len(report.skipped)}")
        print(f"ERRORS: {len(report.errors)}")
        print(f"REPORT: {report.artifact_dir}")
    return 0 if not report.errors else 1


def _store_from_cwd() -> TaskStore:
    repository = GitRepository(Path.cwd())
    override = os.environ.get("AGENT_WORKTREE_STATE_PATH")
    db_path = Path(override) if override else None
    return TaskStore(repository.root, db_path=db_path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-worktree",
        description="Coordinate coding-agent task state, validation, cleanup, and recovery.",
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

    validate = task_commands.add_parser("validate", help="Validate a task completion")
    validate.add_argument("--task", required=True, help="Task ID")
    validate.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    validate.set_defaults(handler=_task_validate)

    run = task_commands.add_parser("run", help="Run a task")
    run.add_argument("--task", required=True, help="Task ID")
    run.set_defaults(handler=lambda _args: _not_implemented("task run"))

    cleanup = task_commands.add_parser("cleanup", help="Clean up a terminal task")
    cleanup.add_argument("--task", required=True, help="Task ID")
    cleanup.add_argument(
        "--remove-branch",
        action="store_true",
        help="Delete the exact managed branch only after safe worktree removal",
    )
    cleanup.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    cleanup.set_defaults(handler=_task_cleanup)

    recover = commands.add_parser("recover", help="Recover stale runtime state")
    modes = recover.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    recover.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    recover.set_defaults(handler=_recover)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return int(args.handler(args))
