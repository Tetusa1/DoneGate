from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from donegate.models import TaskDefinition, TaskState
from donegate.state import (
    DB_SCHEMA_VERSION,
    DuplicateTaskError,
    InvalidTaskTransition,
    RepositoryBindingError,
    StateCorruptionError,
    TaskNotFoundError,
    TaskStore,
    UnsupportedSchemaVersionError,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 21, 14, 30, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def tick(self) -> None:
        self.value += timedelta(seconds=1)


def valid_definition(task_id: str = "state-task") -> TaskDefinition:
    return TaskDefinition.from_mapping(
        {
            "schema_version": "0.1",
            "task_id": task_id,
            "objective": "Validate a generic parser boundary.",
            "base_ref": "HEAD",
            "read_paths": ["src/**"],
            "write_paths": ["src/parser.py"],
            "deny_paths": [".env"],
            "worker_id": "local-agent",
            "worker_command": ["python", "worker.py"],
            "timeout_seconds": 1800,
            "required_checks": [
                {"name": "unit-tests", "command": ["python", "-m", "pytest"]}
            ],
        }
    )


def make_store(tmp_path: Path, clock: FakeClock | None = None) -> tuple[TaskStore, FakeClock]:
    root = tmp_path / "repo"
    root.mkdir()
    test_clock = clock or FakeClock()
    return TaskStore(root, clock=test_clock), test_clock


def run_cli(state_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
    environment["AGENT_WORKTREE_STATE_PATH"] = str(state_path)
    environment["GIT_CONFIG_COUNT"] = "1"
    environment["GIT_CONFIG_KEY_0"] = "safe.directory"
    environment["GIT_CONFIG_VALUE_0"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "donegate", *args],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )


def test_store_initializes_and_reopens(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    record = store.create(valid_definition())
    db_path = store.db_path

    assert db_path.exists()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'repo_root'"
        ).fetchone()[0] == str(store.repo_root)

    reopened = TaskStore(store.repo_root, clock=clock)
    recovered = reopened.get(record.task_id)
    assert recovered.state is TaskState.PENDING
    assert recovered.definition.to_dict() == record.definition.to_dict()
    assert recovered.version == 0


def test_create_persists_definition_and_rejects_duplicate(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    definition = valid_definition("duplicate-task")
    first = store.create(definition)

    with pytest.raises(DuplicateTaskError):
        store.create(definition)

    unchanged = store.get(definition.task_id)
    assert unchanged.state is TaskState.PENDING
    assert unchanged.version == first.version
    assert unchanged.definition.to_dict() == definition.to_dict()


def test_missing_task_is_rejected(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)

    with pytest.raises(TaskNotFoundError):
        store.get("missing-task")


@pytest.mark.parametrize(
    ("prefix", "target"),
    [
        ([], TaskState.ASSIGNED),
        ([], TaskState.BLOCKED),
        ([TaskState.ASSIGNED], TaskState.RUNNING),
        ([TaskState.ASSIGNED], TaskState.BLOCKED),
        ([TaskState.ASSIGNED], TaskState.FAILED),
        ([TaskState.ASSIGNED, TaskState.RUNNING], TaskState.COMPLETED),
        ([TaskState.ASSIGNED, TaskState.RUNNING], TaskState.FAILED),
        ([TaskState.ASSIGNED, TaskState.RUNNING], TaskState.BLOCKED),
        ([TaskState.BLOCKED], TaskState.ASSIGNED),
        ([TaskState.BLOCKED], TaskState.FAILED),
        ([TaskState.BLOCKED], TaskState.CLEANED),
        ([TaskState.ASSIGNED, TaskState.FAILED], TaskState.ASSIGNED),
        ([TaskState.ASSIGNED, TaskState.FAILED], TaskState.CLEANED),
        ([TaskState.ASSIGNED, TaskState.RUNNING, TaskState.COMPLETED], TaskState.CLEANED),
    ],
)
def test_every_legal_transition_is_persisted(
    tmp_path: Path, prefix: list[TaskState], target: TaskState
) -> None:
    store, clock = make_store(tmp_path)
    task_id = f"legal-{target.value}-{len(prefix)}"
    store.create(valid_definition(task_id))
    for state in prefix:
        clock.tick()
        store.transition(task_id, state)

    before = store.get(task_id)
    clock.tick()
    after = store.transition(task_id, target)

    assert after.state is target
    assert after.version == before.version + 1
    assert after.updated_at > before.updated_at
    if target is TaskState.ASSIGNED:
        assert after.assigned_at == after.updated_at
    if target is TaskState.RUNNING:
        assert after.started_at == after.updated_at
    if target in {TaskState.COMPLETED, TaskState.FAILED}:
        assert after.finished_at == after.updated_at
    if target is TaskState.CLEANED:
        assert after.cleaned_at == after.updated_at


@pytest.mark.parametrize(
    ("prefix", "requested"),
    [
        ([], TaskState.COMPLETED),
        ([TaskState.ASSIGNED, TaskState.RUNNING, TaskState.COMPLETED, TaskState.CLEANED], TaskState.ASSIGNED),
        ([TaskState.ASSIGNED, TaskState.RUNNING, TaskState.COMPLETED], TaskState.RUNNING),
        ([TaskState.ASSIGNED, TaskState.RUNNING], TaskState.PENDING),
    ],
)
def test_illegal_transition_is_atomic(
    tmp_path: Path, prefix: list[TaskState], requested: TaskState
) -> None:
    store, clock = make_store(tmp_path)
    task_id = f"illegal-{requested.value}-{len(prefix)}"
    store.create(valid_definition(task_id))
    for state in prefix:
        clock.tick()
        store.transition(task_id, state)
    before = store.get(task_id)

    clock.tick()
    with pytest.raises(InvalidTaskTransition) as error:
        store.transition(task_id, requested)

    after = store.get(task_id)
    assert error.value.task_id == task_id
    assert error.value.current_state == before.state
    assert error.value.requested_state == requested
    assert after.state is before.state
    assert after.version == before.version
    assert after.updated_at == before.updated_at


def test_cleaned_is_terminal_and_unknown_requested_state_fails(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    task_id = "terminal-task"
    store.create(valid_definition(task_id))
    store.transition(task_id, TaskState.ASSIGNED)
    store.transition(task_id, TaskState.RUNNING)
    store.transition(task_id, TaskState.COMPLETED)
    store.transition(task_id, TaskState.CLEANED)
    before = store.get(task_id)

    with pytest.raises(InvalidTaskTransition):
        store.transition(task_id, TaskState.ASSIGNED)
    with pytest.raises(InvalidTaskTransition):
        store.transition(task_id, "not-a-state")

    after = store.get(task_id)
    assert after.state is TaskState.CLEANED
    assert after.version == before.version


def test_restart_recovers_state_definition_timestamps_and_version(tmp_path: Path) -> None:
    clock = FakeClock()
    store, _ = make_store(tmp_path, clock)
    definition = valid_definition("restart-task")
    created = store.create(definition)
    clock.tick()
    assigned = store.transition(created.task_id, TaskState.ASSIGNED)
    clock.tick()
    running = store.transition(created.task_id, TaskState.RUNNING)

    reopened = TaskStore(store.repo_root, clock=clock)
    recovered = reopened.get(created.task_id)

    assert recovered.state is TaskState.RUNNING
    assert recovered.definition.to_dict() == definition.to_dict()
    assert recovered.created_at == created.created_at
    assert recovered.assigned_at == assigned.assigned_at
    assert recovered.started_at == running.started_at
    assert recovered.updated_at == running.updated_at
    assert recovered.version == 2


def test_timestamps_are_utc_and_metadata_update_is_limited(tmp_path: Path) -> None:
    store, clock = make_store(tmp_path)
    created = store.create(valid_definition("metadata-task"))
    assert created.created_at.tzinfo is not None
    assert created.created_at.utcoffset() == timedelta(0)

    clock.tick()
    updated = store.update_task_runtime_metadata(
        created.task_id,
        worktree_path=".agent-worktree/worktrees/metadata-task",
        branch_name="donegate/metadata-task",
        base_commit="a" * 40,
    )

    assert updated.state is TaskState.PENDING
    assert updated.worktree_path == ".agent-worktree/worktrees/metadata-task"
    assert updated.branch_name == "donegate/metadata-task"
    assert updated.base_commit == "a" * 40
    assert updated.version == 1
    assert updated.updated_at > created.updated_at


def test_cli_create_and_status_json_use_real_state_store(tmp_path: Path) -> None:
    db_path = tmp_path / "cli-state.sqlite3"
    created = run_cli(db_path, "task", "create", "--file", "examples/task.yaml")
    assert created.returncode == 0, created.stderr
    created_payload = json.loads(created.stdout)
    assert created_payload["persisted"] is True
    assert created_payload["task"]["state"] == "pending"

    status = run_cli(db_path, "task", "status", "--task", "parser-validation", "--json")
    assert status.returncode == 0, status.stderr
    payload = json.loads(status.stdout)
    assert payload["task_id"] == "parser-validation"
    assert payload["state"] == "pending"
    assert payload["definition"]["read_paths"] == ["src/**", "tests/**"]

    human = run_cli(db_path, "task", "status", "--task", "parser-validation")
    assert human.returncode == 0, human.stderr
    assert "STATE: pending" in human.stdout


def test_cli_missing_task_is_nonzero(tmp_path: Path) -> None:
    result = run_cli(tmp_path / "missing.sqlite3", "task", "status", "--task", "missing")

    assert result.returncode != 0
    assert "TaskNotFound" in result.stderr


def test_corrupt_state_fails_closed(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    task_id = "corrupt-task"
    store.create(valid_definition(task_id))
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("UPDATE tasks SET state = ? WHERE task_id = ?", ("corrupt", task_id))

    reopened = TaskStore(store.repo_root)
    with pytest.raises(StateCorruptionError):
        reopened.get(task_id)


def test_unknown_database_schema_version_fails_closed(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("PRAGMA user_version = 999")

    with pytest.raises(UnsupportedSchemaVersionError):
        TaskStore(store.repo_root)


def test_database_binding_rejects_another_repository(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    other_root = tmp_path / "other-repo"
    other_root.mkdir()

    with pytest.raises(RepositoryBindingError):
        TaskStore(other_root, db_path=store.db_path)
