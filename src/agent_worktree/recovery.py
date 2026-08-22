"""Conservative task cleanup and local recovery orchestration.

This module coordinates the existing Git, lease, worker, validation, and state
abstractions.  It deliberately does not reset files, kill uncertain processes,
delete arbitrary directories, or repair ambiguous task state.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .evidence import (
    ArtifactError,
    artifact_paths,
    create_recovery_artifact,
    read_execution_metadata,
    write_execution_metadata,
    write_recovery_report,
)
from .git import (
    GitRepository,
    GitRepositoryError,
    WorktreeDirtyError,
    WorktreeInfo,
)
from .leases import LeaseManager, LeaseStatus
from .models import (
    ExecutionResult,
    ExecutionStatus,
    RecoveryAction,
    RecoveryFinding,
    RecoveryReport,
    TaskRecord,
    TaskState,
)
from .state import StateStoreError, TaskStore, utc_now


RECOVERY_MODES = frozenset({"dry-run", "apply"})
TERMINAL_CLEANUP_STATES = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.BLOCKED}
)
INCOMPLETE_EXECUTION_STATES = frozenset(
    {ExecutionStatus.CREATED, ExecutionStatus.RUNNING}
)


class CleanupError(StateStoreError):
    """Base error for a refused or incomplete explicit task cleanup."""

    code = "cleanup_failed"

    def __init__(self, task_id: str, message: str) -> None:
        self.task_id = task_id
        super().__init__(f"{self.code}: task_id={task_id}: {message}")


class InvalidCleanupStateError(CleanupError):
    code = "cleanup_state_not_allowed"


class DirtyWorktreeCleanupError(CleanupError):
    code = "dirty_worktree_cleanup_rejected"


class RunningExecutionCleanupError(CleanupError):
    code = "running_execution_cleanup_rejected"


class ActiveValidationCleanupError(CleanupError):
    code = "active_validation_cleanup_rejected"


class UnsafeWorktreeCleanupError(CleanupError):
    code = "unsafe_worktree_cleanup_rejected"


class BranchCleanupPendingError(CleanupError):
    code = "branch_cleanup_pending"


@dataclass(frozen=True)
class CleanupResult:
    task_id: str
    state_before: TaskState
    state_after: TaskState
    worktree: str | None
    leases: tuple[str, ...]
    branch: str | None
    result: str
    actions: tuple[str, ...]
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "state_before": self.state_before.value,
            "state_after": self.state_after.value,
            "worktree": self.worktree,
            "leases": list(self.leases),
            "branch": self.branch,
            "result": self.result,
            "actions": list(self.actions),
            "blockers": list(self.blockers),
        }


class CleanupOrchestrator:
    """Perform one explicit, fail-closed task cleanup."""

    def __init__(
        self,
        store: TaskStore,
        *,
        repository: GitRepository | None = None,
        lease_manager: LeaseManager | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.repository = repository or GitRepository(store.repo_root)
        self.lease_manager = lease_manager or LeaseManager(store, clock=clock)
        self._clock = clock

    def cleanup(self, task_id: str, *, remove_branch: bool = False) -> CleanupResult:
        task = self.store.get(task_id)
        if task.state is TaskState.CLEANED:
            return CleanupResult(
                task_id=task.task_id,
                state_before=task.state,
                state_after=task.state,
                worktree=task.worktree_path,
                leases=(),
                branch=task.branch_name,
                result="ALREADY_CLEANED",
                actions=(),
                blockers=(),
            )
        if task.state not in TERMINAL_CLEANUP_STATES:
            raise InvalidCleanupStateError(
                task.task_id,
                f"state {task.state.value} is not eligible; allowed=completed,failed,blocked",
            )

        incomplete = tuple(
            execution
            for execution in self.store.list_executions(task.task_id)
            if execution.status in INCOMPLETE_EXECUTION_STATES
        )
        if incomplete:
            raise RunningExecutionCleanupError(
                task.task_id,
                "incomplete execution(s): "
                + ", ".join(item.execution_id for item in incomplete),
            )

        active_validations = tuple(
            report
            for report in self.store.list_validations(task.task_id)
            if report.status.value == "running"
        )
        if active_validations:
            raise ActiveValidationCleanupError(
                task.task_id,
                "active validation(s): "
                + ", ".join(item.validation_id for item in active_validations),
            )

        worktree_info = self._validate_task_worktree(task)
        active_leases = tuple(
            lease
            for lease in self.lease_manager.list_task(task.task_id)
            if lease.status is LeaseStatus.ACTIVE
        )
        actions: list[str] = []

        # Release leases before destructive Git operations.  The release API
        # retains task/worker/generation ownership checks and lease history.
        for lease in active_leases:
            try:
                self.lease_manager.release(
                    lease.lease_id,
                    task.task_id,
                    lease.worker_id,
                    lease.generation,
                )
            except StateStoreError as exc:
                raise CleanupError(task.task_id, str(exc)) from exc
            actions.append(f"lease_released:{lease.lease_id}")

        worktree_path = task.worktree_path
        if worktree_info is not None:
            try:
                self.repository.remove_worktree(worktree_info.path)
            except WorktreeDirtyError as exc:
                raise DirtyWorktreeCleanupError(task.task_id, str(exc)) from exc
            except GitRepositoryError as exc:
                raise CleanupError(task.task_id, str(exc)) from exc
            self.store.update_task_runtime_metadata(
                task.task_id,
                worktree_path=None,
            )
            actions.append("worktree_removed")

        if remove_branch:
            branch = task.branch_name
            expected_branch = self.repository.branch_for_task(task.task_id)
            if branch != expected_branch:
                raise UnsafeWorktreeCleanupError(
                    task.task_id,
                    "task branch does not exactly match its managed branch namespace",
                )
            if branch is not None and self.repository.branch_exists(branch):
                try:
                    self.repository.delete_branch(branch)
                except GitRepositoryError as exc:
                    raise BranchCleanupPendingError(task.task_id, str(exc)) from exc
                actions.append(f"branch_removed:{branch}")

        try:
            cleaned = self.store.transition(task.task_id, TaskState.CLEANED)
        except StateStoreError as exc:
            raise CleanupError(task.task_id, str(exc)) from exc
        return CleanupResult(
            task_id=task.task_id,
            state_before=task.state,
            state_after=cleaned.state,
            worktree=worktree_path,
            leases=tuple(lease.lease_id for lease in active_leases),
            branch=task.branch_name,
            result="CLEANED",
            actions=tuple(actions),
            blockers=(),
        )

    def _validate_task_worktree(self, task: TaskRecord) -> WorktreeInfo | None:
        if not task.worktree_path:
            return None
        candidate = Path(task.worktree_path).expanduser().resolve(strict=False)
        namespace = self.repository.worktree_root.resolve(strict=False)
        if not _is_within(candidate, namespace) or candidate == namespace:
            raise UnsafeWorktreeCleanupError(
                task.task_id,
                f"worktree is outside managed namespace: {candidate}",
            )
        if candidate.name != task.task_id:
            raise UnsafeWorktreeCleanupError(
                task.task_id,
                f"worktree name does not match task id: {candidate.name}",
            )
        matches = [
            info for info in self.repository.worktrees() if _same_path(info.path, candidate)
        ]
        if not matches:
            raise UnsafeWorktreeCleanupError(
                task.task_id,
                f"worktree is not registered by Git: {candidate}",
            )
        info = matches[0]
        if info.bare or info.detached or info.branch != task.branch_name:
            raise UnsafeWorktreeCleanupError(
                task.task_id,
                "registered worktree branch does not match task metadata",
            )
        try:
            if not self.repository.is_clean_at(candidate):
                raise DirtyWorktreeCleanupError(
                    task.task_id,
                    f"worktree contains modified, staged, or untracked files: {candidate}",
                )
        except DirtyWorktreeCleanupError:
            raise
        except GitRepositoryError as exc:
            raise UnsafeWorktreeCleanupError(task.task_id, str(exc)) from exc
        return info


class RecoveryOrchestrator:
    """Discover anomalies and apply only repairs proven safe locally."""

    def __init__(
        self,
        store: TaskStore,
        *,
        repository: GitRepository | None = None,
        lease_manager: LeaseManager | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.repository = repository or GitRepository(store.repo_root)
        self.lease_manager = lease_manager or LeaseManager(store, clock=clock)
        self._clock = clock

    def recover(self, mode: str) -> RecoveryReport:
        if mode not in RECOVERY_MODES:
            raise StateStoreError(f"unsupported recovery mode: {mode}")
        recovery_id = uuid.uuid4().hex
        artifact = create_recovery_artifact(self.repository.root, recovery_id)
        started_at = self._clock()
        findings: list[RecoveryFinding] = []
        actions: list[RecoveryAction] = []
        skipped: list[RecoveryAction] = []
        errors: list[str] = []
        try:
            self._scan(
                mode,
                findings,
                actions,
                skipped,
                errors,
            )
        except Exception as exc:  # recovery itself must leave an audit report
            errors.append(f"recovery_internal_error: {exc}")
        report = RecoveryReport(
            recovery_id=recovery_id,
            mode=mode,
            started_at=started_at,
            finished_at=self._clock(),
            findings=tuple(findings),
            actions=tuple(actions),
            skipped=tuple(skipped),
            errors=tuple(errors),
            artifact_dir=str(artifact.directory),
        )
        write_recovery_report(artifact, report.to_dict())
        return report

    def _scan(
        self,
        mode: str,
        findings: list[RecoveryFinding],
        actions: list[RecoveryAction],
        skipped: list[RecoveryAction],
        errors: list[str],
    ) -> None:
        apply = mode == "apply"
        tasks = self.store.list_tasks()
        task_by_id = {task.task_id: task for task in tasks}
        executions = self.store.list_executions()
        worktrees = self.repository.worktrees()
        registered_by_path = {
            _canonical_path(info.path): info for info in worktrees if info.path != self.repository.root
        }

        self._inspect_executions(
            apply, task_by_id, executions, findings, actions, skipped, errors
        )
        self._inspect_tasks(
            apply, tasks, executions, registered_by_path, findings, actions, skipped
        )
        self._inspect_leases(
            apply, task_by_id, findings, actions, skipped, errors
        )
        self._inspect_worktrees(
            apply,
            tasks,
            task_by_id,
            executions,
            worktrees,
            registered_by_path,
            findings,
            actions,
            skipped,
            errors,
        )
        self._inspect_unregistered_directories(
            registered_by_path, findings, skipped, errors
        )

    def _inspect_executions(
        self,
        apply: bool,
        task_by_id: dict[str, TaskRecord],
        executions: tuple[ExecutionResult, ...],
        findings: list[RecoveryFinding],
        actions: list[RecoveryAction],
        skipped: list[RecoveryAction],
        errors: list[str],
    ) -> None:
        for execution in executions:
            if execution.is_terminal:
                has_worktree = bool(execution.worktree_path) and Path(
                    execution.worktree_path
                ).exists()
                has_active_lease = any(
                    lease.status is LeaseStatus.ACTIVE
                    for lease in self.lease_manager.list_task(execution.task_id)
                )
                if has_worktree or has_active_lease:
                    findings.append(
                        RecoveryFinding(
                            code="terminal_execution_with_resources",
                            severity="warning",
                            description=(
                                "terminal execution still has a worktree or active lease; "
                                "resource cleanup remains explicit"
                            ),
                            task_id=execution.task_id,
                            execution_id=execution.execution_id,
                            worktree=execution.worktree_path,
                        )
                    )
                    skipped.append(
                        RecoveryAction(
                            code="TERMINAL_EXECUTION_RESOURCES_PRESERVED",
                            status="skipped",
                            description="terminal execution resources are not auto-removed",
                            task_id=execution.task_id,
                            execution_id=execution.execution_id,
                            worktree=execution.worktree_path,
                        )
                    )
                continue
            if execution.status is not ExecutionStatus.RUNNING:
                continue
            alive = is_process_alive(execution.pid)
            if alive:
                finding = RecoveryFinding(
                    code="running_execution_process_alive",
                    severity="warning",
                    description=(
                        "persisted running execution has a live PID; identity is not "
                        "reliably verified, so recovery will not kill it"
                    ),
                    task_id=execution.task_id,
                    execution_id=execution.execution_id,
                    worktree=execution.worktree_path,
                )
                findings.append(finding)
                skipped.append(
                    RecoveryAction(
                        code="PROCESS_PRESERVED_ALIVE_OR_UNCERTAIN",
                        status="skipped",
                        description="live or uncertain worker process preserved",
                        task_id=execution.task_id,
                        execution_id=execution.execution_id,
                    )
                )
                continue

            finding = RecoveryFinding(
                code="running_execution_process_missing",
                severity="error",
                description="execution is marked running but its persisted PID is not alive",
                task_id=execution.task_id,
                execution_id=execution.execution_id,
                worktree=execution.worktree_path,
            )
            findings.append(finding)
            if not apply:
                skipped.append(
                    RecoveryAction(
                        code="EXECUTION_FAILURE_REPAIR",
                        status="skipped",
                        description="dry-run does not change execution state",
                        task_id=execution.task_id,
                        execution_id=execution.execution_id,
                    )
                )
                continue
            try:
                finished_at = self._clock()
                started_at = execution.started_at or execution.created_at
                duration = max(0.0, (finished_at - started_at).total_seconds())
                failed = self.store.finish_execution(
                    execution.execution_id,
                    status=ExecutionStatus.FAILED,
                    finished_at=finished_at,
                    exit_code=None,
                    duration_seconds=duration,
                )
                self._annotate_execution(
                    failed,
                    "orchestrator_restart_process_missing",
                )
                actions.append(
                    RecoveryAction(
                        code="EXECUTION_MARKED_FAILED",
                        status="applied",
                        description="dead persisted worker marked failed",
                        task_id=execution.task_id,
                        execution_id=execution.execution_id,
                    )
                )
                task = task_by_id.get(execution.task_id)
                if task is not None and task.state is TaskState.RUNNING:
                    self.store.transition(
                        task.task_id,
                        TaskState.BLOCKED,
                        reason="orchestrator_restart_process_missing",
                    )
                    actions.append(
                        RecoveryAction(
                            code="TASK_BLOCKED_PROCESS_MISSING",
                            status="applied",
                            description="running task blocked after worker process loss",
                            task_id=task.task_id,
                            execution_id=execution.execution_id,
                        )
                    )
            except StateStoreError as exc:
                errors.append(
                    f"execution_recovery_failed:{execution.execution_id}: {exc}"
                )

    def _inspect_tasks(
        self,
        apply: bool,
        tasks: tuple[TaskRecord, ...],
        executions: tuple[ExecutionResult, ...],
        registered_by_path: dict[str, WorktreeInfo],
        findings: list[RecoveryFinding],
        actions: list[RecoveryAction],
        skipped: list[RecoveryAction],
    ) -> None:
        execution_by_task: dict[str, list[ExecutionResult]] = {}
        for execution in executions:
            execution_by_task.setdefault(execution.task_id, []).append(execution)
        for task in tasks:
            task_executions = execution_by_task.get(task.task_id, [])
            if task.state is TaskState.RUNNING and not any(
                item.status in INCOMPLETE_EXECUTION_STATES for item in task_executions
            ):
                findings.append(
                    RecoveryFinding(
                        code="running_task_without_execution",
                        severity="error",
                        description="running task has no created or running execution",
                        task_id=task.task_id,
                        worktree=task.worktree_path,
                    )
                )

            if task.state is TaskState.COMPLETED:
                try:
                    passed = any(
                        report.status.value == "passed"
                        for report in self.store.list_validations(task.task_id)
                    )
                except StateStoreError as exc:
                    passed = False
                    skipped.append(
                        RecoveryAction(
                            code="VALIDATION_AUDIT_UNAVAILABLE",
                            status="skipped",
                            description=str(exc),
                            task_id=task.task_id,
                        )
                    )
                if not passed:
                    findings.append(
                        RecoveryFinding(
                            code="completed_without_validation",
                            severity="error",
                            description="completed task has no persisted passed validation",
                            task_id=task.task_id,
                            worktree=task.worktree_path,
                        )
                    )

            if task.worktree_path:
                candidate = _canonical_path(Path(task.worktree_path))
                info = registered_by_path.get(candidate)
                if info is None:
                    finding = RecoveryFinding(
                        code="task_worktree_missing",
                        severity="error" if task.state is TaskState.RUNNING else "warning",
                        description="TaskRecord points to a worktree absent from Git registry",
                        task_id=task.task_id,
                        worktree=task.worktree_path,
                    )
                    findings.append(finding)
                    if apply and task.state is TaskState.RUNNING:
                        self._block_task(
                            task,
                            "worktree_missing",
                            actions,
                        )
                elif info.branch != task.branch_name or info.bare or info.detached:
                    findings.append(
                        RecoveryFinding(
                            code="task_branch_mismatch",
                            severity="error",
                            description="registered worktree branch disagrees with TaskRecord",
                            task_id=task.task_id,
                            worktree=str(info.path),
                        )
                    )
                    if apply and task.state is TaskState.RUNNING:
                        self._block_task(task, "branch_mismatch", actions)

            if task.state is TaskState.CLEANED and (
                task.worktree_path is not None
                or any(item.status in INCOMPLETE_EXECUTION_STATES for item in task_executions)
            ):
                findings.append(
                    RecoveryFinding(
                        code="cleaned_task_with_resources",
                        severity="warning",
                        description="cleaned task still references runtime resources",
                        task_id=task.task_id,
                        worktree=task.worktree_path,
                    )
                )

    def _inspect_leases(
        self,
        apply: bool,
        task_by_id: dict[str, TaskRecord],
        findings: list[RecoveryFinding],
        actions: list[RecoveryAction],
        skipped: list[RecoveryAction],
        errors: list[str],
    ) -> None:
        active = self.lease_manager.list_active()
        for lease in active:
            task = task_by_id.get(lease.task_id)
            if task is None:
                findings.append(
                    RecoveryFinding(
                        code="active_lease_without_task",
                        severity="error",
                        description="unexpired active lease has no persisted task",
                        lease_id=lease.lease_id,
                    )
                )
                skipped.append(
                    RecoveryAction(
                        code="ACTIVE_LEASE_WITHOUT_TASK_PRESERVED",
                        status="skipped",
                        description="active lease without task is ambiguous and was preserved",
                        lease_id=lease.lease_id,
                    )
                )
                continue
            if task.state in {TaskState.COMPLETED, TaskState.FAILED, TaskState.BLOCKED, TaskState.CLEANED}:
                findings.append(
                    RecoveryFinding(
                        code="active_lease_on_terminal_task",
                        severity="warning",
                        description="terminal task still owns an active lease",
                        task_id=task.task_id,
                        lease_id=lease.lease_id,
                    )
                )
                if apply:
                    try:
                        self.lease_manager.release(
                            lease.lease_id,
                            task.task_id,
                            lease.worker_id,
                            lease.generation,
                        )
                        actions.append(
                            RecoveryAction(
                                code="TERMINAL_TASK_LEASE_RELEASED",
                                status="applied",
                                description="active lease released for terminal task",
                                task_id=task.task_id,
                                lease_id=lease.lease_id,
                            )
                        )
                    except StateStoreError as exc:
                        errors.append(f"lease_release_failed:{lease.lease_id}: {exc}")
                else:
                    skipped.append(
                        RecoveryAction(
                            code="TERMINAL_TASK_LEASE_RELEASE",
                            status="skipped",
                            description="dry-run does not change lease state",
                            task_id=task.task_id,
                            lease_id=lease.lease_id,
                        )
                    )

        stale = self.lease_manager.find_stale()
        for lease in stale:
            findings.append(
                RecoveryFinding(
                    code="stale_lease",
                    severity="warning",
                    description="active lease is expired and eligible for stale recovery",
                    task_id=lease.task_id,
                    lease_id=lease.lease_id,
                )
            )
        if not stale:
            return
        if not apply:
            for lease in stale:
                skipped.append(
                    RecoveryAction(
                        code="STALE_LEASE_MARKED_STALE",
                        status="skipped",
                        description="dry-run preserves expired active lease",
                        task_id=lease.task_id,
                        lease_id=lease.lease_id,
                    )
                )
            return
        try:
            recovered = self.lease_manager.recover_stale()
            for lease in recovered:
                actions.append(
                    RecoveryAction(
                        code="STALE_LEASE_MARKED_STALE",
                        status="applied",
                        description="expired active lease marked stale",
                        task_id=lease.task_id,
                        lease_id=lease.lease_id,
                    )
                )
                task = task_by_id.get(lease.task_id)
                if task is not None and task.state is TaskState.RUNNING:
                    self.store.transition(task.task_id, TaskState.BLOCKED, reason="stale_lease")
                    actions.append(
                        RecoveryAction(
                            code="TASK_BLOCKED_STALE_LEASE",
                            status="applied",
                            description="running task blocked after losing write ownership",
                            task_id=task.task_id,
                            lease_id=lease.lease_id,
                        )
                    )
        except StateStoreError as exc:
            errors.append(f"stale_lease_recovery_failed: {exc}")

    def _inspect_worktrees(
        self,
        apply: bool,
        tasks: tuple[TaskRecord, ...],
        task_by_id: dict[str, TaskRecord],
        executions: tuple[ExecutionResult, ...],
        worktrees: tuple[WorktreeInfo, ...],
        registered_by_path: dict[str, WorktreeInfo],
        findings: list[RecoveryFinding],
        actions: list[RecoveryAction],
        skipped: list[RecoveryAction],
        errors: list[str],
    ) -> None:
        del tasks
        namespace = self.repository.worktree_root.resolve(strict=False)
        managed_task_paths = {
            _canonical_path(Path(task.worktree_path))
            for task in task_by_id.values()
            if task.worktree_path
        }
        active_execution_paths = {
            _canonical_path(Path(execution.worktree_path))
            for execution in executions
            if execution.status in INCOMPLETE_EXECUTION_STATES
        }
        unknown_active_leases = {
            lease.task_id
            for lease in self.lease_manager.list_active()
            if lease.task_id not in task_by_id
        }
        for info in worktrees:
            if _same_path(info.path, self.repository.root):
                continue
            path_key = _canonical_path(info.path)
            if not _is_within(info.path, namespace):
                findings.append(
                    RecoveryFinding(
                        code="foreign_worktree",
                        severity="warning",
                        description="registered worktree is outside agent-worktree namespace",
                        worktree=str(info.path),
                    )
                )
                skipped.append(
                    RecoveryAction(
                        code="FOREIGN_WORKTREE_PRESERVED",
                        status="skipped",
                        description="foreign worktree is never modified by recovery",
                        worktree=str(info.path),
                    )
                )
                continue
            if path_key in managed_task_paths:
                continue

            dirty = self._is_dirty(info.path, errors)
            code = "dirty_orphan_worktree" if dirty else "orphan_managed_worktree"
            findings.append(
                RecoveryFinding(
                    code=code,
                    severity="error" if dirty else "warning",
                    description=(
                        "managed namespace worktree has no matching TaskRecord"
                    ),
                    worktree=str(info.path),
                )
            )
            if dirty:
                skipped.append(
                    RecoveryAction(
                        code="DIRTY_ORPHAN_PRESERVED",
                        status="skipped",
                        description="dirty orphan preserves user files",
                        worktree=str(info.path),
                    )
                )
                continue
            if not apply:
                skipped.append(
                    RecoveryAction(
                        code="ORPHAN_WORKTREE_REMOVAL",
                        status="skipped",
                        description="dry-run does not remove orphan worktrees",
                        worktree=str(info.path),
                    )
                )
                continue
            if path_key in active_execution_paths:
                skipped.append(
                    RecoveryAction(
                        code="ORPHAN_WITH_ACTIVE_EXECUTION_PRESERVED",
                        status="skipped",
                        description="orphan worktree has an incomplete execution",
                        worktree=str(info.path),
                    )
                )
                continue
            if unknown_active_leases:
                skipped.append(
                    RecoveryAction(
                        code="ORPHAN_WITH_UNKNOWN_LEASE_PRESERVED",
                        status="skipped",
                        description="active lease without task makes orphan ownership ambiguous",
                        worktree=str(info.path),
                    )
                )
                continue
            if not self._safe_orphan_branch(info):
                skipped.append(
                    RecoveryAction(
                        code="ORPHAN_BRANCH_AMBIGUOUS_PRESERVED",
                        status="skipped",
                        description="orphan branch is not an exact managed task branch",
                        worktree=str(info.path),
                    )
                )
                continue
            try:
                self.repository.remove_worktree(info.path)
                actions.append(
                    RecoveryAction(
                        code="ORPHAN_WORKTREE_REMOVED",
                        status="applied",
                        description="clean exact managed orphan worktree removed",
                        worktree=str(info.path),
                    )
                )
            except GitRepositoryError as exc:
                errors.append(f"orphan_worktree_remove_failed:{info.path}: {exc}")

    def _inspect_unregistered_directories(
        self,
        registered_by_path: dict[str, WorktreeInfo],
        findings: list[RecoveryFinding],
        skipped: list[RecoveryAction],
        errors: list[str],
    ) -> None:
        namespace = self.repository.worktree_root.resolve(strict=False)
        if not namespace.is_dir():
            return
        try:
            children = tuple(namespace.iterdir())
        except OSError as exc:
            errors.append(f"managed_namespace_scan_failed: {exc}")
            return
        for child in children:
            resolved = child.resolve(strict=False)
            if _canonical_path(resolved) in registered_by_path:
                continue
            if not child.is_dir() and not child.is_symlink():
                continue
            findings.append(
                RecoveryFinding(
                    code="unregistered_managed_directory",
                    severity="warning",
                    description="directory exists under managed namespace but Git does not register it",
                    worktree=str(child),
                )
            )
            skipped.append(
                RecoveryAction(
                    code="UNREGISTERED_MANAGED_DIRECTORY_PRESERVED",
                    status="skipped",
                    description="unregistered directory is never recursively deleted",
                    worktree=str(child),
                )
            )

    def _block_task(
        self,
        task: TaskRecord,
        reason: str,
        actions: list[RecoveryAction],
    ) -> None:
        try:
            current = self.store.get(task.task_id)
            if current.state is not TaskState.RUNNING:
                return
            self.store.transition(task.task_id, TaskState.BLOCKED, reason=reason)
            actions.append(
                RecoveryAction(
                    code="TASK_BLOCKED_INCONSISTENCY",
                    status="applied",
                    description=f"running task blocked: {reason}",
                    task_id=task.task_id,
                )
            )
        except StateStoreError as exc:
            actions.append(
                RecoveryAction(
                    code="TASK_BLOCK_FAILED",
                    status="skipped",
                    description=str(exc),
                    task_id=task.task_id,
                )
            )

    def _annotate_execution(self, execution: ExecutionResult, reason: str) -> None:
        artifact = artifact_paths(self.repository.root, execution.execution_id)
        try:
            payload = read_execution_metadata(artifact)
        except (ArtifactError, OSError, ValueError, TypeError):
            payload = execution.to_dict()
        payload["recovery_reason"] = reason
        write_execution_metadata(artifact, payload)

    @staticmethod
    def _is_dirty(path: Path, errors: list[str]) -> bool:
        try:
            return not GitRepository(path).is_clean()
        except GitRepositoryError as exc:
            errors.append(f"worktree_dirty_check_failed:{path}: {exc}")
            return True

    def _safe_orphan_branch(self, info: WorktreeInfo) -> bool:
        if not info.branch:
            return False
        try:
            return self.repository.branch_for_task(info.path.name) == info.branch
        except GitRepositoryError:
            return False


def is_process_alive(pid: int | None) -> bool:
    """Return liveness only; never treats a PID as proof of process identity."""

    if pid is None or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        # ``os.kill(pid, 0)`` is not a reliable existence check on Windows:
        # it may succeed for a PID whose process has already exited.  Query
        # the exit code instead.  Access uncertainty is fail-safe: preserve.
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            process = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not process:
                return True
            exit_code = ctypes.c_ulong()
            try:
                if not kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(process)
        except (OSError, AttributeError, ImportError):
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _canonical_path(path: Path) -> str:
    value = str(path.expanduser().resolve(strict=False))
    return value.casefold() if os.name == "nt" else value


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _same_path(left: Path, right: Path) -> bool:
    return _canonical_path(left) == _canonical_path(right)
