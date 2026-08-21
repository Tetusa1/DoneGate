"""Transactional path leases for coordinating coding-agent tasks.

The lease manager deliberately owns only path ownership.  It does not start
workers, create worktrees, or change task state.  A lease is represented by
one row per requested path so a multi-path acquisition can be inserted and
checked atomically in one SQLite transaction.
"""

from __future__ import annotations

import math
import os
import sqlite3
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Sequence

from .models import Lease, LeaseStatus, TaskState, canonicalize_path_pattern
from .state import StateCorruptionError, StateStoreError, TaskNotFoundError, TaskStore


MIN_TTL_SECONDS = 1
MAX_TTL_SECONDS = 86_400
LEASE_ACQUIRE_ALLOWED_STATES = frozenset({TaskState.ASSIGNED, TaskState.RUNNING})


class LeaseError(StateStoreError):
    """Base error for path lease operations."""


class InvalidLeaseIdentityError(LeaseError):
    """Raised when a lease identity is empty or unsafe."""


class InvalidLeaseTTL(LeaseError):
    """Raised when a lease TTL is not a finite value in the supported range."""


class LeasePathError(LeaseError):
    """Raised when a path list is empty or contains an invalid pattern."""


class LeaseTaskStateError(LeaseError):
    """Raised when a task is not eligible to acquire a lease."""

    def __init__(self, task_id: str, state: TaskState) -> None:
        self.task_id = task_id
        self.state = state
        super().__init__(
            f"LeaseTaskState: task_id={task_id}, state={state.value}, "
            "allowed=assigned,running"
        )


class LeaseConflictError(LeaseError):
    """Raised when a live lease overlaps a requested path."""

    def __init__(
        self,
        *,
        requested_task_id: str,
        requested_path: str,
        conflicting_task_id: str,
        conflicting_path: str,
        lease_id: str,
    ) -> None:
        self.requested_task_id = requested_task_id
        self.requested_path = requested_path
        self.conflicting_task_id = conflicting_task_id
        self.conflicting_path = conflicting_path
        self.lease_id = lease_id
        super().__init__(
            "LeaseConflict: "
            f"requested_task={requested_task_id}, requested_path={requested_path}, "
            f"conflicting_task={conflicting_task_id}, "
            f"conflicting_path={conflicting_path}, lease_id={lease_id}"
        )


class LeaseNotFoundError(LeaseError):
    """Raised when a lease id does not exist."""

    def __init__(self, lease_id: str) -> None:
        self.lease_id = lease_id
        super().__init__(f"LeaseNotFound: {lease_id}")


class LeaseOwnershipError(LeaseError):
    """Raised when task or worker identity does not own a lease."""

    def __init__(self, lease_id: str, task_id: str, worker_id: str) -> None:
        self.lease_id = lease_id
        self.task_id = task_id
        self.worker_id = worker_id
        super().__init__(
            f"LeaseOwnership: lease_id={lease_id}, task_id={task_id}, worker_id={worker_id}"
        )


class LeaseGenerationError(LeaseError):
    """Raised when a renew/release uses an old lease generation."""

    def __init__(self, lease_id: str, expected: int, actual: int) -> None:
        self.lease_id = lease_id
        self.expected_generation = expected
        self.actual_generation = actual
        super().__init__(
            f"LeaseGeneration: lease_id={lease_id}, expected={expected}, actual={actual}"
        )


class LeaseNotActiveError(LeaseError):
    """Raised when an operation requires an active lease."""

    def __init__(self, lease_id: str, status: LeaseStatus) -> None:
        self.lease_id = lease_id
        self.status = status
        super().__init__(f"LeaseNotActive: lease_id={lease_id}, status={status.value}")


class LeaseExpiredError(LeaseError):
    """Raised when an active lease is past its expiry and cannot be renewed."""

    def __init__(self, lease_id: str) -> None:
        self.lease_id = lease_id
        super().__init__(f"LeaseExpired: {lease_id}")


class AlreadyReleasedError(LeaseError):
    """Raised when release is requested for an already released lease."""

    def __init__(self, lease_id: str) -> None:
        self.lease_id = lease_id
        super().__init__(f"LeaseAlreadyReleased: {lease_id}")


class LeaseCorruptionError(LeaseError):
    """Raised when rows belonging to one lease disagree or are malformed."""


def canonicalize_lease_paths(
    paths: Iterable[str], *, windows_casefold: bool | None = None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return user-facing normalized paths and comparison keys.

    Only exact paths and ``directory/**`` are accepted.  On Windows the
    comparison key is case-folded while the stored display path preserves the
    first normalized spelling supplied by the caller.
    """

    if isinstance(paths, (str, bytes)):
        raise LeasePathError("paths must be a non-empty sequence")
    try:
        values = tuple(paths)
    except TypeError as exc:
        raise LeasePathError("paths must be a non-empty sequence") from exc
    if not values:
        raise LeasePathError("paths must contain at least one path")

    use_casefold = os.name == "nt" if windows_casefold is None else windows_casefold
    display: list[str] = []
    canonical: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        try:
            normalized = canonicalize_path_pattern(value, f"paths[{index}]")
        except ValueError as exc:
            raise LeasePathError(str(exc)) from exc
        key = normalized.casefold() if use_casefold else normalized
        if key in seen:
            continue
        seen.add(key)
        display.append(normalized)
        canonical.append(key)
    if not display:
        raise LeasePathError("paths must contain at least one path")
    return tuple(display), tuple(canonical)


def paths_overlap(left: str, right: str) -> bool:
    """Compare two already canonicalized exact/subtree patterns by segments."""

    left_parts, left_subtree = _pattern_parts(left)
    right_parts, right_subtree = _pattern_parts(right)
    if not left_subtree and not right_subtree:
        return left_parts == right_parts
    if left_subtree and right_subtree:
        return _is_prefix(left_parts, right_parts) or _is_prefix(right_parts, left_parts)
    subtree_parts, exact_parts = (
        (left_parts, right_parts) if left_subtree else (right_parts, left_parts)
    )
    return _is_prefix(subtree_parts, exact_parts)


class LeaseManager:
    """Persistent path lease operations backed by a :class:`TaskStore`."""

    def __init__(
        self,
        store: TaskStore,
        *,
        clock: Callable[[], datetime] | None = None,
        windows_casefold: bool | None = None,
    ) -> None:
        self.store = store
        self._clock = clock or store._clock
        self._windows_casefold = windows_casefold

    def acquire(
        self,
        task_id: str,
        worker_id: str,
        paths: Iterable[str],
        *,
        ttl_seconds: int | float,
        owner_pid: int | None = None,
    ) -> Lease:
        task_id = _identity(task_id, "task_id")
        worker_id = _identity(worker_id, "worker_id")
        ttl = _ttl(ttl_seconds)
        display_paths, canonical_paths = canonicalize_lease_paths(
            paths, windows_casefold=self._windows_casefold
        )
        now = _timestamp(self._clock())
        expires = _timestamp(_parse_timestamp(now) + timedelta(seconds=ttl))
        lease_id = uuid.uuid4().hex
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            state_row = connection.execute(
                "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if state_row is None:
                raise TaskNotFoundError(task_id)
            state = _task_state(state_row["state"], task_id)
            if state not in LEASE_ACQUIRE_ALLOWED_STATES:
                raise LeaseTaskStateError(task_id, state)

            active_rows = connection.execute(
                "SELECT * FROM leases WHERE status = ? AND expires_at > ? "
                "ORDER BY lease_id, canonical_pattern",
                (LeaseStatus.ACTIVE.value, now),
            ).fetchall()
            for requested_path, requested_key in zip(display_paths, canonical_paths):
                for row in active_rows:
                    if paths_overlap(requested_key, row["canonical_pattern"]):
                        raise LeaseConflictError(
                            requested_task_id=task_id,
                            requested_path=requested_path,
                            conflicting_task_id=row["task_id"],
                            conflicting_path=row["path_pattern"],
                            lease_id=row["lease_id"],
                        )

            for display_path, canonical_path in zip(display_paths, canonical_paths):
                connection.execute(
                    "INSERT INTO leases(lease_id, task_id, worker_id, owner_pid, "
                    "path_pattern, canonical_pattern, acquired_at, renewed_at, "
                    "expires_at, status, generation) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        lease_id,
                        task_id,
                        worker_id,
                        owner_pid,
                        display_path,
                        canonical_path,
                        now,
                        now,
                        expires,
                        LeaseStatus.ACTIVE.value,
                        0,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(lease_id)

    def renew(
        self,
        lease_id: str,
        task_id: str,
        worker_id: str,
        generation: int,
        *,
        ttl_seconds: int | float,
    ) -> Lease:
        lease_id = _identity(lease_id, "lease_id")
        task_id = _identity(task_id, "task_id")
        worker_id = _identity(worker_id, "worker_id")
        generation = _generation(generation)
        ttl = _ttl(ttl_seconds)
        now = _timestamp(self._clock())
        expires = _timestamp(_parse_timestamp(now) + timedelta(seconds=ttl))
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            lease = self._locked_get(connection, lease_id)
            _check_owner(lease, task_id, worker_id)
            _check_generation(lease, generation)
            if lease.status is not LeaseStatus.ACTIVE:
                raise LeaseNotActiveError(lease_id, lease.status)
            if lease.expires_at <= _parse_timestamp(now):
                raise LeaseExpiredError(lease_id)
            updated = connection.execute(
                "UPDATE leases SET renewed_at = ?, expires_at = ?, generation = generation + 1 "
                "WHERE lease_id = ? AND status = ? AND generation = ?",
                (now, expires, lease_id, LeaseStatus.ACTIVE.value, generation),
            )
            if updated.rowcount != len(lease.canonical_paths):
                raise LeaseCorruptionError(f"lease update was incomplete: {lease_id}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(lease_id)

    def release(
        self,
        lease_id: str,
        task_id: str,
        worker_id: str,
        generation: int,
    ) -> Lease:
        lease_id = _identity(lease_id, "lease_id")
        task_id = _identity(task_id, "task_id")
        worker_id = _identity(worker_id, "worker_id")
        generation = _generation(generation)
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            lease = self._locked_get(connection, lease_id)
            _check_owner(lease, task_id, worker_id)
            _check_generation(lease, generation)
            if lease.status is LeaseStatus.RELEASED:
                raise AlreadyReleasedError(lease_id)
            if lease.status is not LeaseStatus.ACTIVE:
                raise LeaseNotActiveError(lease_id, lease.status)
            updated = connection.execute(
                "UPDATE leases SET status = ? WHERE lease_id = ? AND status = ? AND generation = ?",
                (LeaseStatus.RELEASED.value, lease_id, LeaseStatus.ACTIVE.value, generation),
            )
            if updated.rowcount != len(lease.canonical_paths):
                raise LeaseCorruptionError(f"lease release was incomplete: {lease_id}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(lease_id)

    def get(self, lease_id: str) -> Lease:
        lease_id = _identity(lease_id, "lease_id")
        connection = self.store._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM leases WHERE lease_id = ? ORDER BY canonical_pattern", (lease_id,)
            ).fetchall()
        finally:
            connection.close()
        if not rows:
            raise LeaseNotFoundError(lease_id)
        return _rows_to_leases(rows)[0]

    def list_active(self) -> tuple[Lease, ...]:
        now = _timestamp(self._clock())
        return self._list(
            "WHERE status = ? AND expires_at > ? ORDER BY lease_id, canonical_pattern",
            (LeaseStatus.ACTIVE.value, now),
        )

    def list_task(self, task_id: str) -> tuple[Lease, ...]:
        task_id = _identity(task_id, "task_id")
        return self._list(
            "WHERE task_id = ? ORDER BY lease_id, canonical_pattern", (task_id,)
        )

    def find_stale(self) -> tuple[Lease, ...]:
        now = _timestamp(self._clock())
        return self._list(
            "WHERE status = ? AND expires_at <= ? ORDER BY lease_id, canonical_pattern",
            (LeaseStatus.ACTIVE.value, now),
        )

    def recover_stale(self) -> tuple[Lease, ...]:
        now = _timestamp(self._clock())
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM leases WHERE status = ? AND expires_at <= ? "
                "ORDER BY lease_id, canonical_pattern",
                (LeaseStatus.ACTIVE.value, now),
            ).fetchall()
            lease_ids = tuple(OrderedDict.fromkeys(row["lease_id"] for row in rows))
            for lease_id in lease_ids:
                updated = connection.execute(
                    "UPDATE leases SET status = ?, generation = generation + 1 "
                    "WHERE lease_id = ? AND status = ? AND expires_at <= ?",
                    (
                        LeaseStatus.STALE.value,
                        lease_id,
                        LeaseStatus.ACTIVE.value,
                        now,
                    ),
                )
                if updated.rowcount == 0:
                    raise LeaseCorruptionError(f"stale recovery found no rows: {lease_id}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if not lease_ids:
            return ()
        return tuple(self.get(lease_id) for lease_id in lease_ids)

    def _list(self, clause: str, parameters: tuple[object, ...]) -> tuple[Lease, ...]:
        connection = self.store._connect()
        try:
            rows = connection.execute(f"SELECT * FROM leases {clause}", parameters).fetchall()
        finally:
            connection.close()
        return _rows_to_leases(rows)

    @staticmethod
    def _locked_get(connection: sqlite3.Connection, lease_id: str) -> Lease:
        rows = connection.execute(
            "SELECT * FROM leases WHERE lease_id = ? ORDER BY canonical_pattern", (lease_id,)
        ).fetchall()
        if not rows:
            raise LeaseNotFoundError(lease_id)
        return _rows_to_leases(rows)[0]


def _pattern_parts(pattern: str) -> tuple[tuple[str, ...], bool]:
    subtree = pattern.endswith("/**")
    value = pattern[:-3] if subtree else pattern
    return tuple(value.split("/")), subtree


def _is_prefix(prefix: tuple[str, ...], value: tuple[str, ...]) -> bool:
    return len(prefix) <= len(value) and value[: len(prefix)] == prefix


def _identity(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise InvalidLeaseIdentityError(f"{field} must be a non-empty string")
    return value


def _generation(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LeaseGenerationError("invalid", 0, -1)
    return value


def _ttl(value: int | float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidLeaseTTL("ttl_seconds must be a finite number")
    if not math.isfinite(value) or value < MIN_TTL_SECONDS or value > MAX_TTL_SECONDS:
        raise InvalidLeaseTTL(
            f"ttl_seconds must be between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS} seconds"
        )
    return value


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LeaseError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise LeaseCorruptionError(f"invalid lease timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LeaseCorruptionError(f"lease timestamp is not timezone-aware: {value!r}")
    return parsed.astimezone(timezone.utc)


def _task_state(value: object, task_id: str) -> TaskState:
    try:
        return TaskState(value)
    except (TypeError, ValueError) as exc:
        raise StateCorruptionError(f"invalid stored state for task {task_id}") from exc


def _check_owner(lease: Lease, task_id: str, worker_id: str) -> None:
    if lease.task_id != task_id or lease.worker_id != worker_id:
        raise LeaseOwnershipError(lease.lease_id, task_id, worker_id)


def _check_generation(lease: Lease, generation: int) -> None:
    if lease.generation != generation:
        raise LeaseGenerationError(lease.lease_id, generation, lease.generation)


def _rows_to_leases(rows: Sequence[sqlite3.Row]) -> tuple[Lease, ...]:
    grouped: OrderedDict[str, list[sqlite3.Row]] = OrderedDict()
    for row in rows:
        grouped.setdefault(row["lease_id"], []).append(row)

    leases: list[Lease] = []
    for lease_id, lease_rows in grouped.items():
        first = lease_rows[0]
        fields = (
            "task_id",
            "worker_id",
            "owner_pid",
            "acquired_at",
            "renewed_at",
            "expires_at",
            "status",
            "generation",
        )
        if any(any(row[field] != first[field] for field in fields) for row in lease_rows[1:]):
            raise LeaseCorruptionError(f"inconsistent rows for lease: {lease_id}")
        try:
            status = LeaseStatus(first["status"])
            generation = int(first["generation"])
            if generation < 0:
                raise ValueError("negative generation")
            owner_pid = first["owner_pid"]
            if owner_pid is not None:
                owner_pid = int(owner_pid)
            acquired_at = _parse_timestamp(first["acquired_at"])
            renewed_at = _parse_timestamp(first["renewed_at"])
            expires_at = _parse_timestamp(first["expires_at"])
        except (TypeError, ValueError) as exc:
            raise LeaseCorruptionError(f"invalid lease row: {lease_id}") from exc

        paths: list[str] = []
        canonical_paths: list[str] = []
        for row in lease_rows:
            path = row["path_pattern"]
            canonical = row["canonical_pattern"]
            if not isinstance(path, str) or not isinstance(canonical, str) or not canonical:
                raise LeaseCorruptionError(f"invalid lease path: {lease_id}")
            paths.append(path)
            canonical_paths.append(canonical)
        leases.append(
            Lease(
                lease_id=lease_id,
                task_id=first["task_id"],
                worker_id=first["worker_id"],
                owner_pid=owner_pid,
                paths=tuple(paths),
                canonical_paths=tuple(canonical_paths),
                acquired_at=acquired_at,
                renewed_at=renewed_at,
                expires_at=expires_at,
                status=status,
                generation=generation,
            )
        )
    return tuple(leases)
