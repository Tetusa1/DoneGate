"""SQLite-backed persistent task state for DoneGate."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .models import (
    CheckStatus,
    ExecutionResult,
    ExecutionStatus,
    TaskDefinition,
    TaskRecord,
    TaskState,
    TERMINAL_EXECUTION_STATES,
    ValidationCheckResult,
    ValidationReport,
    ValidationStatus,
)


DB_SCHEMA_VERSION = 4

_LEASES_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS leases ("
    "lease_id TEXT NOT NULL, "
    "task_id TEXT NOT NULL, "
    "worker_id TEXT NOT NULL, "
    "owner_pid INTEGER, "
    "path_pattern TEXT NOT NULL, "
    "canonical_pattern TEXT NOT NULL, "
    "acquired_at TEXT NOT NULL, "
    "renewed_at TEXT NOT NULL, "
    "expires_at TEXT NOT NULL, "
    "status TEXT NOT NULL, "
    "generation INTEGER NOT NULL, "
    "PRIMARY KEY (lease_id, canonical_pattern)"
    ")"
)

_EXECUTIONS_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS executions ("
    "execution_id TEXT PRIMARY KEY, "
    "task_id TEXT NOT NULL, "
    "worker_id TEXT NOT NULL, "
    "command_json TEXT NOT NULL, "
    "worktree_path TEXT NOT NULL, "
    "pid INTEGER, "
    "created_at TEXT NOT NULL, "
    "started_at TEXT, "
    "finished_at TEXT, "
    "duration_seconds REAL, "
    "exit_code INTEGER, "
    "status TEXT NOT NULL, "
    "timeout_seconds REAL NOT NULL, "
    "artifact_dir TEXT NOT NULL, "
    "stdout_path TEXT NOT NULL, "
    "stderr_path TEXT NOT NULL"
    ")"
)

_VALIDATIONS_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS validations ("
    "validation_id TEXT PRIMARY KEY, "
    "task_id TEXT NOT NULL, "
    "execution_id TEXT, "
    "worker_id TEXT, "
    "status TEXT NOT NULL, "
    "started_at TEXT NOT NULL, "
    "finished_at TEXT, "
    "report_json TEXT NOT NULL, "
    "artifact_dir TEXT NOT NULL"
    ")"
)

_LEGAL_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.ASSIGNED, TaskState.BLOCKED}),
    TaskState.ASSIGNED: frozenset({TaskState.RUNNING, TaskState.BLOCKED, TaskState.FAILED}),
    TaskState.RUNNING: frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.BLOCKED}),
    TaskState.BLOCKED: frozenset({TaskState.ASSIGNED, TaskState.FAILED, TaskState.CLEANED}),
    TaskState.FAILED: frozenset({TaskState.ASSIGNED, TaskState.CLEANED}),
    TaskState.COMPLETED: frozenset({TaskState.CLEANED}),
    TaskState.CLEANED: frozenset(),
}
_UNSET = object()


class StateStoreError(RuntimeError):
    """Base error for persistent task state operations."""


class TaskNotFoundError(StateStoreError):
    """Raised when a requested task does not exist."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"TaskNotFound: {task_id}")


class DuplicateTaskError(StateStoreError):
    """Raised when task creation would overwrite an existing task."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"TaskAlreadyExists: {task_id}")


class InvalidTaskTransition(StateStoreError):
    """Raised when a state transition is not allowed by the central policy."""

    def __init__(
        self,
        task_id: str,
        current_state: TaskState | str | None,
        requested_state: TaskState | str,
    ) -> None:
        self.task_id = task_id
        self.current_state = current_state
        self.requested_state = requested_state
        current = current_state.value if isinstance(current_state, TaskState) else current_state
        requested = requested_state.value if isinstance(requested_state, TaskState) else requested_state
        super().__init__(
            f"InvalidTaskTransition: task_id={task_id}, current_state={current}, requested_state={requested}"
        )


class StateCorruptionError(StateStoreError):
    """Raised when stored state, definition, timestamp, or metadata is invalid."""


class UnsupportedSchemaVersionError(StateStoreError):
    """Raised when the SQLite schema is newer or otherwise unsupported."""


class RepositoryBindingError(StateStoreError):
    """Raised when a state database belongs to another repository root."""


class ConcurrentStateUpdateError(StateStoreError):
    """Raised when optimistic state versioning detects a lost update."""


class ExecutionNotFoundError(StateStoreError):
    """Raised when a requested execution does not exist."""

    def __init__(self, execution_id: str) -> None:
        self.execution_id = execution_id
        super().__init__(f"ExecutionNotFound: {execution_id}")


class DuplicateExecutionError(StateStoreError):
    """Raised when an execution id would overwrite an existing artifact."""

    def __init__(self, execution_id: str) -> None:
        self.execution_id = execution_id
        super().__init__(f"ExecutionAlreadyExists: {execution_id}")


class InvalidExecutionTransition(StateStoreError):
    """Raised when an execution status transition is not allowed."""

    def __init__(self, execution_id: str, current: ExecutionStatus, requested: ExecutionStatus) -> None:
        self.execution_id = execution_id
        self.current_status = current
        self.requested_status = requested
        super().__init__(
            f"InvalidExecutionTransition: execution_id={execution_id}, "
            f"current_status={current.value}, requested_status={requested.value}"
        )


class ValidationNotFoundError(StateStoreError):
    """Raised when a requested validation does not exist."""

    def __init__(self, validation_id: str) -> None:
        self.validation_id = validation_id
        super().__init__(f"ValidationNotFound: {validation_id}")


class ValidationAlreadyRunningError(StateStoreError):
    """Raised when the same task/execution already has an active validation."""

    def __init__(self, task_id: str, execution_id: str | None) -> None:
        self.task_id = task_id
        self.execution_id = execution_id
        suffix = execution_id if execution_id is not None else "<none>"
        super().__init__(
            f"ValidationAlreadyRunning: task_id={task_id}, execution_id={suffix}"
        )


class InvalidValidationTransition(StateStoreError):
    """Raised when a validation report is finalized more than once."""

    def __init__(self, validation_id: str, current: ValidationStatus) -> None:
        self.validation_id = validation_id
        self.current_status = current
        super().__init__(
            f"InvalidValidationTransition: validation_id={validation_id}, "
            f"current_status={current.value}"
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StateStoreError("timestamps must be timezone-aware UTC values")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str | None, field: str, task_id: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateCorruptionError(f"invalid {field} for task {task_id}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StateCorruptionError(f"non-timezone-aware {field} for task {task_id}")
    return parsed.astimezone(timezone.utc)


class TaskStore:
    """A small SQLite task store with transactional state transitions."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        db_path: str | Path | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        root = Path(repo_root).expanduser()
        if not root.exists() or not root.is_dir():
            raise StateStoreError(f"repository root does not exist: {repo_root}")
        self.repo_root = root.resolve()
        self.db_path = (
            Path(db_path).expanduser().resolve()
            if db_path is not None
            else self.repo_root / ".agent-worktree" / "state" / "state.sqlite3"
        )
        self._clock = clock
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        except sqlite3.DatabaseError as exc:
            raise StateStoreError(f"cannot initialize state database: {self.db_path}") from exc

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.db_path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=True,
            )
        except sqlite3.Error as exc:
            raise StateStoreError(f"cannot open state database: {self.db_path}") from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS metadata ("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL"
                    ")"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS tasks ("
                    "task_id TEXT PRIMARY KEY, "
                    "schema_version TEXT NOT NULL, "
                    "definition_json TEXT NOT NULL, "
                    "state TEXT NOT NULL, "
                    "created_at TEXT NOT NULL, "
                    "updated_at TEXT NOT NULL, "
                    "assigned_at TEXT, "
                    "started_at TEXT, "
                    "finished_at TEXT, "
                    "cleaned_at TEXT, "
                    "failure_reason TEXT, "
                    "worktree_path TEXT, "
                    "branch_name TEXT, "
                    "base_commit TEXT, "
                    "head_commit TEXT, "
                    "state_version INTEGER NOT NULL"
                    ")"
                )
                self._create_leases_schema(connection)
                self._create_execution_schema(connection)
                self._create_validation_schema(connection)
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES(?, ?)",
                    ("repo_root", str(self.repo_root)),
                )
                connection.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
                connection.commit()
            elif version == 1:
                connection.execute("BEGIN IMMEDIATE")
                self._create_leases_schema(connection)
                self._create_execution_schema(connection)
                self._create_validation_schema(connection)
                connection.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
                connection.commit()
            elif version == 2:
                connection.execute("BEGIN IMMEDIATE")
                self._create_execution_schema(connection)
                self._create_validation_schema(connection)
                connection.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
                connection.commit()
            elif version == 3:
                connection.execute("BEGIN IMMEDIATE")
                self._create_validation_schema(connection)
                connection.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
                connection.commit()
            elif version != DB_SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(
                    f"unsupported state database schema version: {version}"
                )
            self._verify_binding(connection)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _create_leases_schema(connection: sqlite3.Connection) -> None:
        connection.execute(_LEASES_TABLE_SQL)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_leases_active_path "
            "ON leases(status, expires_at, canonical_pattern)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_leases_task "
            "ON leases(task_id, acquired_at)"
        )

    @staticmethod
    def _create_execution_schema(connection: sqlite3.Connection) -> None:
        connection.execute(_EXECUTIONS_TABLE_SQL)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_executions_task "
            "ON executions(task_id, created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_executions_incomplete "
            "ON executions(status, created_at)"
        )

    @staticmethod
    def _create_validation_schema(connection: sqlite3.Connection) -> None:
        connection.execute(_VALIDATIONS_TABLE_SQL)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_validations_task "
            "ON validations(task_id, started_at)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_validations_active_execution "
            "ON validations(task_id, execution_id) "
            "WHERE status = 'running' AND execution_id IS NOT NULL"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_validations_active_task "
            "ON validations(task_id) "
            "WHERE status = 'running' AND execution_id IS NULL"
        )

    def _verify_binding(self, connection: sqlite3.Connection) -> None:
        try:
            stored = connection.execute(
                "SELECT value FROM metadata WHERE key = ?", ("repo_root",)
            ).fetchone()
        except sqlite3.Error as exc:
            raise StateCorruptionError("state database metadata table is invalid") from exc
        if stored is None:
            raise StateCorruptionError("state database has no repository binding")
        if _canonical_root(stored[0]) != _canonical_root(str(self.repo_root)):
            raise RepositoryBindingError("state database belongs to another repository")

    def create(self, definition: TaskDefinition) -> TaskRecord:
        if not isinstance(definition, TaskDefinition):
            raise StateStoreError("create requires a validated TaskDefinition")
        now = _timestamp(self._clock())
        definition_json = json.dumps(
            definition.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO tasks("
                    "task_id, schema_version, definition_json, state, created_at, updated_at, state_version"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        definition.task_id,
                        definition.schema_version,
                        definition_json,
                        TaskState.PENDING.value,
                        now,
                        now,
                        0,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateTaskError(definition.task_id) from exc
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(definition.task_id)

    def get(self, task_id: str) -> TaskRecord:
        task_id = _task_id(task_id)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise StateStoreError("cannot read task state") from exc
        finally:
            connection.close()
        if row is None:
            raise TaskNotFoundError(task_id)
        return self._row_to_record(row)

    def list_tasks(self) -> tuple[TaskRecord, ...]:
        """Return all persisted tasks in deterministic creation order."""

        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY created_at, task_id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError("cannot list task state") from exc
        finally:
            connection.close()
        return tuple(self._row_to_record(row) for row in rows)

    def transition(
        self,
        task_id: str,
        new_state: TaskState | str,
        *,
        reason: str | None = None,
    ) -> TaskRecord:
        task_id = _task_id(task_id)
        requested = _state(new_state, task_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskNotFoundError(task_id)
            current = _stored_state(row["state"], task_id)
            if requested not in _LEGAL_TRANSITIONS[current]:
                raise InvalidTaskTransition(task_id, current, requested)

            now = _timestamp(self._clock())
            assigned_at = row["assigned_at"]
            started_at = row["started_at"]
            finished_at = row["finished_at"]
            cleaned_at = row["cleaned_at"]
            failure_reason = row["failure_reason"]
            if requested == TaskState.ASSIGNED:
                assigned_at = now
            elif requested == TaskState.RUNNING:
                started_at = now
            elif requested in {TaskState.COMPLETED, TaskState.FAILED}:
                finished_at = now
            elif requested == TaskState.CLEANED:
                cleaned_at = now
            if requested == TaskState.FAILED:
                failure_reason = reason

            updated = connection.execute(
                "UPDATE tasks SET state = ?, updated_at = ?, assigned_at = ?, started_at = ?, "
                "finished_at = ?, cleaned_at = ?, failure_reason = ?, state_version = state_version + 1 "
                "WHERE task_id = ? AND state_version = ?",
                (
                    requested.value,
                    now,
                    assigned_at,
                    started_at,
                    finished_at,
                    cleaned_at,
                    failure_reason,
                    task_id,
                    row["state_version"],
                ),
            )
            if updated.rowcount != 1:
                raise ConcurrentStateUpdateError(f"state version changed for task: {task_id}")
            result = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if result is None:
            raise StateStoreError(f"task disappeared after transition: {task_id}")
        return self._row_to_record(result)

    def update_task_runtime_metadata(
        self,
        task_id: str,
        *,
        worktree_path: str | None | object = _UNSET,
        branch_name: str | None | object = _UNSET,
        base_commit: str | None | object = _UNSET,
        head_commit: str | None | object = _UNSET,
    ) -> TaskRecord:
        task_id = _task_id(task_id)
        updates = {
            "worktree_path": worktree_path,
            "branch_name": branch_name,
            "base_commit": base_commit,
            "head_commit": head_commit,
        }
        selected = {name: value for name, value in updates.items() if value is not _UNSET}
        if not selected:
            return self.get(task_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone() is None:
                raise TaskNotFoundError(task_id)
            now = _timestamp(self._clock())
            assignments = ", ".join(f"{name} = ?" for name in selected)
            values = [selected[name] for name in selected]
            values.extend([now, task_id])
            connection.execute(
                f"UPDATE tasks SET {assignments}, updated_at = ?, state_version = state_version + 1 "
                "WHERE task_id = ?",
                values,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(task_id)

    def create_execution(self, execution: ExecutionResult) -> ExecutionResult:
        if not isinstance(execution, ExecutionResult):
            raise StateStoreError("create_execution requires an ExecutionResult")
        if execution.status is not ExecutionStatus.CREATED:
            raise StateStoreError("new executions must start in created state")
        _execution_id(execution.execution_id)
        if not execution.command:
            raise StateStoreError("execution command cannot be empty")
        if execution.timeout_seconds <= 0:
            raise StateStoreError("execution timeout must be positive")
        _timestamp(execution.created_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (execution.task_id,)
            ).fetchone() is None:
                raise TaskNotFoundError(execution.task_id)
            try:
                connection.execute(
                    "INSERT INTO executions("
                    "execution_id, task_id, worker_id, command_json, worktree_path, pid, "
                    "created_at, started_at, finished_at, duration_seconds, exit_code, status, "
                    "timeout_seconds, artifact_dir, stdout_path, stderr_path"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        execution.execution_id,
                        execution.task_id,
                        execution.worker_id,
                        json.dumps(list(execution.command), ensure_ascii=False, separators=(",", ":")),
                        execution.worktree_path,
                        execution.pid,
                        _timestamp(execution.created_at),
                        _optional_timestamp(execution.started_at),
                        _optional_timestamp(execution.finished_at),
                        execution.duration_seconds,
                        execution.exit_code,
                        execution.status.value,
                        execution.timeout_seconds,
                        execution.artifact_dir,
                        execution.stdout_path,
                        execution.stderr_path,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateExecutionError(execution.execution_id) from exc
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_execution(execution.execution_id)

    def get_execution(self, execution_id: str) -> ExecutionResult:
        execution_id = _execution_id(execution_id)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise StateStoreError("cannot read execution state") from exc
        finally:
            connection.close()
        if row is None:
            raise ExecutionNotFoundError(execution_id)
        return self._row_to_execution(row)

    def list_executions(self, task_id: str | None = None) -> tuple[ExecutionResult, ...]:
        connection = self._connect()
        try:
            if task_id is None:
                rows = connection.execute(
                    "SELECT * FROM executions ORDER BY created_at, execution_id"
                ).fetchall()
            else:
                task_id = _task_id(task_id)
                rows = connection.execute(
                    "SELECT * FROM executions WHERE task_id = ? "
                    "ORDER BY created_at, execution_id",
                    (task_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError("cannot list execution state") from exc
        finally:
            connection.close()
        return tuple(self._row_to_execution(row) for row in rows)

    def find_incomplete_executions(self) -> tuple[ExecutionResult, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM executions WHERE status = ? "
                "ORDER BY created_at, execution_id",
                (ExecutionStatus.RUNNING.value,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError("cannot list incomplete executions") from exc
        finally:
            connection.close()
        return tuple(self._row_to_execution(row) for row in rows)

    def start_execution(
        self, execution_id: str, *, pid: int, started_at: datetime
    ) -> ExecutionResult:
        execution_id = _execution_id(execution_id)
        started_text = _timestamp(started_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            if row is None:
                raise ExecutionNotFoundError(execution_id)
            current = _execution_status(row["status"], execution_id)
            if current is not ExecutionStatus.CREATED:
                raise InvalidExecutionTransition(execution_id, current, ExecutionStatus.RUNNING)
            connection.execute(
                "UPDATE executions SET status = ?, pid = ?, started_at = ? "
                "WHERE execution_id = ? AND status = ?",
                (
                    ExecutionStatus.RUNNING.value,
                    pid,
                    started_text,
                    execution_id,
                    ExecutionStatus.CREATED.value,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_execution(execution_id)

    def finish_execution(
        self,
        execution_id: str,
        *,
        status: ExecutionStatus,
        finished_at: datetime,
        exit_code: int | None,
        duration_seconds: float | None,
    ) -> ExecutionResult:
        execution_id = _execution_id(execution_id)
        if status not in TERMINAL_EXECUTION_STATES:
            raise StateStoreError("execution finish status must be terminal")
        finished_text = _timestamp(finished_at)
        if duration_seconds is not None and duration_seconds < 0:
            raise StateStoreError("execution duration cannot be negative")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            if row is None:
                raise ExecutionNotFoundError(execution_id)
            current = _execution_status(row["status"], execution_id)
            if current in TERMINAL_EXECUTION_STATES:
                raise InvalidExecutionTransition(execution_id, current, status)
            connection.execute(
                "UPDATE executions SET status = ?, finished_at = ?, exit_code = ?, "
                "duration_seconds = ? WHERE execution_id = ? AND status IN (?, ?)",
                (
                    status.value,
                    finished_text,
                    exit_code,
                    duration_seconds,
                    execution_id,
                    ExecutionStatus.CREATED.value,
                    ExecutionStatus.RUNNING.value,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_execution(execution_id)

    def begin_validation(self, report: ValidationReport) -> ValidationReport:
        if not isinstance(report, ValidationReport):
            raise StateStoreError("begin_validation requires a ValidationReport")
        if report.status is not ValidationStatus.RUNNING:
            raise StateStoreError("new validations must start in running state")
        _validation_id(report.validation_id)
        _timestamp(report.started_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (report.task_id,)
            ).fetchone() is None:
                raise TaskNotFoundError(report.task_id)
            try:
                connection.execute(
                    "INSERT INTO validations("
                    "validation_id, task_id, execution_id, worker_id, status, started_at, "
                    "finished_at, report_json, artifact_dir"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        report.validation_id,
                        report.task_id,
                        report.execution_id,
                        report.worker_id,
                        report.status.value,
                        _timestamp(report.started_at),
                        _timestamp(report.finished_at),
                        json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True),
                        report.artifact_dir,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValidationAlreadyRunningError(
                    report.task_id, report.execution_id
                ) from exc
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_validation(report.validation_id)

    def finish_validation(self, report: ValidationReport) -> ValidationReport:
        if not isinstance(report, ValidationReport):
            raise StateStoreError("finish_validation requires a ValidationReport")
        if report.status is ValidationStatus.RUNNING:
            raise StateStoreError("finished validation cannot remain running")
        validation_id = _validation_id(report.validation_id)
        finished_text = _timestamp(report.finished_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM validations WHERE validation_id = ?", (validation_id,)
            ).fetchone()
            if row is None:
                raise ValidationNotFoundError(validation_id)
            if (
                row["task_id"] != report.task_id
                or row["execution_id"] != report.execution_id
                or row["worker_id"] != report.worker_id
                or row["artifact_dir"] != report.artifact_dir
            ):
                raise StateStoreError("validation report identity does not match stored state")
            current = _validation_status(row["status"], validation_id)
            if current is not ValidationStatus.RUNNING:
                raise InvalidValidationTransition(validation_id, current)
            connection.execute(
                "UPDATE validations SET status = ?, finished_at = ?, report_json = ? "
                "WHERE validation_id = ? AND status = ?",
                (
                    report.status.value,
                    finished_text,
                    json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True),
                    validation_id,
                    ValidationStatus.RUNNING.value,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_validation(validation_id)

    def get_validation(self, validation_id: str) -> ValidationReport:
        validation_id = _validation_id(validation_id)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM validations WHERE validation_id = ?", (validation_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise StateStoreError("cannot read validation state") from exc
        finally:
            connection.close()
        if row is None:
            raise ValidationNotFoundError(validation_id)
        return self._row_to_validation(row)

    def list_validations(self, task_id: str | None = None) -> tuple[ValidationReport, ...]:
        connection = self._connect()
        try:
            if task_id is None:
                rows = connection.execute(
                    "SELECT * FROM validations ORDER BY started_at, validation_id"
                ).fetchall()
            else:
                task_id = _task_id(task_id)
                rows = connection.execute(
                    "SELECT * FROM validations WHERE task_id = ? "
                    "ORDER BY started_at, validation_id",
                    (task_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError("cannot list validations") from exc
        finally:
            connection.close()
        return tuple(self._row_to_validation(row) for row in rows)

    def _row_to_record(self, row: sqlite3.Row) -> TaskRecord:
        task_id = row["task_id"]
        state = _stored_state(row["state"], task_id)
        try:
            payload: Any = json.loads(row["definition_json"])
            definition = TaskDefinition.from_mapping(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateCorruptionError(f"invalid task definition for task {task_id}") from exc
        try:
            version = int(row["state_version"])
        except (TypeError, ValueError) as exc:
            raise StateCorruptionError(f"invalid state version for task {task_id}") from exc
        if version < 0:
            raise StateCorruptionError(f"negative state version for task {task_id}")
        created_at = _parse_timestamp(row["created_at"], "created_at", task_id)
        updated_at = _parse_timestamp(row["updated_at"], "updated_at", task_id)
        if created_at is None or updated_at is None:
            raise StateCorruptionError(f"missing required timestamp for task {task_id}")
        return TaskRecord(
            task_id=task_id,
            state=state,
            definition=definition,
            created_at=created_at,
            updated_at=updated_at,
            assigned_at=_parse_timestamp(row["assigned_at"], "assigned_at", task_id),
            started_at=_parse_timestamp(row["started_at"], "started_at", task_id),
            finished_at=_parse_timestamp(row["finished_at"], "finished_at", task_id),
            cleaned_at=_parse_timestamp(row["cleaned_at"], "cleaned_at", task_id),
            failure_reason=row["failure_reason"],
            worktree_path=row["worktree_path"],
            branch_name=row["branch_name"],
            base_commit=row["base_commit"],
            head_commit=row["head_commit"],
            version=version,
        )

    def _row_to_execution(self, row: sqlite3.Row) -> ExecutionResult:
        execution_id = row["execution_id"]
        try:
            status = _execution_status(row["status"], execution_id)
            command = json.loads(row["command_json"])
            if (
                not isinstance(command, list)
                or not command
                or any(not isinstance(item, str) or not item for item in command)
            ):
                raise ValueError("invalid command")
            timeout = float(row["timeout_seconds"])
            if timeout <= 0:
                raise ValueError("invalid timeout")
            duration = row["duration_seconds"]
            if duration is not None:
                duration = float(duration)
                if duration < 0:
                    raise ValueError("invalid duration")
            pid = row["pid"]
            if pid is not None:
                pid = int(pid)
            exit_code = row["exit_code"]
            if exit_code is not None:
                exit_code = int(exit_code)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateCorruptionError(f"invalid execution record: {execution_id}") from exc
        created_at = _parse_timestamp(row["created_at"], "created_at", execution_id)
        if created_at is None:
            raise StateCorruptionError(f"missing execution created_at: {execution_id}")
        return ExecutionResult(
            execution_id=execution_id,
            task_id=row["task_id"],
            worker_id=row["worker_id"],
            status=status,
            command=tuple(command),
            worktree_path=row["worktree_path"],
            pid=pid,
            created_at=created_at,
            started_at=_parse_timestamp(row["started_at"], "started_at", execution_id),
            finished_at=_parse_timestamp(row["finished_at"], "finished_at", execution_id),
            duration_seconds=duration,
            exit_code=exit_code,
            timeout_seconds=timeout,
            artifact_dir=row["artifact_dir"],
            stdout_path=row["stdout_path"],
            stderr_path=row["stderr_path"],
        )

    def _row_to_validation(self, row: sqlite3.Row) -> ValidationReport:
        validation_id = row["validation_id"]
        try:
            payload: Any = json.loads(row["report_json"])
            if not isinstance(payload, dict):
                raise ValueError("report is not an object")
            report = _validation_from_mapping(payload, validation_id)
            if (
                report.validation_id != validation_id
                or report.task_id != row["task_id"]
                or report.execution_id != row["execution_id"]
                or report.worker_id != row["worker_id"]
                or report.status.value != row["status"]
                or report.artifact_dir != row["artifact_dir"]
            ):
                raise ValueError("validation report does not match stored summary")
            return report
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateCorruptionError(
                f"invalid validation report: {validation_id}"
            ) from exc


def _canonical_root(value: str) -> str:
    return str(Path(value).expanduser().resolve(strict=False)).casefold()


def _task_id(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise StateStoreError("task_id must be a non-empty string")
    return value


def _execution_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or value in {".", ".."}
    ):
        raise StateStoreError("execution_id must be a safe non-empty string")
    return value


def _execution_status(value: object, execution_id: str) -> ExecutionStatus:
    try:
        return ExecutionStatus(value)
    except (TypeError, ValueError) as exc:
        raise StateCorruptionError(f"invalid execution status: {execution_id}") from exc


def _validation_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or value in {".", ".."}
    ):
        raise StateStoreError("validation_id must be a safe non-empty string")
    return value


def _validation_status(value: object, validation_id: str) -> ValidationStatus:
    try:
        return ValidationStatus(value)
    except (TypeError, ValueError) as exc:
        raise StateCorruptionError(f"invalid validation status: {validation_id}") from exc


def _validation_from_mapping(payload: dict[str, Any], validation_id: str) -> ValidationReport:
    def optional_text(name: str) -> str | None:
        value = payload.get(name)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"invalid {name}")
        return value

    status = _validation_status(payload.get("status"), validation_id)
    started_at = _parse_timestamp(payload.get("started_at"), "started_at", validation_id)
    finished_at = _parse_timestamp(payload.get("finished_at"), "finished_at", validation_id)
    if started_at is None or finished_at is None:
        raise ValueError("validation timestamps are required")
    execution_value = payload.get("execution_status")
    execution_status = (
        None
        if execution_value is None
        else ExecutionStatus(execution_value)
    )
    check_results: list[ValidationCheckResult] = []
    raw_checks = payload.get("required_check_results", [])
    if not isinstance(raw_checks, list):
        raise ValueError("invalid required_check_results")
    for raw in raw_checks:
        if not isinstance(raw, dict):
            raise ValueError("invalid required check result")
        check_started = _parse_timestamp(raw.get("started_at"), "check.started_at", validation_id)
        check_finished = _parse_timestamp(raw.get("finished_at"), "check.finished_at", validation_id)
        command = raw.get("command")
        if (
            check_started is None
            or check_finished is None
            or not isinstance(command, list)
            or not command
            or any(not isinstance(item, str) for item in command)
        ):
            raise ValueError("invalid required check timestamps or command")
        duration_value = raw.get("duration_seconds")
        duration = float(duration_value)
        if duration < 0 or not math.isfinite(duration):
            raise ValueError("invalid required check duration")
        check_results.append(
            ValidationCheckResult(
                name=required_text_from(raw, "name"),
                command=tuple(command),
                started_at=check_started,
                finished_at=check_finished,
                duration_seconds=duration,
                exit_code=None if raw.get("exit_code") is None else int(raw["exit_code"]),
                status=CheckStatus(raw.get("status")),
                stdout_path=required_text_from(raw, "stdout_path"),
                stderr_path=required_text_from(raw, "stderr_path"),
            )
        )
    return ValidationReport(
        validation_id=required_text_from(payload, "validation_id"),
        task_id=required_text_from(payload, "task_id"),
        execution_id=optional_text("execution_id"),
        worker_id=optional_text("worker_id"),
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        expected_branch=optional_text("expected_branch"),
        actual_branch=optional_text("actual_branch"),
        base_commit=optional_text("base_commit"),
        reported_commit=optional_text("reported_commit"),
        verified_commit=optional_text("verified_commit"),
        actual_changed_files=_text_tuple(payload.get("actual_changed_files", [])),
        allowlist_violations=_text_tuple(payload.get("allowlist_violations", [])),
        denylist_violations=_text_tuple(payload.get("denylist_violations", [])),
        required_check_results=tuple(check_results),
        execution_status=execution_status,
        lease_valid=_optional_bool(payload.get("lease_valid"), "lease_valid"),
        blocking_reasons=_text_tuple(payload.get("blocking_reasons", [])),
        artifact_dir=required_text_from(payload, "artifact_dir"),
    )


def required_text_from(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing {name}")
    return value


def _optional_bool(value: Any, name: str) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"invalid {name}")
    return value


def _text_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("expected list of strings")
    return tuple(value)


def _optional_timestamp(value: datetime | None) -> str | None:
    return _timestamp(value) if value is not None else None


def _state(value: TaskState | str, task_id: str) -> TaskState:
    if isinstance(value, TaskState):
        return value
    try:
        return TaskState(value)
    except (TypeError, ValueError) as exc:
        raise InvalidTaskTransition(task_id, None, str(value)) from exc


def _stored_state(value: object, task_id: str) -> TaskState:
    try:
        return TaskState(value)
    except (TypeError, ValueError) as exc:
        raise StateCorruptionError(f"invalid stored state for task {task_id}") from exc
