from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from donegate.evidence import (
    create_execution_artifact,
    read_execution_metadata,
    read_recovery_report,
)
from donegate.leases import LeaseManager
from donegate.models import (
    ExecutionResult,
    ExecutionStatus,
    TaskState,
    ValidationReport,
    ValidationStatus,
)
from donegate.recovery import (
    ActiveValidationCleanupError,
    BranchCleanupPendingError,
    CleanupOrchestrator,
    DirtyWorktreeCleanupError,
    InvalidCleanupStateError,
    RecoveryOrchestrator,
    RunningExecutionCleanupError,
)
from donegate.state import TaskStore

from test_worker import make_repo, prepare_task, python_command, run_git, task_definition


def _terminal_task(
    tmp_path: Path,
    task_id: str,
    *,
    state: TaskState = TaskState.FAILED,
    write_paths: list[str] | None = None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    root, repository, store, worktree = prepare_task(
        tmp_path, task_id, python_command("pass"), write_paths=write_paths
    )
    store.transition(task_id, TaskState.RUNNING)
    if state is not TaskState.RUNNING:
        store.transition(task_id, state, reason="test fixture" if state is TaskState.FAILED else None)
    return root, repository, store, worktree


def _running_execution(
    root: Path,
    store: TaskStore,
    task_id: str,
    worktree: Path,
    execution_id: str,
    pid: int,
) -> ExecutionResult:
    now = datetime.now(timezone.utc)
    artifact = create_execution_artifact(root, execution_id)
    created = ExecutionResult(
        execution_id=execution_id,
        task_id=task_id,
        worker_id="test-worker",
        status=ExecutionStatus.CREATED,
        command=(sys.executable, "-c", "pass"),
        worktree_path=str(worktree),
        pid=None,
        created_at=now,
        started_at=None,
        finished_at=None,
        duration_seconds=None,
        exit_code=None,
        timeout_seconds=30,
        artifact_dir=str(artifact.directory),
        stdout_path=str(artifact.stdout_path),
        stderr_path=str(artifact.stderr_path),
    )
    store.create_execution(created)
    return store.start_execution(execution_id, pid=pid, started_at=now)


def _finding_codes(report) -> set[str]:
    return {finding.code for finding in report.findings}


def test_cleanup_completed_releases_lease_removes_worktree_and_preserves_branch(
    tmp_path: Path,
) -> None:
    root, repository, store, worktree = _terminal_task(
        tmp_path, "completed-clean", state=TaskState.COMPLETED, write_paths=["src/**"]
    )
    branch = store.get("completed-clean").branch_name
    lease = LeaseManager(store).list_task("completed-clean")[0]

    result = CleanupOrchestrator(store, repository=repository).cleanup("completed-clean")

    assert result.result == "CLEANED"
    assert store.get("completed-clean").state is TaskState.CLEANED
    assert not worktree.exists()
    assert repository.branch_exists(branch)
    assert LeaseManager(store).get(lease.lease_id).status.value == "released"
    assert list((root / ".agent-worktree" / "executions").glob("**/*")) == []


def test_cleanup_failed_and_blocked_are_allowed(tmp_path: Path) -> None:
    _, repository, store, worktree = _terminal_task(tmp_path / "failed", "failed-clean")
    CleanupOrchestrator(store, repository=repository).cleanup("failed-clean")
    assert store.get("failed-clean").state is TaskState.CLEANED
    assert not worktree.exists()

    _, repository, store, worktree = _terminal_task(
        tmp_path / "blocked", "blocked-clean", state=TaskState.BLOCKED
    )
    CleanupOrchestrator(store, repository=repository).cleanup("blocked-clean")
    assert store.get("blocked-clean").state is TaskState.CLEANED
    assert not worktree.exists()


def test_cleanup_rejects_running_dirty_running_execution_and_active_validation(
    tmp_path: Path,
) -> None:
    for name in ("running", "dirty", "execution", "validation"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    root, repository, store, worktree = prepare_task(
        tmp_path / "running", "running-clean", python_command("pass")
    )
    store.transition("running-clean", TaskState.RUNNING)
    with pytest.raises(InvalidCleanupStateError):
        CleanupOrchestrator(store, repository=repository).cleanup("running-clean")

    root, repository, store, worktree = _terminal_task(tmp_path / "dirty", "dirty-clean")
    (worktree / "untracked.txt").write_text("preserve\n", encoding="utf-8")
    with pytest.raises(DirtyWorktreeCleanupError):
        CleanupOrchestrator(store, repository=repository).cleanup("dirty-clean")
    assert (worktree / "untracked.txt").read_text(encoding="utf-8") == "preserve\n"

    root, repository, store, worktree = _terminal_task(tmp_path / "execution", "execution-clean")
    _running_execution(root, store, "execution-clean", worktree, "running-execution", os.getpid())
    with pytest.raises(RunningExecutionCleanupError):
        CleanupOrchestrator(store, repository=repository).cleanup("execution-clean")

    root, repository, store, worktree = _terminal_task(tmp_path / "validation", "validation-clean")
    now = datetime.now(timezone.utc)
    store.begin_validation(
        ValidationReport(
            validation_id="active-validation",
            task_id="validation-clean",
            execution_id=None,
            worker_id="test-worker",
            status=ValidationStatus.RUNNING,
            started_at=now,
            finished_at=now,
            expected_branch=None,
            actual_branch=None,
            base_commit=None,
            reported_commit=None,
            verified_commit=None,
            actual_changed_files=(),
            allowlist_violations=(),
            denylist_violations=(),
            required_check_results=(),
            execution_status=None,
            lease_valid=None,
            blocking_reasons=(),
            artifact_dir=str(root / ".agent-worktree" / "validations" / "active-validation"),
        )
    )
    with pytest.raises(ActiveValidationCleanupError):
        CleanupOrchestrator(store, repository=repository).cleanup("validation-clean")
    assert worktree.exists()


def test_cleanup_idempotency_and_safe_branch_delete(tmp_path: Path) -> None:
    _, repository, store, worktree = _terminal_task(
        tmp_path / "branch", "branch-clean", state=TaskState.COMPLETED
    )
    branch = store.get("branch-clean").branch_name
    result = CleanupOrchestrator(store, repository=repository).cleanup(
        "branch-clean", remove_branch=True
    )
    assert result.result == "CLEANED"
    assert not repository.branch_exists(branch)
    again = CleanupOrchestrator(store, repository=repository).cleanup(
        "branch-clean", remove_branch=True
    )
    assert again.result == "ALREADY_CLEANED"
    assert not worktree.exists()


def test_branch_delete_failure_keeps_task_out_of_cleaned_state(tmp_path: Path) -> None:
    root, repository, store, worktree = prepare_task(
        tmp_path, "branch-failure", python_command("pass"), write_paths=["src/**"]
    )
    store.transition("branch-failure", TaskState.RUNNING)
    (worktree / "src").mkdir()
    (worktree / "src" / "change.txt").write_text("change\n", encoding="utf-8")
    run_git(worktree, "add", "src/change.txt")
    run_git(worktree, "commit", "-m", "unmerged worker result")
    store.transition("branch-failure", TaskState.COMPLETED)

    with pytest.raises(BranchCleanupPendingError):
        CleanupOrchestrator(store, repository=repository).cleanup(
            "branch-failure", remove_branch=True
        )
    assert store.get("branch-failure").state is TaskState.COMPLETED
    assert not worktree.exists()
    assert repository.branch_exists("donegate/branch-failure")


def test_recovery_dry_run_is_non_destructive_and_persists_report(tmp_path: Path) -> None:
    root, repository, store, worktree = _terminal_task(
        tmp_path, "stale-dry", state=TaskState.RUNNING
    )
    # The helper intentionally leaves the running task without an execution;
    # this is an audit finding, not a reason to mutate it during dry-run.
    now = datetime.now(timezone.utc)
    lease_manager = LeaseManager(store, clock=lambda: now)
    lease_manager.acquire("stale-dry", "test-worker", ["src/**"], ttl_seconds=1)
    orphan = repository.create_worktree("orphan-dry").worktree_path
    (repository.worktree_root / "unregistered").mkdir(parents=True)
    before_state = store.get("stale-dry")
    before_worktrees = repository.worktrees()
    before_lease = lease_manager.list_task("stale-dry")[-1]

    report = RecoveryOrchestrator(
        store,
        repository=repository,
        lease_manager=LeaseManager(store, clock=lambda: now + timedelta(seconds=2)),
        clock=lambda: now + timedelta(seconds=2),
    ).recover("dry-run")

    assert "stale_lease" in _finding_codes(report)
    assert "orphan_managed_worktree" in _finding_codes(report)
    assert "unregistered_managed_directory" in _finding_codes(report)
    assert store.get("stale-dry") == before_state
    assert repository.worktrees() == before_worktrees
    assert lease_manager.get(before_lease.lease_id).status.value == "active"
    assert orphan.exists()
    persisted = read_recovery_report(root, report.recovery_id)
    assert persisted["recovery_id"] == report.recovery_id
    assert persisted["mode"] == "dry-run"


def test_recovery_stale_lease_apply_blocks_running_task(tmp_path: Path) -> None:
    root, repository, store, worktree = _terminal_task(
        tmp_path, "stale-apply", state=TaskState.RUNNING
    )
    now = datetime.now(timezone.utc)
    lease_manager = LeaseManager(store, clock=lambda: now)
    lease = lease_manager.acquire("stale-apply", "test-worker", ["src/**"], ttl_seconds=1)
    report = RecoveryOrchestrator(
        store,
        repository=repository,
        lease_manager=LeaseManager(store, clock=lambda: now + timedelta(seconds=2)),
        clock=lambda: now + timedelta(seconds=2),
    ).recover("apply")

    assert "stale_lease" in _finding_codes(report)
    assert store.get("stale-apply").state is TaskState.BLOCKED
    assert LeaseManager(store, clock=lambda: now + timedelta(seconds=2)).get(lease.lease_id).status.value == "stale"
    assert worktree.exists()
    assert root.exists()


def test_recovery_orphan_policy_removes_only_clean_managed_orphans(tmp_path: Path) -> None:
    _, repository, store = make_repo(tmp_path)
    clean = repository.create_worktree("orphan-clean").worktree_path
    dirty = repository.create_worktree("orphan-dirty").worktree_path
    (dirty / "keep.txt").write_text("keep\n", encoding="utf-8")
    foreign = tmp_path / "foreign-worktree"
    run_git(tmp_path / "project", "worktree", "add", "-b", "foreign-branch", str(foreign), "HEAD")
    ghost = repository.worktree_root / "ghost"
    ghost.mkdir(parents=True)
    (ghost / "keep.txt").write_text("keep\n", encoding="utf-8")

    report = RecoveryOrchestrator(store, repository=repository).recover("apply")

    assert "orphan_managed_worktree" in _finding_codes(report)
    assert "dirty_orphan_worktree" in _finding_codes(report)
    assert "foreign_worktree" in _finding_codes(report)
    assert "unregistered_managed_directory" in _finding_codes(report)
    assert not clean.exists()
    assert dirty.exists()
    assert foreign.exists()
    assert ghost.exists()


def test_recovery_missing_process_marks_execution_failed_and_task_blocked(tmp_path: Path) -> None:
    root, repository, store, worktree = prepare_task(
        tmp_path, "missing-process", python_command("pass")
    )
    store.transition("missing-process", TaskState.RUNNING)
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=10)
    execution = _running_execution(
        root, store, "missing-process", worktree, "missing-process-execution", process.pid
    )
    dry = RecoveryOrchestrator(store, repository=repository).recover("dry-run")
    assert "running_execution_process_missing" in _finding_codes(dry)
    assert store.get_execution(execution.execution_id).status is ExecutionStatus.RUNNING
    assert store.get("missing-process").state is TaskState.RUNNING

    applied = RecoveryOrchestrator(store, repository=repository).recover("apply")

    assert "running_execution_process_missing" in _finding_codes(applied)
    assert store.get_execution(execution.execution_id).status is ExecutionStatus.FAILED
    assert store.get("missing-process").state is TaskState.BLOCKED
    assert read_execution_metadata(root, execution.execution_id)["recovery_reason"] == (
        "orchestrator_restart_process_missing"
    )
    assert worktree.exists()


def test_recovery_does_not_kill_live_or_uncertain_process(tmp_path: Path) -> None:
    root, repository, store, worktree = prepare_task(
        tmp_path, "live-process", python_command("pass")
    )
    store.transition("live-process", TaskState.RUNNING)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        execution = _running_execution(
            root, store, "live-process", worktree, "live-process-execution", process.pid
        )
        report = RecoveryOrchestrator(store, repository=repository).recover("apply")
        assert "running_execution_process_alive" in _finding_codes(report)
        assert process.poll() is None
        assert store.get_execution(execution.execution_id).status is ExecutionStatus.RUNNING
        assert store.get("live-process").state is TaskState.RUNNING
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_recovery_blocks_running_task_with_missing_worktree_or_branch_mismatch(
    tmp_path: Path,
) -> None:
    (tmp_path / "missing").mkdir(parents=True, exist_ok=True)
    (tmp_path / "branch").mkdir(parents=True, exist_ok=True)
    _, repository, store, worktree = prepare_task(
        tmp_path / "missing", "missing-worktree", python_command("pass")
    )
    store.transition("missing-worktree", TaskState.RUNNING)
    repository.remove_worktree(worktree)
    report = RecoveryOrchestrator(store, repository=repository).recover("apply")
    assert "task_worktree_missing" in _finding_codes(report)
    assert store.get("missing-worktree").state is TaskState.BLOCKED

    _, repository, store, worktree = prepare_task(
        tmp_path / "branch", "branch-mismatch", python_command("pass")
    )
    store.transition("branch-mismatch", TaskState.RUNNING)
    store.update_task_runtime_metadata("branch-mismatch", branch_name="donegate/wrong")
    report = RecoveryOrchestrator(store, repository=repository).recover("apply")
    assert "task_branch_mismatch" in _finding_codes(report)
    assert store.get("branch-mismatch").state is TaskState.BLOCKED
    assert worktree.exists()


def test_recovery_preserves_completed_without_validation_and_terminal_resources(tmp_path: Path) -> None:
    root, repository, store, worktree = _terminal_task(
        tmp_path, "completed-no-validation", state=TaskState.COMPLETED
    )
    execution = _running_execution(
        root,
        store,
        "completed-no-validation",
        worktree,
        "terminal-execution",
        os.getpid(),
    )
    store.finish_execution(
        execution.execution_id,
        status=ExecutionStatus.FAILED,
        finished_at=datetime.now(timezone.utc),
        exit_code=1,
        duration_seconds=0,
    )
    report = RecoveryOrchestrator(store, repository=repository).recover("apply")
    assert "completed_without_validation" in _finding_codes(report)
    assert "terminal_execution_with_resources" in _finding_codes(report)
    assert store.get("completed-no-validation").state is TaskState.COMPLETED
    assert worktree.exists()
    assert root.exists()


def test_cleanup_and_recover_cli_json_are_wired(tmp_path: Path) -> None:
    root, _, store, _ = _terminal_task(tmp_path, "cli-cleanup")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    cleanup = subprocess.run(
        [
            sys.executable,
            "-m",
            "donegate",
            "task",
            "cleanup",
            "--task",
            "cli-cleanup",
            "--json",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert cleanup.returncode == 0, cleanup.stderr
    assert json.loads(cleanup.stdout)["result"] == "CLEANED"
    assert TaskStore(root).get("cli-cleanup").state is TaskState.CLEANED

    recovery = subprocess.run(
        [sys.executable, "-m", "donegate", "recover", "--dry-run", "--json"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert recovery.returncode == 0, recovery.stderr
    payload = json.loads(recovery.stdout)
    assert payload["mode"] == "dry-run"
    assert Path(payload["artifact_dir"]).is_dir()
