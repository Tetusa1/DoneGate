from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from test_worker import run_git


ROOT = Path(__file__).resolve().parents[1]


def _cli(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "donegate", *args],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )


def _write_task(
    root: Path,
    task_id: str,
    *,
    worker_args: tuple[str, ...] = (),
    write_paths: str = '  - "src/message.txt"',
    deny_paths: str = '  - "secrets/**"',
    required_check: str = "python examples/demo_check.py",
) -> Path:
    command = '  - "python"\n  - "examples/demo_worker.py"'
    for argument in worker_args:
        command += f'\n  - "{argument}"'
    if required_check == "python examples/demo_check.py":
        check = '  - name: "demo-check"\n    command:\n      - "python"\n      - "examples/demo_check.py"'
    else:
        check = (
            '  - name: "failing-check"\n'
            '    command:\n'
            '      - "python"\n'
            '      - "-c"\n'
            f'      - "{required_check}"'
        )
    task = f'''schema_version: "0.1"
task_id: "{task_id}"
objective: "Run a deterministic CLI integration task."
base_ref: "HEAD"
read_paths:
  - "examples/**"
  - "src/**"
write_paths:
{write_paths}
deny_paths:
{deny_paths}
worker_id: "demo-worker"
worker_command:
{command}
timeout_seconds: 60
required_checks:
{check}
'''
    path = root / f"{task_id}.yaml"
    path.write_text(task, encoding="utf-8")
    run_git(root, "add", path.name)
    run_git(root, "commit", "-m", f"add task {task_id}")
    return path


def _make_demo_repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir(parents=True)
    run_git(root, "init", "-b", "main")
    run_git(root, "config", "user.name", "DoneGate e2e")
    run_git(root, "config", "user.email", "donegate-e2e@local.invalid")
    (root / ".gitignore").write_text(".agent-worktree/\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "message.txt").write_text("hello\n", encoding="utf-8")
    (root / "examples").mkdir()
    for name in ("demo_worker.py", "demo_check.py"):
        shutil.copy2(ROOT / "examples" / name, root / "examples" / name)
    run_git(root, "add", ".")
    run_git(root, "commit", "-m", "initial demo repo")
    return root


def test_full_offline_cli_workflow_completes_and_cleans(tmp_path: Path) -> None:
    root = _make_demo_repo(tmp_path)
    task_file = _write_task(root, "demo-message")

    created = _cli(root, "task", "create", "--file", str(task_file))
    assert created.returncode == 0, created.stderr
    assert json.loads(created.stdout)["task"]["state"] == "pending"

    run = _cli(root, "task", "run", "--task", "demo-message", "--json")
    assert run.returncode == 0, run.stderr
    run_payload = json.loads(run.stdout)
    assert "AGENT_WORKTREE_COMMIT:" not in run.stdout
    assert "Worker output:" not in run.stdout
    assert run_payload["status"] == "completed"
    assert run_payload["state"] == "completed"
    assert run_payload["execution"]["status"] == "succeeded"
    assert run_payload["validation"]["status"] == "passed"
    execution_dir = Path(run_payload["execution"]["artifact_dir"])
    validation_dir = Path(run_payload["validation"]["artifact_dir"])
    assert execution_dir.is_dir()
    assert validation_dir.is_dir()

    status = _cli(root, "task", "status", "--task", "demo-message", "--json")
    assert json.loads(status.stdout)["state"] == "completed"
    revalidated = _cli(root, "task", "validate", "--task", "demo-message", "--json")
    assert revalidated.returncode == 0, revalidated.stderr
    assert json.loads(revalidated.stdout)["status"] == "passed"

    cleanup = _cli(root, "task", "cleanup", "--task", "demo-message", "--json")
    assert cleanup.returncode == 0, cleanup.stderr
    cleanup_payload = json.loads(cleanup.stdout)
    assert cleanup_payload["result"] == "CLEANED"
    assert not Path(cleanup_payload["worktree"]).exists()
    assert execution_dir.is_dir()
    assert validation_dir.is_dir()
    final_status = _cli(root, "task", "status", "--task", "demo-message", "--json")
    assert json.loads(final_status.stdout)["state"] == "cleaned"


def test_human_cli_streams_worker_output_and_keeps_summary(tmp_path: Path) -> None:
    root = _make_demo_repo(tmp_path)
    task_file = _write_task(root, "human-output")
    assert _cli(root, "task", "create", "--file", str(task_file)).returncode == 0

    run = _cli(root, "task", "run", "--task", "human-output")
    assert run.returncode == 0, run.stderr
    assert "Task: human-output" in run.stdout
    assert "Worker output:" in run.stdout
    assert "AGENT_WORKTREE_COMMIT:" in run.stdout
    assert "STATUS: completed" in run.stdout
    assert "AGENT_WORKTREE_COMMIT:" not in run.stderr

    cleanup = _cli(root, "task", "cleanup", "--task", "human-output")
    assert cleanup.returncode == 0, cleanup.stderr


def test_worker_exit_zero_but_allowlist_violation_fails_validation(tmp_path: Path) -> None:
    root = _make_demo_repo(tmp_path)
    task_file = _write_task(
        root,
        "allowlist-failure",
        worker_args=("--path", "secrets/changed.txt"),
        deny_paths='  - ".env"',
    )
    assert _cli(root, "task", "create", "--file", str(task_file)).returncode == 0

    run = _cli(root, "task", "run", "--task", "allowlist-failure", "--json")
    assert run.returncode == 1
    payload = json.loads(run.stdout)
    assert payload["status"] == "failed"
    assert payload["execution"]["status"] == "succeeded"
    assert payload["validation"]["status"] == "failed"
    assert "write_scope_violation" in payload["validation"]["blocking_reasons"]
    assert Path(payload["worktree"]).is_dir()
    assert _cli(root, "task", "cleanup", "--task", "allowlist-failure").returncode == 0


def test_worker_exit_zero_but_required_check_failure_fails_validation(tmp_path: Path) -> None:
    root = _make_demo_repo(tmp_path)
    task_file = _write_task(
        root,
        "check-failure",
        required_check="raise SystemExit(7)",
    )
    assert _cli(root, "task", "create", "--file", str(task_file)).returncode == 0

    run = _cli(root, "task", "run", "--task", "check-failure", "--json")
    assert run.returncode == 1
    payload = json.loads(run.stdout)
    assert payload["execution"]["status"] == "succeeded"
    assert payload["validation"]["status"] == "failed"
    assert "required_check_failed" in payload["validation"]["blocking_reasons"]
    assert Path(payload["worktree"]).is_dir()
    assert _cli(root, "task", "cleanup", "--task", "check-failure").returncode == 0


def test_public_run_uses_lease_gate_for_overlapping_tasks(tmp_path: Path) -> None:
    root = _make_demo_repo(tmp_path)
    first = _write_task(root, "lease-one")
    second = _write_task(root, "lease-two")
    assert _cli(root, "task", "create", "--file", str(first)).returncode == 0
    assert _cli(root, "task", "create", "--file", str(second)).returncode == 0
    assert _cli(root, "task", "run", "--task", "lease-one").returncode == 0

    blocked = _cli(root, "task", "run", "--task", "lease-two", "--json")
    assert blocked.returncode == 3
    payload = json.loads(blocked.stdout)
    assert payload["code"] == "task_setup_failed"
    status = _cli(root, "task", "status", "--task", "lease-two", "--json")
    assert json.loads(status.stdout)["state"] == "blocked"
    assert _cli(root, "task", "cleanup", "--task", "lease-one").returncode == 0
    assert _cli(root, "task", "cleanup", "--task", "lease-two").returncode == 0


def test_recovery_cli_dry_run_then_apply_repairs_clean_orphan(tmp_path: Path) -> None:
    root = _make_demo_repo(tmp_path)
    orphan = root / ".agent-worktree" / "worktrees" / "orphan-cli"
    orphan.parent.mkdir(parents=True)
    run_git(root, "worktree", "add", "-b", "donegate/orphan-cli", str(orphan), "HEAD")

    dry = _cli(root, "recover", "--dry-run", "--json")
    assert dry.returncode == 0, dry.stderr
    dry_payload = json.loads(dry.stdout)
    assert any(item["code"] == "orphan_managed_worktree" for item in dry_payload["findings"])
    assert orphan.exists()

    applied = _cli(root, "recover", "--apply", "--json")
    assert applied.returncode == 0, applied.stderr
    applied_payload = json.loads(applied.stdout)
    assert any(item["code"] == "ORPHAN_WORKTREE_REMOVED" for item in applied_payload["actions"])
    assert not orphan.exists()
