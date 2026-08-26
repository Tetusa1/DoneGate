"""Independent, fail-closed completion validation for coding-agent tasks."""

from __future__ import annotations

import math
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .evidence import (
    ExecutionArtifact,
    ValidationArtifact,
    artifact_paths,
    create_validation_artifact,
    read_execution_metadata,
    write_validation_report,
)
from .git import GitRepository, GitRepositoryError, WorktreeInfo
from .leases import LeaseManager, canonicalize_lease_paths, paths_overlap
from .models import (
    CheckStatus,
    ExecutionResult,
    ExecutionStatus,
    RequiredCheck,
    TaskRecord,
    TaskState,
    ValidationCheckResult,
    ValidationReport,
    ValidationStatus,
)
from .state import TaskStore, utc_now
from .worker import redact_command_for_storage


VALIDATION_FAILURE_CODES = frozenset(
    {
        "worker_not_succeeded",
        "worktree_missing",
        "worktree_not_registered",
        "branch_mismatch",
        "base_commit_invalid",
        "commit_missing",
        "commit_invalid",
        "commit_not_descendant",
        "no_new_commit",
        "dirty_worktree",
        "write_scope_violation",
        "denylist_violation",
        "lease_missing",
        "lease_expired",
        "required_check_start_failed",
        "required_check_failed",
        "required_check_timeout",
        "read_only_changed",
        "execution_evidence_missing",
        "execution_relationship_invalid",
        "task_not_running",
        "validation_internal_error",
    }
)


class ValidationError(RuntimeError):
    """Base error for completion validation."""


class CheckRunnerError(ValidationError):
    """Raised for an infrastructure failure while running a required check."""


class CompletionValidator:
    """Verify Git, lease, execution, scope, and check evidence before completion."""

    def __init__(
        self,
        store: TaskStore,
        *,
        repository: GitRepository | None = None,
        lease_manager: LeaseManager | None = None,
        clock=utc_now,
        check_grace_seconds: float = 0.2,
    ) -> None:
        if not isinstance(check_grace_seconds, (int, float)) or not math.isfinite(
            check_grace_seconds
        ) or check_grace_seconds < 0:
            raise ValidationError("check_grace_seconds must be finite and non-negative")
        self.store = store
        self.repository = repository or GitRepository(store.repo_root)
        self.lease_manager = lease_manager or LeaseManager(store, clock=clock)
        self._clock = clock
        self.check_grace_seconds = float(check_grace_seconds)

    def validate(
        self,
        task_id: str,
        *,
        execution_id: str | None = None,
    ) -> ValidationReport:
        task = self.store.get(task_id)
        execution = self._select_execution(task_id, execution_id)
        validation_id = str(uuid.uuid4())
        started_at = self._clock()
        worker_id = execution.worker_id if execution is not None else task.worker_id
        execution_status = execution.status if execution is not None else None
        artifact = _unmaterialized_validation_artifact(self.repository.root, validation_id)
        initial = self._blank_report(
            validation_id=validation_id,
            task=task,
            execution=execution,
            artifact=artifact,
            started_at=started_at,
            status=ValidationStatus.RUNNING,
            worker_id=worker_id,
            execution_status=execution_status,
        )
        self.store.begin_validation(initial)
        try:
            artifact = create_validation_artifact(self.repository.root, validation_id)
        except Exception:
            self.store.rollback_validation_claim(validation_id)
            raise

        try:
            report = self._evaluate(task, execution, artifact, initial)
        except Exception as exc:
            report = replace(
                initial,
                status=ValidationStatus.BLOCKED,
                finished_at=self._clock(),
                blocking_reasons=("validation_internal_error",),
            )
            self._write_report(artifact, report, internal_error=str(exc))
            persisted = self.store.finish_validation(report)
            self._transition_after_validation(task_id, persisted)
            return persisted

        if report.status is ValidationStatus.PASSED and task.state is TaskState.RUNNING:
            try:
                self.store.transition(task_id, TaskState.COMPLETED)
            except Exception as exc:
                report = replace(
                    report,
                    status=ValidationStatus.BLOCKED,
                    blocking_reasons=_append_reason(
                        report.blocking_reasons, "validation_internal_error"
                    ),
                )
                self._write_report(artifact, report, internal_error=str(exc))
                persisted = self.store.finish_validation(report)
                self._transition_after_validation(task_id, persisted)
                return persisted

        self._write_report(artifact, report)
        persisted = self.store.finish_validation(report)
        self._transition_after_validation(task_id, persisted)
        return persisted

    def _evaluate(
        self,
        task: TaskRecord,
        execution: ExecutionResult | None,
        artifact: ValidationArtifact,
        initial: ValidationReport,
    ) -> ValidationReport:
        reasons: list[str] = []
        allowlist_violations: tuple[str, ...] = ()
        denylist_violations: tuple[str, ...] = ()
        checks: tuple[ValidationCheckResult, ...] = ()
        actual_changed_files: tuple[str, ...] = ()
        actual_branch: str | None = None
        base_commit: str | None = None
        reported_commit: str | None = None
        verified_commit: str | None = None
        lease_valid: bool | None = None
        writable = bool(task.definition.write_paths)

        if task.state not in {TaskState.RUNNING, TaskState.COMPLETED}:
            reasons.append("task_not_running")
        if execution is None or execution.status is not ExecutionStatus.SUCCEEDED:
            reasons.append("worker_not_succeeded")
        elif execution.task_id != task.task_id or execution.worker_id != task.worker_id:
            reasons.append("execution_relationship_invalid")
        elif not self._execution_artifact_is_valid(execution):
            reasons.append("execution_evidence_missing")

        worktree_path, worktree_info, worktree_reasons = self._verify_worktree(task)
        reasons.extend(worktree_reasons)
        if worktree_info is not None:
            actual_branch = worktree_info.branch

        if execution is not None and execution.status is ExecutionStatus.SUCCEEDED:
            metadata = self._execution_metadata(execution)
            if metadata is not None:
                value = metadata.get("reported_commit")
                if isinstance(value, str) and value.strip():
                    reported_commit = value.strip()
                elif writable:
                    reasons.append("commit_missing")
            else:
                reasons.append("execution_evidence_missing")

        if worktree_path is not None and worktree_info is not None:
            try:
                actual_head = self.repository.head_at(worktree_path)
            except GitRepositoryError:
                actual_head = None
                reasons.append("worktree_missing")
            base_ref = task.base_commit or task.base_ref
            try:
                base_commit = self.repository.resolve_commit(base_ref)
            except GitRepositoryError:
                reasons.append("base_commit_invalid")

            if writable and reported_commit is not None:
                object_type = self.repository.object_type(reported_commit)
                if object_type is None:
                    reasons.append("commit_missing")
                elif object_type != "commit":
                    reasons.append("commit_invalid")
                else:
                    try:
                        reported_full = self.repository.resolve_commit(reported_commit)
                    except GitRepositoryError:
                        reported_full = None
                        reasons.append("commit_invalid")
                    if reported_full is not None:
                        if actual_head != reported_full:
                            reasons.append("commit_not_descendant")
                        elif base_commit is not None and not self.repository.is_ancestor(
                            base_commit, reported_full
                        ):
                            reasons.append("commit_not_descendant")
                        elif base_commit == reported_full:
                            reasons.append("no_new_commit")
                        else:
                            verified_commit = actual_head
            elif not writable and actual_head is not None:
                # Read-only tasks do not require a worker commit claim, but any
                # committed change from the declared base still violates the
                # empty write scope below.
                verified_commit = actual_head
                if base_commit is not None and actual_head != base_commit:
                    reasons.append("read_only_changed")

            if verified_commit is not None and base_commit is not None:
                actual_changed_files = self.repository.changed_files_between(
                    base_commit, verified_commit
                )
            try:
                if not self.repository.is_clean_at(worktree_path):
                    reasons.append("dirty_worktree")
            except GitRepositoryError:
                reasons.append("worktree_missing")

        if actual_changed_files:
            allowlist_violations, denylist_violations = self._scope_violations(
                actual_changed_files, task
            )
            if allowlist_violations:
                reasons.append("write_scope_violation")
            if denylist_violations:
                reasons.append("denylist_violation")

        lease_valid = self._lease_valid(task, execution, reasons)

        if (
            execution is not None
            and execution.status is ExecutionStatus.SUCCEEDED
            and worktree_path is not None
            and worktree_info is not None
        ):
            checks = self._run_required_checks(task.definition.required_checks, worktree_path, artifact)
            for check in checks:
                if check.status is CheckStatus.START_FAILED:
                    reasons.append("required_check_start_failed")
                elif check.status is CheckStatus.TIMED_OUT:
                    reasons.append("required_check_timeout")
                elif check.status is CheckStatus.FAILED:
                    reasons.append("required_check_failed")

        reasons = list(_unique(reasons))
        status = ValidationStatus.PASSED if not reasons else ValidationStatus.FAILED
        return replace(
            initial,
            status=status,
            finished_at=self._clock(),
            expected_branch=task.branch_name,
            actual_branch=actual_branch,
            base_commit=base_commit,
            reported_commit=reported_commit,
            verified_commit=verified_commit,
            actual_changed_files=actual_changed_files,
            allowlist_violations=allowlist_violations,
            denylist_violations=denylist_violations,
            required_check_results=checks,
            lease_valid=lease_valid,
            blocking_reasons=tuple(reasons),
        )

    def _verify_worktree(
        self, task: TaskRecord
    ) -> tuple[Path | None, WorktreeInfo | None, tuple[str, ...]]:
        raw_path = task.worktree_path
        if not raw_path:
            return None, None, ("worktree_missing",)
        path = Path(raw_path).expanduser().resolve(strict=False)
        root = self.repository.root.resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return path, None, ("worktree_not_registered",)
        if path == root or not path.is_dir():
            return path, None, ("worktree_missing",)
        try:
            matches = [info for info in self.repository.worktrees() if _same_path(info.path, path)]
        except GitRepositoryError:
            raise
        if not matches:
            return path, None, ("worktree_not_registered",)
        info = matches[0]
        reasons: list[str] = []
        if info.branch != task.branch_name or info.detached or info.bare:
            reasons.append("branch_mismatch")
        return path, info, tuple(reasons)

    def _scope_violations(
        self, changed_files: tuple[str, ...], task: TaskRecord
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        _, allowed = canonicalize_lease_paths(
            task.definition.write_paths, windows_casefold=os.name == "nt"
        ) if task.definition.write_paths else ((), ())
        _, denied = canonicalize_lease_paths(
            task.definition.deny_paths, windows_casefold=os.name == "nt"
        ) if task.definition.deny_paths else ((), ())
        allowlist_violations: list[str] = []
        denylist_violations: list[str] = []
        for path in changed_files:
            _, canonical = canonicalize_lease_paths(
                [path], windows_casefold=os.name == "nt"
            )
            key = canonical[0]
            if not any(paths_overlap(key, pattern) for pattern in denied):
                if not any(paths_overlap(key, pattern) for pattern in allowed):
                    allowlist_violations.append(path)
            else:
                denylist_violations.append(path)
        return tuple(allowlist_violations), tuple(denylist_violations)

    def _lease_valid(
        self,
        task: TaskRecord,
        execution: ExecutionResult | None,
        reasons: list[str],
    ) -> bool | None:
        if not task.definition.write_paths:
            return True
        if execution is None:
            reasons.append("lease_missing")
            return False
        leases = self.lease_manager.list_task(task.task_id)
        now = self._clock()
        matching = [lease for lease in leases if lease.worker_id == execution.worker_id]
        _, required_paths = canonicalize_lease_paths(
            task.definition.write_paths, windows_casefold=os.name == "nt"
        )
        if any(
            lease.status.value == "active"
            and lease.expires_at > now
            and all(
                any(_pattern_covers(lease_path, required_path) for lease_path in lease.canonical_paths)
                for required_path in required_paths
            )
            for lease in matching
        ):
            return True
        if any(lease.expires_at <= now for lease in matching):
            reasons.append("lease_expired")
        else:
            reasons.append("lease_missing")
        return False

    def _run_required_checks(
        self,
        checks: tuple[RequiredCheck, ...],
        worktree_path: Path,
        artifact: ValidationArtifact,
    ) -> tuple[ValidationCheckResult, ...]:
        results: list[ValidationCheckResult] = []
        for index, check in enumerate(checks):
            results.append(self._run_check(check, index, worktree_path, artifact))
        return tuple(results)

    def _run_check(
        self,
        check: RequiredCheck,
        index: int,
        worktree_path: Path,
        artifact: ValidationArtifact,
    ) -> ValidationCheckResult:
        safe_name = f"{index:02d}-{_safe_check_name(check.name)}"
        directory = artifact.checks_directory / safe_name
        directory.mkdir(parents=False, exist_ok=False)
        stdout_path = directory / "stdout.log"
        stderr_path = directory / "stderr.log"
        stdout_path.touch(exist_ok=False)
        stderr_path.touch(exist_ok=False)
        started_at = self._clock()
        started_monotonic = time.monotonic()
        command = redact_command_for_storage(check.command)
        try:
            process = _start_check_process(check.command, worktree_path)
        except OSError:
            finished_at = self._clock()
            return ValidationCheckResult(
                name=check.name,
                command=command,
                started_at=started_at,
                finished_at=finished_at,
                duration_seconds=max(0.0, time.monotonic() - started_monotonic),
                exit_code=None,
                status=CheckStatus.START_FAILED,
                stdout_path=str(stdout_path),
                stderr_path=str(stderr_path),
            )

        if process.stdout is None or process.stderr is None:
            raise CheckRunnerError("required check pipes were not created")
        threads = (
            threading.Thread(target=_pump, args=(process.stdout, stdout_path), daemon=True),
            threading.Thread(target=_pump, args=(process.stderr, stderr_path), daemon=True),
        )
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + check.timeout_seconds
        timed_out = False
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_group(process)
                try:
                    process.wait(timeout=self.check_grace_seconds)
                except subprocess.TimeoutExpired:
                    _kill_group(process)
                    process.wait(timeout=5)
                break
            time.sleep(min(0.05, remaining))
        returncode = process.returncode if process.returncode is not None else process.poll()
        for thread in threads:
            thread.join(timeout=5)
        finished_at = self._clock()
        if timed_out:
            status = CheckStatus.TIMED_OUT
        else:
            status = CheckStatus.PASSED if returncode == 0 else CheckStatus.FAILED
        return ValidationCheckResult(
            name=check.name,
            command=command,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=max(0.0, time.monotonic() - started_monotonic),
            exit_code=returncode,
            status=status,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )

    def _select_execution(
        self, task_id: str, execution_id: str | None
    ) -> ExecutionResult | None:
        executions = self.store.list_executions(task_id)
        if execution_id is not None:
            selected = [item for item in executions if item.execution_id == execution_id]
            return selected[0] if selected else None
        return executions[-1] if executions else None

    def _execution_artifact_is_valid(self, execution: ExecutionResult) -> bool:
        expected = artifact_paths(self.repository.root, execution.execution_id)
        if expected.directory != Path(execution.artifact_dir).resolve(strict=False):
            return False
        if not expected.metadata_path.is_file() or not expected.stdout_path.is_file():
            return False
        return expected.stderr_path.is_file()

    def _execution_metadata(self, execution: ExecutionResult) -> dict[str, Any] | None:
        try:
            expected = artifact_paths(self.repository.root, execution.execution_id)
            return read_execution_metadata(expected)
        except Exception:
            return None

    def _blank_report(
        self,
        *,
        validation_id: str,
        task: TaskRecord,
        execution: ExecutionResult | None,
        artifact: ValidationArtifact,
        started_at: datetime,
        status: ValidationStatus,
        worker_id: str | None,
        execution_status: ExecutionStatus | None,
    ) -> ValidationReport:
        return ValidationReport(
            validation_id=validation_id,
            task_id=task.task_id,
            execution_id=execution.execution_id if execution is not None else None,
            worker_id=worker_id,
            status=status,
            started_at=started_at,
            finished_at=started_at,
            expected_branch=task.branch_name,
            actual_branch=None,
            base_commit=None,
            reported_commit=None,
            verified_commit=None,
            actual_changed_files=(),
            allowlist_violations=(),
            denylist_violations=(),
            required_check_results=(),
            execution_status=execution_status,
            lease_valid=None,
            blocking_reasons=(),
            artifact_dir=str(artifact.directory),
        )

    def _write_report(
        self,
        artifact: ValidationArtifact,
        report: ValidationReport,
        **extra: str,
    ) -> None:
        payload = report.to_dict()
        payload.update(extra)
        write_validation_report(artifact, payload)

    def _transition_after_validation(self, task_id: str, report: ValidationReport) -> None:
        if report.status is ValidationStatus.FAILED:
            task = self.store.get(task_id)
            if task.state is TaskState.RUNNING:
                self.store.transition(
                    task_id,
                    TaskState.FAILED,
                    reason=";".join(report.blocking_reasons),
                )
        elif report.status is ValidationStatus.BLOCKED:
            task = self.store.get(task_id)
            if task.state is TaskState.RUNNING:
                self.store.transition(task_id, TaskState.BLOCKED)


def _append_reason(reasons: tuple[str, ...], reason: str) -> tuple[str, ...]:
    return tuple(_unique((*reasons, reason)))


def _unique(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _same_path(left: Path, right: Path) -> bool:
    left_text = str(left.resolve(strict=False))
    right_text = str(right.resolve(strict=False))
    return left_text.casefold() == right_text.casefold() if os.name == "nt" else left_text == right_text


def _safe_check_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return normalized or "check"


def _pattern_covers(lease_pattern: str, required_pattern: str) -> bool:
    lease_subtree = lease_pattern.endswith("/**")
    required_subtree = required_pattern.endswith("/**")
    lease_value = lease_pattern[:-3] if lease_subtree else lease_pattern
    required_value = required_pattern[:-3] if required_subtree else required_pattern
    lease_parts = tuple(lease_value.split("/"))
    required_parts = tuple(required_value.split("/"))
    if not lease_subtree:
        return not required_subtree and lease_parts == required_parts
    return len(lease_parts) <= len(required_parts) and required_parts[: len(lease_parts)] == lease_parts


def _start_check_process(command: tuple[str, ...], cwd: Path) -> subprocess.Popen[bytes]:
    options: dict[str, object] = {
        "args": list(command),
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "bufsize": 0,
    }
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    else:
        options["start_new_session"] = True
    return subprocess.Popen(**options)  # type: ignore[arg-type]


def _pump(pipe, target: Path) -> None:  # type: ignore[no-untyped-def]
    try:
        with target.open("ab", buffering=0) as output:
            while True:
                chunk = pipe.read(64 * 1024)
                if not chunk:
                    break
                output.write(chunk)
    finally:
        pipe.close()


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (AttributeError, OSError):
            process.terminate()
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            check=False,
        )
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


def _unmaterialized_validation_artifact(
    repo_root: str | Path, validation_id: str
) -> ValidationArtifact:
    """Build the claim record's path without resolving or touching the filesystem."""

    directory = (
        Path(repo_root).expanduser().resolve()
        / ".agent-worktree"
        / "validations"
        / validation_id
    )
    return ValidationArtifact(
        directory=directory,
        report_path=directory / "report.json",
        checks_directory=directory / "checks",
    )
