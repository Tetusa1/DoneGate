"""Public task-run orchestration built from the existing runtime components."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .evidence import artifact_paths, read_execution_metadata, write_execution_metadata
from .git import GitRepository, GitRepositoryError
from .leases import LeaseManager
from .models import ExecutionResult, ExecutionStatus, LeaseStatus, TaskRecord, TaskState
from .state import StateStoreError, TaskStore, utc_now
from .validate import CompletionValidator, ValidationError
from .worker import WorkerError, WorkerProcess, WorkerStartError


COMMIT_CLAIM_PREFIX = "AGENT_WORKTREE_COMMIT:"
_COMMIT_CLAIM_PATTERN = re.compile(
    r"^\s*AGENT_WORKTREE_COMMIT:\s*([0-9A-Fa-f]{4,64})\s*$",
    re.MULTILINE,
)
RUNNABLE_STATES = frozenset({TaskState.PENDING, TaskState.ASSIGNED})


class TaskRunError(RuntimeError):
    """Base error for a public task-run operation."""

    code = "task_run_failed"
    exit_code = 3

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(message)


class TaskRunPreconditionError(TaskRunError):
    code = "task_run_precondition_failed"
    exit_code = 2


class TaskRunInfrastructureError(TaskRunError):
    code = "task_run_infrastructure_failed"
    exit_code = 3


@dataclass(frozen=True)
class TaskRunResult:
    task_id: str
    status: str
    state: TaskState
    execution: ExecutionResult | None
    validation: object | None
    worktree: str | None
    actions: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "task_id": self.task_id,
            "state": self.state.value,
            "execution": self.execution.to_dict() if self.execution else None,
            "validation": self.validation.to_dict() if self.validation else None,
            "worktree": self.worktree,
            "actions": list(self.actions),
        }


class TaskRunner:
    """Run one task from persisted definition through independent validation."""

    def __init__(
        self,
        store: TaskStore,
        *,
        repository: GitRepository | None = None,
        lease_manager: LeaseManager | None = None,
        worker: WorkerProcess | None = None,
        validator: CompletionValidator | None = None,
        clock: Callable = utc_now,
    ) -> None:
        self.store = store
        self.repository = repository or GitRepository(store.repo_root)
        self.lease_manager = lease_manager or LeaseManager(store, clock=clock)
        self.worker = worker or WorkerProcess(
            store,
            repository=self.repository,
            lease_manager=self.lease_manager,
            clock=clock,
        )
        self.validator = validator or CompletionValidator(
            store,
            repository=self.repository,
            lease_manager=self.lease_manager,
            clock=clock,
        )
        self._clock = clock

    def run(self, task_id: str) -> TaskRunResult:
        task = self.store.get(task_id)
        if task.state not in RUNNABLE_STATES:
            raise TaskRunPreconditionError(
                f"task state {task.state.value} cannot be run; expected pending or assigned",
                code="task_state_not_runnable",
            )

        actions: list[str] = []
        created_worktree: Path | None = None
        was_pending = task.state is TaskState.PENDING
        try:
            task, created_worktree = self._prepare(task, actions)
        except (GitRepositoryError, StateStoreError) as exc:
            if was_pending and created_worktree is None:
                try:
                    current = self.store.get(task_id)
                    if current.worktree_path:
                        created_worktree = Path(current.worktree_path)
                except StateStoreError:
                    pass
            self._block_setup_failure(task.task_id, str(exc), created_worktree)
            raise TaskRunInfrastructureError(
                f"task setup failed: {exc}", code="task_setup_failed"
            ) from exc

        try:
            handle = self.worker.start(task.task_id)
            actions.append("worker_started")
        except WorkerStartError as exc:
            execution = self.store.get_execution(exc.execution_id)
            actions.append("worker_start_failed")
            return self._result(task.task_id, execution, None, actions)
        except WorkerError as exc:
            self._block_setup_failure(task.task_id, str(exc), created_worktree)
            raise TaskRunInfrastructureError(
                f"worker could not start: {exc}", code="worker_start_precondition_failed"
            ) from exc

        try:
            execution = self.worker.wait(handle)
        except WorkerError as exc:
            raise TaskRunInfrastructureError(
                f"worker lifecycle failed: {exc}", code="worker_lifecycle_failed"
            ) from exc
        actions.append(f"worker_finished:{execution.status.value}")

        if execution.status is not ExecutionStatus.SUCCEEDED:
            return self._result(task.task_id, execution, None, actions)

        self._record_commit_claim(execution, task)
        try:
            validation = self.validator.validate(
                task.task_id, execution_id=execution.execution_id
            )
        except (GitRepositoryError, StateStoreError, ValidationError) as exc:
            self._block_validation_failure(task.task_id, str(exc))
            raise TaskRunInfrastructureError(
                f"validation orchestration failed: {exc}",
                code="validation_orchestration_failed",
            ) from exc
        actions.append(f"validation_finished:{validation.status.value}")
        return self._result(task.task_id, execution, validation, actions)

    def _prepare(
        self, task: TaskRecord, actions: list[str]
    ) -> tuple[TaskRecord, Path | None]:
        created_worktree: Path | None = None
        if task.state is TaskState.PENDING:
            created = self.repository.create_worktree(task.task_id, task.base_ref)
            created_worktree = created.worktree_path
            self.store.update_task_runtime_metadata(
                task.task_id,
                worktree_path=str(created.worktree_path),
                branch_name=created.branch,
                base_commit=created.base_commit,
                head_commit=created.head_commit,
            )
            self.store.transition(task.task_id, TaskState.ASSIGNED)
            actions.append("worktree_created")
            task = self.store.get(task.task_id)
        elif not task.worktree_path:
            raise StateStoreError("assigned task has no worktree metadata")

        task = self.store.get(task.task_id)
        if task.definition.write_paths and not self._has_valid_lease(task):
            ttl = min(max(float(task.definition.timeout_seconds) + 300.0, 1.0), 86_400.0)
            self.lease_manager.acquire(
                task.task_id,
                task.worker_id,
                task.definition.write_paths,
                ttl_seconds=ttl,
            )
            actions.append("lease_acquired")
        return task, created_worktree

    def _has_valid_lease(self, task: TaskRecord) -> bool:
        now = self._clock()
        required = set(task.definition.write_paths)
        return any(
            lease.status is LeaseStatus.ACTIVE
            and lease.worker_id == task.worker_id
            and lease.expires_at > now
            and set(lease.paths) == required
            for lease in self.lease_manager.list_task(task.task_id)
        )

    def _record_commit_claim(self, execution: ExecutionResult, task: TaskRecord) -> None:
        if not task.definition.write_paths:
            return
        artifact = artifact_paths(self.repository.root, execution.execution_id)
        try:
            stdout = Path(execution.stdout_path).read_text(encoding="utf-8", errors="replace")
            match = _COMMIT_CLAIM_PATTERN.search(stdout)
            if match is None:
                return
            metadata = read_execution_metadata(artifact)
            metadata["reported_commit"] = match.group(1)
            write_execution_metadata(artifact, metadata)
        except (OSError, TypeError, ValueError, RuntimeError):
            return

    def _block_setup_failure(
        self, task_id: str, reason: str, created_worktree: Path | None
    ) -> None:
        try:
            task = self.store.get(task_id)
            if task.state in {TaskState.PENDING, TaskState.ASSIGNED}:
                self.store.transition(task_id, TaskState.BLOCKED, reason=reason)
        except StateStoreError:
            return
        if created_worktree is None:
            return
        try:
            if self.repository.is_clean_at(created_worktree):
                self.repository.remove_worktree(created_worktree)
                self.store.update_task_runtime_metadata(task_id, worktree_path=None)
        except GitRepositoryError:
            # Ambiguous or dirty resources remain available for explicit cleanup.
            return

    def _block_validation_failure(self, task_id: str, reason: str) -> None:
        try:
            task = self.store.get(task_id)
            if task.state is TaskState.RUNNING:
                self.store.transition(task_id, TaskState.BLOCKED, reason=reason)
        except StateStoreError:
            return

    def _result(
        self,
        task_id: str,
        execution: ExecutionResult | None,
        validation: object | None,
        actions: list[str],
    ) -> TaskRunResult:
        task = self.store.get(task_id)
        status = {
            TaskState.COMPLETED: "completed",
            TaskState.FAILED: "failed",
            TaskState.BLOCKED: "blocked",
        }.get(task.state, "failed")
        return TaskRunResult(
            task_id=task_id,
            status=status,
            state=task.state,
            execution=execution,
            validation=validation,
            worktree=task.worktree_path,
            actions=tuple(actions),
        )
