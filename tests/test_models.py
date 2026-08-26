from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from donegate.models import TaskDefinition, TaskValidationError, load_task_file


ROOT = Path(__file__).resolve().parents[1]


def valid_task() -> dict:
    return {
        "schema_version": "0.1",
        "task_id": "parser-validation",
        "objective": "Add validation for malformed parser input.",
        "base_ref": "HEAD",
        "read_paths": ["src/**", "tests/**"],
        "write_paths": ["src/parser.py", "tests/test_parser.py"],
        "deny_paths": [".env", "secrets/**"],
        "worker_id": "local-agent",
        "worker_command": ["python", "worker.py"],
        "timeout_seconds": 1800,
        "required_checks": [
            {"name": "unit-tests", "command": ["python", "-m", "pytest", "-q"]}
        ],
    }


def run_cli(*args: str, state_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source = str(ROOT / "src")
    environment["PYTHONPATH"] = source + os.pathsep + environment.get("PYTHONPATH", "")
    environment["GIT_CONFIG_COUNT"] = "1"
    environment["GIT_CONFIG_KEY_0"] = "safe.directory"
    environment["GIT_CONFIG_VALUE_0"] = str(ROOT)
    if state_path is not None:
        environment["AGENT_WORKTREE_STATE_PATH"] = str(state_path)
    return subprocess.run(
        [sys.executable, "-m", "donegate", *args],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_valid_task_loads_and_normalizes() -> None:
    task = TaskDefinition.from_mapping(valid_task())
    assert task.task_id == "parser-validation"
    assert task.worker_command == ("python", "worker.py")
    assert task.required_checks[0].name == "unit-tests"


@pytest.mark.parametrize("path", ["C:\\Users\\test\\secret", "/etc/passwd"])
def test_absolute_paths_are_rejected(path: str) -> None:
    payload = valid_task()
    payload["write_paths"] = [path]
    with pytest.raises(TaskValidationError):
        TaskDefinition.from_mapping(payload)


def test_traversal_is_rejected() -> None:
    payload = valid_task()
    payload["read_paths"] = ["../secret"]
    with pytest.raises(TaskValidationError):
        TaskDefinition.from_mapping(payload)


def test_empty_worker_command_is_rejected() -> None:
    payload = valid_task()
    payload["worker_command"] = []
    with pytest.raises(TaskValidationError):
        TaskDefinition.from_mapping(payload)


def test_malformed_required_check_is_rejected() -> None:
    payload = valid_task()
    payload["required_checks"] = [{"name": "unit-tests", "command": []}]
    with pytest.raises(TaskValidationError):
        TaskDefinition.from_mapping(payload)


def test_cli_help_passes() -> None:
    result = run_cli("--help")
    assert result.returncode == 0, result.stderr
    assert "task" in result.stdout


def test_task_create_validates_example(tmp_path: Path) -> None:
    result = run_cli(
        "task", "create", "--file", "examples/task.yaml", state_path=tmp_path / "state.sqlite3"
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "validated"
    assert payload["task"]["task_id"] == "parser-validation"


def test_runtime_command_is_public_and_rejects_missing_task() -> None:
    result = run_cli("task", "run", "--task", "parser-validation")
    assert result.returncode != 0
    assert "TaskNotFound" in result.stderr


def test_load_task_file_reads_yaml() -> None:
    task = load_task_file(ROOT / "examples" / "task.yaml")
    assert task.base_ref == "HEAD"
