from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from agent_worktree.evidence import (
    artifact_paths,
    read_execution_metadata,
    read_validation_report,
    write_execution_metadata,
)
from agent_worktree.git import GitRepository
from agent_worktree.leases import LeaseManager
from agent_worktree.models import (
    CheckStatus,
    ExecutionResult,
    ExecutionStatus,
    RequiredCheck,
    TaskState,
    ValidationStatus,
)
from agent_worktree.state import (
    DB_SCHEMA_VERSION,
    TaskStore,
    UnsupportedSchemaVersionError,
    ValidationAlreadyRunningError,
    utc_now,
)
from agent_worktree.validate import CompletionValidator
from agent_worktree.worker import WorkerProcess

from test_worker import (
    finish_worktree,
    make_repo,
    python_command,
    run_git,
    task_definition,
)


ROOT = Path(__file__).resolve().parents[1]
NO_CLAIM = object()


def commit_file_command(path: str, contents: str) -> list[str]:
    return python_command(
        "from pathlib import Path; import subprocess; "
        f"p=Path({path!r}); p.parent.mkdir(parents=True, exist_ok=True); "
        f"p.write_text({contents!r}, encoding='utf-8'); "
        "subprocess.run(['git','add','-A'], check=True); "
        "subprocess.run(['git','commit','-m','worker result'], check=True)"
    )


def prepare_validation_task(
    tmp_path: Path,
    task_id: str,
    command: list[str],
    *,
    write_paths: list[str] | None = None,
    deny_paths: list[str] | None = None,
    required_checks: tuple[RequiredCheck, ...] = (),
    timeout_seconds: int = 10,
    seed_files: dict[str, str] | None = None,
) -> tuple[Path, GitRepository, TaskStore, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root, repository, store = make_repo(tmp_path)
    for relative_path, contents in (seed_files or {}).items():
        seeded = root / relative_path
        seeded.parent.mkdir(parents=True, exist_ok=True)
        seeded.write_text(contents, encoding="utf-8")
    if seed_files:
        run_git(root, "add", "-A")
        run_git(root, "commit", "-m", "seed validation fixture")
    definition = replace(
        task_definition(
            task_id,
            command,
            write_paths=write_paths,
            timeout_seconds=timeout_seconds,
        ),
        deny_paths=tuple(deny_paths or [".env"]),
        required_checks=required_checks,
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
    if definition.write_paths:
        LeaseManager(store).acquire(
            task_id, definition.worker_id, definition.write_paths, ttl_seconds=60
        )
    return root, repository, store, created.worktree_path


def run_successful_worker(
    root: Path,
    repository: GitRepository,
    store: TaskStore,
    worktree: Path,
    task_id: str,
    *,
    claim: str | object = NO_CLAIM,
) -> ExecutionResult:
    worker = WorkerProcess(store, repository=repository, termination_grace_seconds=0.2)
    result = worker.wait(worker.start(task_id))
    assert result.status is ExecutionStatus.SUCCEEDED
    if claim is not NO_CLAIM:
        payload = read_execution_metadata(root, result.execution_id)
        if claim is True:
            payload["reported_commit"] = repository.head_at(worktree)
        elif isinstance(claim, str):
            payload["reported_commit"] = claim
        write_execution_metadata(artifact_paths(root, result.execution_id), payload)
    return result


def test_worker_success_requires_independent_commit_and_scope_evidence(tmp_path: Path) -> None:
    root, repository, store, worktree = prepare_validation_task(
        tmp_path,
        "valid",
        commit_file_command("src/parser.py", "ok\n"),
        write_paths=["src/**"],
    )
    result = run_successful_worker(
        root, repository, store, worktree, "valid", claim=True
    )
    report = CompletionValidator(store, repository=repository).validate("valid")

    assert report.status is ValidationStatus.PASSED
    assert report.execution_id == result.execution_id
    assert report.verified_commit == repository.head_at(worktree)
    assert report.actual_changed_files == ("src/parser.py",)
    assert store.get("valid").state is TaskState.COMPLETED
    assert read_validation_report(root, report.validation_id)["status"] == "passed"
    reopened = TaskStore(root)
    restored = reopened.get_validation(report.validation_id)
    assert restored.verified_commit == report.verified_commit
    assert restored.actual_changed_files == report.actual_changed_files
    finish_worktree(repository, worktree)


def test_exit_zero_without_commit_is_not_completion(tmp_path: Path) -> None:
    root, repository, store, worktree = prepare_validation_task(
        tmp_path, "no-commit", python_command("pass"), write_paths=["src/**"]
    )
    run_successful_worker(root, repository, store, worktree, "no-commit")
    report = CompletionValidator(store, repository=repository).validate("no-commit")

    assert report.status is ValidationStatus.FAILED
    assert "commit_missing" in report.blocking_reasons
    assert store.get("no-commit").state is TaskState.FAILED
    finish_worktree(repository, worktree)


def test_fake_and_base_commit_claims_fail_closed(tmp_path: Path) -> None:
    root, repository, store, worktree = prepare_validation_task(
        tmp_path, "fake-commit", python_command("pass"), write_paths=["src/**"]
    )
    result = run_successful_worker(
        root, repository, store, worktree, "fake-commit", claim="0" * 40
    )
    fake_report = CompletionValidator(store, repository=repository).validate("fake-commit")
    assert fake_report.status is ValidationStatus.FAILED
    assert "commit_missing" in fake_report.blocking_reasons
    finish_worktree(repository, worktree)

    root, repository, store, worktree = prepare_validation_task(
        tmp_path / "base", "base-commit", python_command("pass"), write_paths=["src/**"]
    )
    result = run_successful_worker(
        root, repository, store, worktree, "base-commit", claim=True
    )
    base_report = CompletionValidator(store, repository=repository).validate("base-commit")
    assert base_report.status is ValidationStatus.FAILED
    assert "no_new_commit" in base_report.blocking_reasons
    assert result.execution_id == base_report.execution_id
    finish_worktree(repository, worktree)


def test_branch_mismatch_and_unrelated_commit_claim_fail_closed(tmp_path: Path) -> None:
    root, repository, store, worktree = prepare_validation_task(
        tmp_path,
        "branch-mismatch",
        commit_file_command("src/branch.py", "ok\n"),
        write_paths=["src/**"],
    )
    run_successful_worker(
        root, repository, store, worktree, "branch-mismatch", claim=True
    )
    store.update_task_runtime_metadata(
        "branch-mismatch", branch_name="agent-worktree/wrong-branch"
    )
    report = CompletionValidator(store, repository=repository).validate("branch-mismatch")
    assert report.status is ValidationStatus.FAILED
    assert "branch_mismatch" in report.blocking_reasons
    finish_worktree(repository, worktree)

    root, repository, store, worktree = prepare_validation_task(
        tmp_path / "unrelated",
        "unrelated-commit",
        python_command("pass"),
        write_paths=["src/**"],
    )
    result = run_successful_worker(
        root, repository, store, worktree, "unrelated-commit"
    )
    tree = run_git(worktree, "rev-parse", "HEAD^{tree}").stdout.strip()
    unrelated = run_git(worktree, "commit-tree", tree, "-m", "unrelated").stdout.strip()
    payload = read_execution_metadata(root, result.execution_id)
    payload["reported_commit"] = unrelated
    write_execution_metadata(artifact_paths(root, result.execution_id), payload)
    report = CompletionValidator(store, repository=repository).validate("unrelated-commit")
    assert report.status is ValidationStatus.FAILED
    assert "commit_not_descendant" in report.blocking_reasons
    finish_worktree(repository, worktree)


def test_dirty_worktree_and_out_of_scope_commit_fail(tmp_path: Path) -> None:
    root, repository, store, worktree = prepare_validation_task(
        tmp_path,
        "dirty",
        python_command(
            "from pathlib import Path; import subprocess; "
            "Path('src/ok.py').parent.mkdir(parents=True, exist_ok=True); "
            "Path('src/ok.py').write_text('committed'); "
            "subprocess.run(['git','add','-A'], check=True); "
            "subprocess.run(['git','commit','-m','dirty'], check=True); "
            "Path('src/dirty.py').write_text('uncommitted')"
        ),
        write_paths=["src/**"],
    )
    run_successful_worker(root, repository, store, worktree, "dirty", claim=True)
    report = CompletionValidator(store, repository=repository).validate("dirty")
    assert report.status is ValidationStatus.FAILED
    assert "dirty_worktree" in report.blocking_reasons
    assert store.get("dirty").state is TaskState.FAILED


def test_out_of_scope_and_denylist_are_independent_gates(tmp_path: Path) -> None:
    root, repository, store, worktree = prepare_validation_task(
        tmp_path,
        "scope",
        python_command(
            "from pathlib import Path; import subprocess; "
            "Path('src/ok.py').parent.mkdir(parents=True, exist_ok=True); "
            "Path('src/ok.py').write_text('ok'); "
            "Path('other.py').write_text('bad'); "
            "subprocess.run(['git','add','-A'], check=True); "
            "subprocess.run(['git','commit','-m','scope'], check=True)"
        ),
        write_paths=["src/**"],
    )
    run_successful_worker(root, repository, store, worktree, "scope", claim=True)
    report = CompletionValidator(store, repository=repository).validate("scope")
    assert report.status is ValidationStatus.FAILED
    assert report.allowlist_violations == ("other.py",)
    assert "write_scope_violation" in report.blocking_reasons
    finish_worktree(repository, worktree)

    root, repository, store, worktree = prepare_validation_task(
        tmp_path / "deny",
        "deny",
        python_command(
            "from pathlib import Path; import subprocess; "
            "Path('src/secret.py').parent.mkdir(parents=True, exist_ok=True); "
            "Path('src/secret.py').write_text('secret'); "
            "subprocess.run(['git','add','-A'], check=True); "
            "subprocess.run(['git','commit','-m','deny'], check=True)"
        ),
        write_paths=["src/**"],
        deny_paths=["src/secret.py"],
    )
    run_successful_worker(root, repository, store, worktree, "deny", claim=True)
    report = CompletionValidator(store, repository=repository).validate("deny")
    assert report.status is ValidationStatus.FAILED
    assert report.denylist_violations == ("src/secret.py",)
    assert "denylist_violation" in report.blocking_reasons
    finish_worktree(repository, worktree)


def test_directory_allowlist_is_segment_aware_and_read_only_is_supported(
    tmp_path: Path,
) -> None:
    root, repository, store, worktree = prepare_validation_task(
        tmp_path,
        "sibling",
        commit_file_command("src/parser_extra/file.py", "bad\n"),
        write_paths=["src/parser/**"],
    )
    run_successful_worker(root, repository, store, worktree, "sibling", claim=True)
    report = CompletionValidator(store, repository=repository).validate("sibling")
    assert report.status is ValidationStatus.FAILED
    assert report.allowlist_violations == ("src/parser_extra/file.py",)
    finish_worktree(repository, worktree)

    root, repository, store, worktree = prepare_validation_task(
        tmp_path / "readonly", "readonly", python_command("pass")
    )
    run_successful_worker(root, repository, store, worktree, "readonly")
    report = CompletionValidator(store, repository=repository).validate("readonly")
    assert report.status is ValidationStatus.PASSED
    assert report.reported_commit is None
    assert store.get("readonly").state is TaskState.COMPLETED
    finish_worktree(repository, worktree)

    root, repository, store, worktree = prepare_validation_task(
        tmp_path / "readonly-commit",
        "readonly-commit",
        python_command(
            "import subprocess; "
            "subprocess.run(['git','commit','--allow-empty','-m','read-only'], check=True)"
        ),
    )
    run_successful_worker(root, repository, store, worktree, "readonly-commit")
    report = CompletionValidator(store, repository=repository).validate("readonly-commit")
    assert report.status is ValidationStatus.FAILED
    assert "read_only_changed" in report.blocking_reasons
    finish_worktree(repository, worktree)


def test_rename_to_denied_path_reports_both_committed_paths(tmp_path: Path) -> None:
    root, repository, store, worktree = prepare_validation_task(
        tmp_path,
        "rename",
        python_command(
            "from pathlib import Path; import subprocess; "
            "Path('src/allowed.py').rename('secrets.py'); "
            "subprocess.run(['git','add','-A'], check=True); "
            "subprocess.run(['git','commit','-m','rename'], check=True)"
        ),
        write_paths=["src/**", "secrets.py"],
        deny_paths=["secrets.py"],
        seed_files={"src/allowed.py": "x"},
    )
    run_successful_worker(root, repository, store, worktree, "rename", claim=True)
    report = CompletionValidator(store, repository=repository).validate("rename")
    assert report.status is ValidationStatus.FAILED
    assert report.actual_changed_files == ("secrets.py", "src/allowed.py")
    assert report.denylist_violations == ("secrets.py",)
    finish_worktree(repository, worktree)


def test_required_checks_all_run_and_persist_safe_artifacts(tmp_path: Path) -> None:
    checks = (
        RequiredCheck("pass", tuple(python_command("print('stdout'); print('stderr', file=__import__('sys').stderr)")), 2),
        RequiredCheck("nonzero", tuple(python_command("raise SystemExit(3)")), 2),
        RequiredCheck("timeout", tuple(python_command("import time; time.sleep(3)")), 1),
        RequiredCheck("missing", ("agent-worktree-check-missing",), 1),
        RequiredCheck("secret", (sys.executable, "-c", "print('ok')", "--token", "secret"), 2),
    )
    root, repository, store, worktree = prepare_validation_task(
        tmp_path,
        "checks",
        commit_file_command("src/checks.py", "ok\n"),
        write_paths=["src/**"],
        required_checks=checks,
    )
    run_successful_worker(root, repository, store, worktree, "checks", claim=True)
    report = CompletionValidator(
        store, repository=repository, check_grace_seconds=0.1
    ).validate("checks")

    statuses = {item.name: item.status for item in report.required_check_results}
    assert report.status is ValidationStatus.FAILED
    assert statuses["pass"] is CheckStatus.PASSED
    assert statuses["nonzero"] is CheckStatus.FAILED
    assert statuses["timeout"] is CheckStatus.TIMED_OUT
    assert statuses["missing"] is CheckStatus.START_FAILED
    assert statuses["secret"] is CheckStatus.PASSED
    secret_result = next(item for item in report.required_check_results if item.name == "secret")
    assert "secret" not in secret_result.command
    assert "<redacted>" in secret_result.command
    first = next(item for item in report.required_check_results if item.name == "pass")
    assert "stdout" in Path(first.stdout_path).read_text(encoding="utf-8")
    assert "stderr" in Path(first.stderr_path).read_text(encoding="utf-8")
    assert store.get("checks").state is TaskState.FAILED
    finish_worktree(repository, worktree)


def test_lease_is_required_for_writable_validation(tmp_path: Path) -> None:
    root, repository, store, worktree = prepare_validation_task(
        tmp_path,
        "lease",
        commit_file_command("src/lease.py", "ok\n"),
        write_paths=["src/**"],
    )
    result = run_successful_worker(root, repository, store, worktree, "lease", claim=True)
    lease = LeaseManager(store).list_task("lease")[0]
    LeaseManager(store).release(lease.lease_id, "lease", "test-worker", lease.generation)
    report = CompletionValidator(store, repository=repository).validate(
        "lease", execution_id=result.execution_id
    )
    assert report.status is ValidationStatus.FAILED
    assert report.lease_valid is False
    assert "lease_missing" in report.blocking_reasons
    finish_worktree(repository, worktree)


def test_expired_and_wrong_worker_leases_fail_closed(tmp_path: Path) -> None:
    root, repository, store, worktree = prepare_validation_task(
        tmp_path,
        "expired-lease",
        commit_file_command("src/expired.py", "ok\n"),
        write_paths=["src/**"],
    )
    run_successful_worker(root, repository, store, worktree, "expired-lease", claim=True)
    connection = sqlite3.connect(store.db_path)
    try:
        connection.execute(
            "UPDATE leases SET expires_at = ? WHERE task_id = ?",
            ("1970-01-01T00:00:00Z", "expired-lease"),
        )
        connection.commit()
    finally:
        connection.close()
    report = CompletionValidator(store, repository=repository).validate("expired-lease")
    assert report.status is ValidationStatus.FAILED
    assert "lease_expired" in report.blocking_reasons
    finish_worktree(repository, worktree)

    root, repository, store, worktree = prepare_validation_task(
        tmp_path / "wrong-worker",
        "wrong-worker",
        commit_file_command("src/wrong.py", "ok\n"),
        write_paths=["src/**"],
    )
    run_successful_worker(root, repository, store, worktree, "wrong-worker", claim=True)
    lease = LeaseManager(store).list_task("wrong-worker")[0]
    LeaseManager(store).release(
        lease.lease_id, "wrong-worker", "test-worker", lease.generation
    )
    LeaseManager(store).acquire(
        "wrong-worker", "other-worker", ["src/**"], ttl_seconds=60
    )
    report = CompletionValidator(store, repository=repository).validate("wrong-worker")
    assert report.status is ValidationStatus.FAILED
    assert "lease_missing" in report.blocking_reasons
    finish_worktree(repository, worktree)


def test_validation_concurrency_allows_one_active_run(tmp_path: Path) -> None:
    marker = tmp_path / "check-count.txt"
    check = RequiredCheck(
        "slow",
        tuple(
            python_command(
                f"from pathlib import Path; import time; "
                f"p=Path({str(marker)!r}); "
                "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1'); "
                "time.sleep(0.5)"
            )
        ),
        3,
    )
    root, repository, store, worktree = prepare_validation_task(
        tmp_path,
        "concurrent",
        commit_file_command("src/concurrent.py", "ok\n"),
        write_paths=["src/**"],
        required_checks=(check,),
    )
    result = run_successful_worker(
        root, repository, store, worktree, "concurrent", claim=True
    )
    barrier = threading.Barrier(2)
    reports = []
    errors = []

    def run_validation() -> None:
        barrier.wait()
        try:
            reports.append(
                CompletionValidator(store, repository=repository).validate(
                    "concurrent", execution_id=result.execution_id
                )
            )
        except Exception as exc:  # noqa: BLE001 - assert the exact concurrency error below
            errors.append(exc)

    threads = [threading.Thread(target=run_validation) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(reports) == 1
    assert reports[0].status is ValidationStatus.PASSED
    assert len(errors) == 1
    assert isinstance(errors[0], ValidationAlreadyRunningError)
    assert marker.read_text(encoding="utf-8") == "1"
    assert len(store.list_validations("concurrent")) == 1
    assert store.get("concurrent").state is TaskState.COMPLETED
    assert result.execution_id == reports[0].execution_id
    finish_worktree(repository, worktree)


def test_internal_validator_error_blocks_without_completion(tmp_path: Path) -> None:
    root, repository, store, worktree = prepare_validation_task(
        tmp_path,
        "internal",
        commit_file_command("src/internal.py", "ok\n"),
        write_paths=["src/**"],
    )
    run_successful_worker(root, repository, store, worktree, "internal", claim=True)
    validator = CompletionValidator(store, repository=repository)

    def fail_evaluation(*_args, **_kwargs):
        raise RuntimeError("synthetic validator failure")

    validator._evaluate = fail_evaluation  # type: ignore[method-assign]
    report = validator.validate("internal")
    assert report.status is ValidationStatus.BLOCKED
    assert report.blocking_reasons == ("validation_internal_error",)
    assert store.get("internal").state is TaskState.BLOCKED
    assert read_validation_report(root, report.validation_id)["status"] == "blocked"
    finish_worktree(repository, worktree)


def test_v3_to_v4_migration_preserves_execution_and_lease_history(tmp_path: Path) -> None:
    root, repository, store, worktree = prepare_validation_task(
        tmp_path,
        "migration-v4",
        python_command("pass"),
        write_paths=["src/**"],
    )
    lease = LeaseManager(store).list_task("migration-v4")[0]
    now = utc_now()
    execution_artifact = artifact_paths(root, "old-execution")
    execution_artifact.directory.mkdir(parents=True)
    execution_artifact.stdout_path.touch()
    execution_artifact.stderr_path.touch()
    execution_artifact.metadata_path.write_text("{}\n", encoding="utf-8")
    execution = ExecutionResult(
        execution_id="old-execution",
        task_id="migration-v4",
        worker_id="test-worker",
        status=ExecutionStatus.CREATED,
        command=("python", "-c", "pass"),
        worktree_path=str(worktree),
        pid=None,
        created_at=now,
        started_at=None,
        finished_at=None,
        duration_seconds=None,
        exit_code=None,
        timeout_seconds=10,
        artifact_dir=str(execution_artifact.directory),
        stdout_path=str(execution_artifact.stdout_path),
        stderr_path=str(execution_artifact.stderr_path),
    )
    store.create_execution(execution)
    store.start_execution("old-execution", pid=1, started_at=now)
    store.finish_execution(
        "old-execution",
        status=ExecutionStatus.SUCCEEDED,
        finished_at=now,
        exit_code=0,
        duration_seconds=0.1,
    )
    LeaseManager(store).release(lease.lease_id, "migration-v4", "test-worker", lease.generation)
    connection = sqlite3.connect(store.db_path)
    try:
        connection.execute("DROP TABLE validations")
        connection.execute("PRAGMA user_version = 3")
    finally:
        connection.close()

    migrated = TaskStore(root)
    assert DB_SCHEMA_VERSION == 4
    assert migrated.get("migration-v4").state is TaskState.ASSIGNED
    assert migrated.get_execution("old-execution").status is ExecutionStatus.SUCCEEDED
    assert Path(migrated.get_execution("old-execution").stdout_path).is_file()
    assert LeaseManager(migrated).get(lease.lease_id).status.value == "released"
    connection = sqlite3.connect(migrated.db_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='validations'"
        ).fetchone()[0] == "validations"
    finally:
        connection.close()
    finish_worktree(repository, worktree)


def test_future_schema_fails_closed_and_cli_json_is_pure(tmp_path: Path) -> None:
    root, repository, store, worktree = prepare_validation_task(
        tmp_path,
        "cli-validate",
        commit_file_command("src/cli.py", "ok\n"),
        write_paths=["src/**"],
    )
    run_successful_worker(root, repository, store, worktree, "cli-validate", claim=True)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-m", "agent_worktree", "task", "validate", "--task", "cli-validate", "--json"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert payload["task_id"] == "cli-validate"
    assert result.stdout.lstrip().startswith("{")
    finish_worktree(repository, worktree)

    connection = sqlite3.connect(store.db_path)
    try:
        connection.execute("PRAGMA user_version = 999")
    finally:
        connection.close()
    with pytest.raises(UnsupportedSchemaVersionError):
        TaskStore(root)
