from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_worktree.leases import (
    AlreadyReleasedError,
    InvalidLeaseTTL,
    LeaseConflictError,
    LeaseExpiredError,
    LeaseGenerationError,
    LeaseManager,
    LeaseNotActiveError,
    LeaseOwnershipError,
    LeasePathError,
    LeaseTaskStateError,
    canonicalize_lease_paths,
    paths_overlap,
)
from agent_worktree.models import TaskDefinition, TaskState, TaskValidationError
from agent_worktree.state import (
    DB_SCHEMA_VERSION,
    StateStoreError,
    TaskStore,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 21, 15, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int | float) -> None:
        self.value += timedelta(seconds=seconds)


def definition(task_id: str) -> TaskDefinition:
    return TaskDefinition.from_mapping(
        {
            "schema_version": "0.1",
            "task_id": task_id,
            "objective": "Reserve a source path for a generic coding task.",
            "base_ref": "HEAD",
            "read_paths": ["src/**"],
            "write_paths": ["src/worker.py"],
            "deny_paths": [".env"],
            "worker_id": f"worker-{task_id}",
            "worker_command": ["python", "worker.py"],
            "timeout_seconds": 300,
            "required_checks": [],
        }
    )


def make_store(tmp_path: Path, clock: FakeClock | None = None) -> tuple[TaskStore, FakeClock]:
    test_clock = clock or FakeClock()
    root = tmp_path / "repo"
    root.mkdir()
    return TaskStore(root, clock=test_clock), test_clock


def add_task(store: TaskStore, task_id: str, state: TaskState = TaskState.ASSIGNED) -> None:
    store.create(definition(task_id))
    if state is TaskState.ASSIGNED:
        store.transition(task_id, state)
    elif state is TaskState.BLOCKED:
        store.transition(task_id, TaskState.BLOCKED)
    elif state is TaskState.RUNNING:
        store.transition(task_id, TaskState.ASSIGNED)
        store.transition(task_id, state)
    elif state is TaskState.COMPLETED:
        store.transition(task_id, TaskState.ASSIGNED)
        store.transition(task_id, TaskState.RUNNING)
        store.transition(task_id, state)
    elif state is TaskState.FAILED:
        store.transition(task_id, TaskState.ASSIGNED)
        store.transition(task_id, state)
    elif state is TaskState.CLEANED:
        store.transition(task_id, TaskState.ASSIGNED)
        store.transition(task_id, TaskState.RUNNING)
        store.transition(task_id, TaskState.COMPLETED)
        store.transition(task_id, state)
    elif state is not TaskState.PENDING:
        raise AssertionError(state)


def test_path_canonicalization_and_windows_case_policy() -> None:
    display, canonical = canonicalize_lease_paths(
        [r"src\./workers//", "src/workers/**", "SRC\\WORKERS/**"],
        windows_casefold=True,
    )
    assert display == ("src/workers", "src/workers/**")
    assert canonical == ("src/workers", "src/workers/**")

    with pytest.raises(LeasePathError):
        canonicalize_lease_paths([])
    with pytest.raises(LeasePathError):
        canonicalize_lease_paths(["src/*.py"])
    for invalid in ["", "../secret", "src/../../secret", "/absolute", r"C:\repo\file", "src\x00file"]:
        with pytest.raises(LeasePathError):
            canonicalize_lease_paths([invalid])


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("src/file.py", "src/file.py", True),
        ("src/file.py", "src/other.py", False),
        ("src/**", "src/file.py", True),
        ("src/**", "src/nested/file.py", True),
        ("src/**", "src-extra/file.py", False),
        ("src/**", "tests/**", False),
        ("src/a/**", "src/**", True),
        ("src/a/**", "src/b/**", False),
    ],
)
def test_segment_aware_overlap(left: str, right: str, expected: bool) -> None:
    assert paths_overlap(left, right) is expected


def test_only_assigned_and_running_tasks_can_acquire(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    manager = LeaseManager(store, clock=clock)
    for task_id, state in [("pending", TaskState.PENDING), ("blocked", TaskState.BLOCKED), ("completed", TaskState.COMPLETED), ("failed", TaskState.FAILED), ("cleaned", TaskState.CLEANED)]:
        add_task(store, task_id, state)
        with pytest.raises(LeaseTaskStateError):
            manager.acquire(task_id, "worker", [f"{task_id}.py"], ttl_seconds=30)

    add_task(store, "assigned")
    add_task(store, "running", TaskState.RUNNING)
    assert manager.acquire("assigned", "worker", ["assigned.py"], ttl_seconds=30).status.value == "active"
    assert manager.acquire("running", "worker", ["running.py"], ttl_seconds=30).status.value == "active"


@pytest.mark.parametrize("ttl", [0, -1, float("nan"), float("inf"), 86_401, True, "30"])
def test_ttl_is_finite_and_bounded(tmp_path: Path, ttl: object) -> None:
    store, clock = make_store(tmp_path)
    add_task(store, "ttl-task")
    with pytest.raises(InvalidLeaseTTL):
        LeaseManager(store, clock=clock).acquire("ttl-task", "worker", ["file.py"], ttl_seconds=ttl)  # type: ignore[arg-type]


def test_acquire_is_atomic_and_conflict_reports_both_sides(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    manager = LeaseManager(store, clock=clock)
    add_task(store, "owner")
    add_task(store, "contender")
    owner = manager.acquire("owner", "owner-worker", ["src/owned.py"], ttl_seconds=30)

    with pytest.raises(LeaseConflictError) as error:
        manager.acquire(
            "contender", "contender-worker", ["src/other.py", "src/owned.py"], ttl_seconds=30
        )
    assert error.value.requested_task_id == "contender"
    assert error.value.requested_path == "src/owned.py"
    assert error.value.conflicting_task_id == "owner"
    assert error.value.conflicting_path == "src/owned.py"
    assert error.value.lease_id == owner.lease_id
    assert manager.list_task("contender") == ()


def test_parent_child_and_case_insensitive_conflicts(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    manager = LeaseManager(store, clock=clock, windows_casefold=True)
    add_task(store, "parent")
    add_task(store, "child")
    manager.acquire("parent", "worker", ["SRC/**"], ttl_seconds=30)
    with pytest.raises(LeaseConflictError):
        manager.acquire("child", "worker", ["src/nested/file.py"], ttl_seconds=30)


def test_renew_requires_owner_and_generation_and_increments_generation(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    manager = LeaseManager(store, clock=clock)
    add_task(store, "renew-task")
    lease = manager.acquire("renew-task", "worker-a", ["src/file.py"], ttl_seconds=10)

    with pytest.raises(LeaseOwnershipError):
        manager.renew(lease.lease_id, "renew-task", "worker-b", lease.generation, ttl_seconds=10)
    with pytest.raises(LeaseGenerationError):
        manager.renew(lease.lease_id, "renew-task", "worker-a", 9, ttl_seconds=10)

    clock.advance(3)
    renewed = manager.renew(
        lease.lease_id, "renew-task", "worker-a", lease.generation, ttl_seconds=20
    )
    assert renewed.generation == lease.generation + 1
    assert renewed.expires_at == clock.value + timedelta(seconds=20)


def test_expired_leases_do_not_block_and_cannot_renew(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    manager = LeaseManager(store, clock=clock)
    add_task(store, "expired-owner")
    add_task(store, "new-owner")
    expired = manager.acquire("expired-owner", "worker", ["same.py"], ttl_seconds=5)
    clock.advance(5)

    with pytest.raises(LeaseExpiredError):
        manager.renew(expired.lease_id, "expired-owner", "worker", expired.generation, ttl_seconds=5)
    replacement = manager.acquire("new-owner", "worker", ["same.py"], ttl_seconds=5)
    assert replacement.lease_id != expired.lease_id
    assert manager.find_stale()[0].lease_id == expired.lease_id


def test_release_is_guarded_and_already_released_is_explicit(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    manager = LeaseManager(store, clock=clock)
    add_task(store, "release-task")
    lease = manager.acquire("release-task", "worker-a", ["file.py"], ttl_seconds=30)
    with pytest.raises(LeaseOwnershipError):
        manager.release(lease.lease_id, "release-task", "worker-b", lease.generation)
    with pytest.raises(LeaseGenerationError):
        manager.release(lease.lease_id, "release-task", "worker-a", 99)

    released = manager.release(lease.lease_id, "release-task", "worker-a", lease.generation)
    assert released.status.value == "released"
    with pytest.raises(AlreadyReleasedError):
        manager.release(lease.lease_id, "release-task", "worker-a", lease.generation)


def test_recover_stale_preserves_history_and_does_not_change_task_state(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    manager = LeaseManager(store, clock=clock)
    add_task(store, "stale-task")
    before = store.get("stale-task")
    lease = manager.acquire("stale-task", "worker", ["stale.py"], ttl_seconds=3)
    clock.advance(3)
    recovered = manager.recover_stale()

    assert recovered[0].lease_id == lease.lease_id
    assert recovered[0].status.value == "stale"
    assert recovered[0].generation == lease.generation + 1
    assert manager.get(lease.lease_id).paths == ("stale.py",)
    after = store.get("stale-task")
    assert after.state is before.state
    assert after.version == before.version
    with pytest.raises(LeaseNotActiveError):
        manager.release(lease.lease_id, "stale-task", "worker", recovered[0].generation)


def test_restart_persists_lease_history(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    add_task(store, "restart-lease")
    first = LeaseManager(store, clock=clock)
    lease = first.acquire("restart-lease", "worker", ["src/**", "README.md"], ttl_seconds=30)

    reopened = TaskStore(store.repo_root, clock=clock)
    recovered = LeaseManager(reopened, clock=clock).get(lease.lease_id)
    assert recovered.to_dict() == lease.to_dict()


def test_v1_database_migrates_to_latest_lease_schema(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    add_task(store, "migration-task")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DROP TABLE leases")
        connection.execute("PRAGMA user_version = 1")

    migrated = TaskStore(store.repo_root, clock=clock)
    with sqlite3.connect(migrated.db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
        assert connection.execute(
            "SELECT type FROM sqlite_master WHERE name = 'leases'"
        ).fetchone()[0] == "table"
    assert migrated.get("migration-task").state is TaskState.ASSIGNED


def test_failed_v1_migration_rolls_back(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DROP TABLE leases")
        connection.execute("CREATE VIEW leases AS SELECT 1 AS invalid")
        connection.execute("PRAGMA user_version = 1")

    with pytest.raises(StateStoreError):
        TaskStore(store.repo_root)
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute(
            "SELECT type FROM sqlite_master WHERE name = 'leases'"
        ).fetchone()[0] == "view"


def test_overlapping_acquire_serializes_to_one_success(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    add_task(store, "concurrent-a")
    add_task(store, "concurrent-b")
    managers = [LeaseManager(TaskStore(store.repo_root, clock=clock), clock=clock) for _ in range(2)]
    barrier = threading.Barrier(2)
    results: list[object] = []

    def acquire(manager: LeaseManager, task_id: str) -> None:
        barrier.wait()
        try:
            results.append(manager.acquire(task_id, "worker", ["src/**"], ttl_seconds=30))
        except LeaseConflictError as exc:
            results.append(exc)

    threads = [
        threading.Thread(target=acquire, args=(managers[0], "concurrent-a")),
        threading.Thread(target=acquire, args=(managers[1], "concurrent-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(isinstance(result, LeaseConflictError) for result in results) == 1
    assert sum(result.__class__.__name__ == "Lease" for result in results) == 1


def test_non_overlapping_concurrent_acquire_both_succeed(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    add_task(store, "nonoverlap-a")
    add_task(store, "nonoverlap-b")
    managers = [LeaseManager(TaskStore(store.repo_root, clock=clock), clock=clock) for _ in range(2)]
    barrier = threading.Barrier(2)
    results: list[object] = []

    def acquire(manager: LeaseManager, task_id: str, path: str) -> None:
        barrier.wait()
        results.append(manager.acquire(task_id, "worker", [path], ttl_seconds=30))

    threads = [
        threading.Thread(target=acquire, args=(managers[0], "nonoverlap-a", "a.py")),
        threading.Thread(target=acquire, args=(managers[1], "nonoverlap-b", "b.py")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert all(result.__class__.__name__ == "Lease" for result in results)
