from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_worktree.evidence import (
    ArtifactAlreadyExistsError,
    UnsafeArtifactPathError,
    artifact_paths,
    create_execution_artifact,
    read_execution_metadata,
    write_execution_metadata,
)
from agent_worktree.git import GitRepository
from agent_worktree.leases import LeaseManager
from agent_worktree.models import (
    ExecutionResult,
    ExecutionStatus,
    TaskDefinition,
    TaskState,
)
from agent_worktree.state import (
    DB_SCHEMA_VERSION,
    InvalidExecutionTransition,
    StateStoreError,
    TaskStore,
    UnsupportedSchemaVersionError,
)
from agent_worktree.worker import (
    AlreadyFinishedError,
    WorkerCommandError,
    WorkerPreconditionError,
    WorkerProcess,
    WorkerStartError,
    WorkerWaitTimeout,
    redact_command_for_storage,
)


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git failed: {args}\n{result.stderr}")
    return result


def make_repo(tmp_path: Path) -> tuple[Path, GitRepository, TaskStore]:
    root = tmp_path / "project"
    root.mkdir()
    run_git(root, "init", "-b", "main")
    run_git(root, "config", "user.name", "agent-worktree worker tests")
    run_git(root, "config", "user.email", "worker-tests@local.invalid")
    (root / ".gitignore").write_text(".agent-worktree/\n", encoding="utf-8")
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    run_git(root, "add", ".")
    run_git(root, "commit", "-m", "initial test commit")
    repository = GitRepository(root)
    return root, repository, TaskStore(root)


def task_definition(
    task_id: str,
    command: list[str],
    *,
    write_paths: list[str] | None = None,
    timeout_seconds: int = 10,
) -> TaskDefinition:
    return TaskDefinition.from_mapping(
        {
            "schema_version": "0.1",
            "task_id": task_id,
            "objective": "Run a provider-neutral worker process.",
            "base_ref": "HEAD",
            "read_paths": ["src/**"],
            "write_paths": write_paths or [],
            "deny_paths": [".env"],
            "worker_id": "test-worker",
            "worker_command": command,
            "timeout_seconds": timeout_seconds,
            "required_checks": [],
        }
    )


def prepare_task(
    tmp_path: Path,
    task_id: str,
    command: list[str],
    *,
    write_paths: list[str] | None = None,
    timeout_seconds: int = 10,
) -> tuple[Path, GitRepository, TaskStore, Path]:
    root, repository, store = make_repo(tmp_path)
    definition = task_definition(
        task_id, command, write_paths=write_paths, timeout_seconds=timeout_seconds
    )
    store.create(definition)
    created = repository.create_worktree(task_id)
    store.update_task_runtime_metadata(
        task_id,
        worktree_path=str(created.worktree_path),
        branch_name=created.branch,
        base_commit=created.base_commit,
        head_commit=created.head_commit,
    )
    store.transition(task_id, TaskState.ASSIGNED)
    if write_paths:
        LeaseManager(store).acquire(
            task_id, "test-worker", write_paths, ttl_seconds=60
        )
    return root, repository, store, created.worktree_path


def python_command(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def finish_worktree(repository: GitRepository, worktree: Path) -> None:
    repository.remove_worktree(worktree)


def test_execution_status_model_and_terminal_transition(tmp_path: Path) -> None:
    root, _, store = make_repo(tmp_path)
    now = datetime(2026, 8, 21, 16, 0, tzinfo=timezone.utc)
    definition = task_definition("model-task", python_command("pass"))
    store.create(definition)
    execution = ExecutionResult(
        execution_id="execution-model",
        task_id="model-task",
        worker_id="test-worker",
        status=ExecutionStatus.CREATED,
        command=("python", "-c", "pass"),
        worktree_path=str(root),
        pid=None,
        created_at=now,
        started_at=None,
        finished_at=None,
        duration_seconds=None,
        exit_code=None,
        timeout_seconds=10,
        artifact_dir=str(root / ".agent-worktree" / "executions" / "execution-model"),
        stdout_path=str(root / "stdout.log"),
        stderr_path=str(root / "stderr.log"),
    )
    store.create_execution(execution)
    store.start_execution("execution-model", pid=123, started_at=now)
    result = store.finish_execution(
        "execution-model",
        status=ExecutionStatus.SUCCEEDED,
        finished_at=now,
        exit_code=0,
        duration_seconds=0,
    )
    assert result.is_terminal
    with pytest.raises(InvalidExecutionTransition):
        store.finish_execution(
            "execution-model",
            status=ExecutionStatus.FAILED,
            finished_at=now,
            exit_code=1,
            duration_seconds=0,
        )


def test_command_validation_and_secret_redaction() -> None:
    assert redact_command_for_storage(
        ["agent", "--token", "abc", "--api-key=xyz", "--password", "pw", "ok"]
    ) == ("agent", "--token", "<redacted>", "--api-key=<redacted>", "--password", "<redacted>", "ok")
    assert redact_command_for_storage(["agent", "Authorization", "Bearer secret"])[2] == "<redacted>"
    with pytest.raises(WorkerCommandError):
        redact_command_for_storage(["agent", "\x00bad"])


def test_artifacts_are_safe_atomic_and_restart_readable(tmp_path: Path) -> None:
    artifact = create_execution_artifact(tmp_path, "execution-1")
    write_execution_metadata(artifact, {"status": "running", "secret": "not-process-output"})
    assert read_execution_metadata(artifact)["status"] == "running"
    assert artifact.stdout_path.is_file()
    assert artifact.stderr_path.is_file()
    with pytest.raises(ArtifactAlreadyExistsError):
        create_execution_artifact(tmp_path, "execution-1")
    with pytest.raises(UnsafeArtifactPathError):
        artifact_paths(tmp_path, "../escape")


def test_success_captures_separate_output_and_does_not_complete_task(tmp_path: Path) -> None:
    command = python_command("import os; print('out', flush=True); print('err', file=__import__('sys').stderr, flush=True); print(os.getcwd(), flush=True)")
    root, repository, store, worktree = prepare_task(tmp_path, "success", command)
    worker = WorkerProcess(store, repository=repository, termination_grace_seconds=0.2)
    handle = worker.start("success")
    result = worker.wait(handle)

    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.exit_code == 0
    assert store.get("success").state is TaskState.RUNNING
    stdout = Path(result.stdout_path).read_bytes()
    stderr = Path(result.stderr_path).read_bytes()
    assert b"out" in stdout
    assert b"err" in stderr
    assert b".agent-worktree\\worktrees\\success" in stdout
    assert read_execution_metadata(root, result.execution_id)["status"] == "succeeded"
    finish_worktree(repository, worktree)


def test_output_callbacks_stream_before_process_exit_and_preserve_artifacts(
    tmp_path: Path,
) -> None:
    command = python_command(
        "import sys,time; "
        "print('STEP_ONE', flush=True); "
        "print('ERR_ONE', file=sys.stderr, flush=True); "
        "time.sleep(0.8); "
        "print('STEP_TWO', flush=True); "
        "print('ERR_TWO', file=sys.stderr, flush=True)"
    )
    _, repository, store, worktree = prepare_task(tmp_path, "live-output", command)
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    first_stdout = threading.Event()
    first_stderr = threading.Event()

    def on_stdout(text: str) -> None:
        stdout_chunks.append(text)
        first_stdout.set()

    def on_stderr(text: str) -> None:
        stderr_chunks.append(text)
        first_stderr.set()

    worker = WorkerProcess(store, repository=repository)
    handle = worker.start(
        "live-output",
        on_stdout=on_stdout,
        on_stderr=on_stderr,
    )
    assert first_stdout.wait(2)
    assert first_stderr.wait(2)
    assert handle.process.poll() is None

    result = worker.wait(handle)
    assert result.status is ExecutionStatus.SUCCEEDED
    assert "STEP_ONE" in "".join(stdout_chunks)
    assert "STEP_TWO" in "".join(stdout_chunks)
    assert "ERR_ONE" in "".join(stderr_chunks)
    assert "ERR_TWO" in "".join(stderr_chunks)
    assert "ERR_ONE" not in "".join(stdout_chunks)
    assert "STEP_ONE" not in "".join(stderr_chunks)
    assert "STEP_ONE" in Path(result.stdout_path).read_text(encoding="utf-8")
    assert "STEP_TWO" in Path(result.stdout_path).read_text(encoding="utf-8")
    assert "ERR_ONE" in Path(result.stderr_path).read_text(encoding="utf-8")
    assert "ERR_TWO" in Path(result.stderr_path).read_text(encoding="utf-8")
    finish_worktree(repository, worktree)


def test_output_callback_failure_does_not_interrupt_artifact_capture(tmp_path: Path) -> None:
    command = python_command(
        "print('BEFORE', flush=True); "
        "print('AFTER', flush=True); "
        "print('x'*100000, flush=True)"
    )
    _, repository, store, worktree = prepare_task(tmp_path, "broken-sink", command)

    def broken_sink(_: str) -> None:
        raise BrokenPipeError("terminal closed")

    worker = WorkerProcess(store, repository=repository)
    result = worker.wait(worker.start("broken-sink", on_stdout=broken_sink))
    assert result.status is ExecutionStatus.SUCCEEDED
    stdout = Path(result.stdout_path).read_text(encoding="utf-8")
    assert "BEFORE" in stdout
    assert "AFTER" in stdout
    assert len(stdout) >= 100000
    finish_worktree(repository, worktree)


def test_failed_worker_updates_task_failed_and_captures_stderr(tmp_path: Path) -> None:
    command = python_command("import sys; print('failure', file=sys.stderr, flush=True); raise SystemExit(7)")
    root, repository, store, worktree = prepare_task(tmp_path, "failure", command)
    worker = WorkerProcess(store, repository=repository)
    result = worker.wait(worker.start("failure"))
    assert result.status is ExecutionStatus.FAILED
    assert result.exit_code == 7
    assert "failure" in Path(result.stderr_path).read_text(encoding="utf-8")
    assert store.get("failure").state is TaskState.FAILED
    finish_worktree(repository, worktree)


def test_missing_executable_is_persisted_failed_without_running_leak(tmp_path: Path) -> None:
    command = ["agent-worktree-command-that-does-not-exist", "--token", "secret"]
    root, repository, store, worktree = prepare_task(tmp_path, "missing", command)
    worker = WorkerProcess(store, repository=repository)
    with pytest.raises(WorkerStartError) as error:
        worker.start("missing")
    result = store.get_execution(error.value.execution_id)
    assert result.status is ExecutionStatus.FAILED
    assert store.find_incomplete_executions() == ()
    assert store.get("missing").state is TaskState.FAILED
    metadata = read_execution_metadata(root, result.execution_id)
    assert metadata["command"][2] == "<redacted>"
    finish_worktree(repository, worktree)


def test_timeout_terminates_process_group_and_marks_task_failed(tmp_path: Path) -> None:
    marker = tmp_path / "child-survived.txt"
    child_code = f"import time; time.sleep(30); open({str(marker)!r}, 'w').write('survived')"
    command = python_command(
        f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child_code!r}]); time.sleep(30)"
    )
    _, repository, store, worktree = prepare_task(
        tmp_path, "timeout", command, timeout_seconds=1
    )
    worker = WorkerProcess(store, repository=repository, termination_grace_seconds=0.1)
    result = worker.wait(worker.start("timeout"))
    assert result.status is ExecutionStatus.TIMED_OUT
    assert store.get("timeout").state is TaskState.FAILED
    time.sleep(0.4)
    assert not marker.exists()
    finish_worktree(repository, worktree)


def test_cancel_marks_task_blocked_and_repeated_cancel_is_explicit(tmp_path: Path) -> None:
    command = python_command("import time; print('ready', flush=True); time.sleep(30)")
    _, repository, store, worktree = prepare_task(tmp_path, "cancel", command)
    worker = WorkerProcess(store, repository=repository, termination_grace_seconds=0.1)
    handle = worker.start("cancel")
    time.sleep(0.1)
    result = worker.cancel(handle)
    assert result.status is ExecutionStatus.CANCELLED
    assert store.get("cancel").state is TaskState.BLOCKED
    with pytest.raises(AlreadyFinishedError):
        worker.cancel(handle)
    finish_worktree(repository, worktree)


def test_wait_timeout_does_not_cancel_worker(tmp_path: Path) -> None:
    command = python_command("import time; time.sleep(1)")
    _, repository, store, worktree = prepare_task(tmp_path, "wait-limit", command)
    worker = WorkerProcess(store, repository=repository)
    handle = worker.start("wait-limit")
    with pytest.raises(WorkerWaitTimeout):
        worker.wait(handle, timeout=0.01)
    assert worker.cancel(handle).status is ExecutionStatus.CANCELLED
    finish_worktree(repository, worktree)


def test_large_stdout_and_stderr_do_not_deadlock(tmp_path: Path) -> None:
    command = python_command(
        "import sys; sys.stdout.write('o'*1048576); sys.stdout.flush(); sys.stderr.write('e'*1048576); sys.stderr.flush()"
    )
    stdout_size = [0]
    stderr_size = [0]

    def count_stdout(text: str) -> None:
        stdout_size[0] += len(text)

    def count_stderr(text: str) -> None:
        stderr_size[0] += len(text)

    _, repository, store, worktree = prepare_task(tmp_path, "large-output", command)
    worker = WorkerProcess(store, repository=repository)
    result = worker.wait(
        worker.start(
            "large-output",
            on_stdout=count_stdout,
            on_stderr=count_stderr,
        )
    )
    assert result.status is ExecutionStatus.SUCCEEDED
    assert Path(result.stdout_path).stat().st_size >= 1024 * 1024
    assert Path(result.stderr_path).stat().st_size >= 1024 * 1024
    assert stdout_size[0] >= 1024 * 1024
    assert stderr_size[0] >= 1024 * 1024
    finish_worktree(repository, worktree)


def test_restart_reads_completed_and_discovers_running_execution(tmp_path: Path) -> None:
    command = python_command("import time; time.sleep(2)")
    root, repository, store, worktree = prepare_task(tmp_path, "restart", command)
    worker = WorkerProcess(store, repository=repository)
    completed = worker.wait(worker.start("restart"))
    reopened = TaskStore(root)
    assert reopened.get_execution(completed.execution_id).status is ExecutionStatus.SUCCEEDED

    second_task = task_definition("incomplete", command)
    reopened.create(second_task)
    second_worktree = repository.create_worktree("incomplete")
    reopened.update_task_runtime_metadata(
        "incomplete",
        worktree_path=str(second_worktree.worktree_path),
        branch_name=second_worktree.branch,
        base_commit=second_worktree.base_commit,
        head_commit=second_worktree.head_commit,
    )
    reopened.transition("incomplete", TaskState.ASSIGNED)
    second_worker = WorkerProcess(reopened, repository=repository)
    handle = second_worker.start("incomplete")
    after_restart = TaskStore(root)
    assert [item.execution_id for item in after_restart.find_incomplete_executions()] == [
        handle.execution_id
    ]
    second_worker.cancel(handle)
    finish_worktree(repository, worktree)
    finish_worktree(repository, second_worktree.worktree_path)


def test_worker_requires_registered_worktree_and_write_lease(tmp_path: Path) -> None:
    root, repository, store = make_repo(tmp_path)
    definition = task_definition("precondition", python_command("pass"), write_paths=["a.py"])
    store.create(definition)
    store.transition("precondition", TaskState.ASSIGNED)
    worker = WorkerProcess(store, repository=repository)
    with pytest.raises(WorkerPreconditionError):
        worker.start("precondition")
    created = repository.create_worktree("precondition")
    store.update_task_runtime_metadata(
        "precondition",
        worktree_path=str(created.worktree_path),
        branch_name=created.branch,
    )
    with pytest.raises(WorkerPreconditionError):
        worker.start("precondition")
    finish_worktree(repository, created.worktree_path)


def test_v2_to_v3_migration_preserves_tasks_and_lease_history(tmp_path: Path) -> None:
    root, repository, store = make_repo(tmp_path)
    definition = task_definition("migration", python_command("pass"), write_paths=["a.py"])
    store.create(definition)
    store.transition("migration", TaskState.ASSIGNED)
    lease = LeaseManager(store).acquire("migration", "test-worker", ["a.py"], ttl_seconds=60)
    LeaseManager(store).release(lease.lease_id, "migration", "test-worker", lease.generation)
    created = repository.create_worktree("migration")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DROP TABLE executions")
        connection.execute("PRAGMA user_version = 2")

    migrated = TaskStore(root)
    assert DB_SCHEMA_VERSION == 4
    assert migrated.get("migration").state is TaskState.ASSIGNED
    assert migrated.list_executions() == ()
    assert migrated.db_path.exists()
    with sqlite3.connect(migrated.db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT status FROM leases").fetchone()[0] == "released"
    finish_worktree(repository, created.worktree_path)


def test_unknown_future_schema_still_fails_closed(tmp_path: Path) -> None:
    root, _, store = make_repo(tmp_path)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("PRAGMA user_version = 999")
    with pytest.raises(UnsupportedSchemaVersionError):
        TaskStore(root)


def test_failed_v2_to_v3_migration_rolls_back(tmp_path: Path) -> None:
    root, _, store = make_repo(tmp_path)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DROP TABLE executions")
        connection.execute("CREATE VIEW executions AS SELECT 1 AS invalid")
        connection.execute("PRAGMA user_version = 2")

    with pytest.raises(StateStoreError):
        TaskStore(root)
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute(
            "SELECT type FROM sqlite_master WHERE name = 'executions'"
        ).fetchone()[0] == "view"
