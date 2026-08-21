"""SQLite-backed persistent task state for agent-worktree."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .models import TaskDefinition, TaskRecord, TaskState


DB_SCHEMA_VERSION = 2

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
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES(?, ?)",
                    ("repo_root", str(self.repo_root)),
                )
                connection.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
                connection.commit()
            elif version == 1:
                connection.execute("BEGIN IMMEDIATE")
                self._create_leases_schema(connection)
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


def _canonical_root(value: str) -> str:
    return str(Path(value).expanduser().resolve(strict=False)).casefold()


def _task_id(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise StateStoreError("task_id must be a non-empty string")
    return value


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
