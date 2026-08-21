"""Provider-neutral task definitions for the agent-worktree CLI skeleton."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping, Sequence

import yaml


class TaskValidationError(ValueError):
    """Raised when a task file violates the v0.1 schema or path safety rules."""


_TASK_FIELDS = {
    "schema_version",
    "task_id",
    "objective",
    "base_ref",
    "read_paths",
    "write_paths",
    "deny_paths",
    "worker_id",
    "worker_command",
    "timeout_seconds",
    "required_checks",
}
_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TaskValidationError(f"{context} must be a mapping")
    return value


def _require_nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _require_argv(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TaskValidationError(f"{field} must be a non-empty argv list")
    if not value:
        raise TaskValidationError(f"{field} must be a non-empty argv list")
    command: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise TaskValidationError(f"{field}[{index}] must be a non-empty string")
        if "\x00" in item:
            raise TaskValidationError(f"{field}[{index}] contains a NUL byte")
        command.append(item)
    return tuple(command)


def _validate_relative_path(value: Any, field: str, index: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskValidationError(f"{field}[{index}] must be a non-empty repository-relative path")

    raw = value.strip()
    if "\x00" in raw:
        raise TaskValidationError(f"{field}[{index}] contains a NUL byte")

    windows_path = PureWindowsPath(raw)
    posix_path = PurePosixPath(raw.replace("\\", "/"))
    if windows_path.is_absolute() or windows_path.drive or posix_path.is_absolute():
        raise TaskValidationError(f"{field}[{index}] must be repository-relative")
    if raw.startswith(("/", "\\")):
        raise TaskValidationError(f"{field}[{index}] must be repository-relative")

    parts = [part for part in re.split(r"[\\/]+", raw) if part]
    if not parts or any(part in {"..", "."} for part in parts):
        raise TaskValidationError(f"{field}[{index}] cannot contain traversal segments")

    return "/".join(parts)


def _path_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TaskValidationError(f"{field} must be a list")
    return tuple(_validate_relative_path(item, field, index) for index, item in enumerate(value))


@dataclass(frozen=True)
class RequiredCheck:
    name: str
    command: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Any, index: int) -> "RequiredCheck":
        mapping = _require_mapping(value, f"required_checks[{index}]")
        unexpected = set(mapping) - {"name", "command"}
        if unexpected:
            raise TaskValidationError(
                f"required_checks[{index}] has unknown fields: {sorted(unexpected)}"
            )
        return cls(
            name=_require_nonempty_string(mapping.get("name"), f"required_checks[{index}].name"),
            command=_require_argv(mapping.get("command"), f"required_checks[{index}].command"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "command": list(self.command)}


@dataclass(frozen=True)
class TaskDefinition:
    schema_version: str
    task_id: str
    objective: str
    base_ref: str
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    deny_paths: tuple[str, ...]
    worker_id: str
    worker_command: tuple[str, ...]
    timeout_seconds: int
    required_checks: tuple[RequiredCheck, ...]

    @classmethod
    def from_mapping(cls, value: Any) -> "TaskDefinition":
        mapping = _require_mapping(value, "task")
        unexpected = set(mapping) - _TASK_FIELDS
        if unexpected:
            raise TaskValidationError(f"task has unknown fields: {sorted(unexpected)}")

        schema_version = _require_nonempty_string(mapping.get("schema_version"), "schema_version")
        if schema_version != "0.1":
            raise TaskValidationError("schema_version must be '0.1'")

        task_id = _require_nonempty_string(mapping.get("task_id"), "task_id")
        if not _TASK_ID_PATTERN.fullmatch(task_id):
            raise TaskValidationError(
                "task_id may contain only letters, numbers, '.', '_' and '-'"
            )

        timeout = mapping.get("timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise TaskValidationError("timeout_seconds must be a positive integer")

        checks_value = mapping.get("required_checks")
        if not isinstance(checks_value, Sequence) or isinstance(checks_value, (str, bytes)):
            raise TaskValidationError("required_checks must be a list")

        return cls(
            schema_version=schema_version,
            task_id=task_id,
            objective=_require_nonempty_string(mapping.get("objective"), "objective"),
            base_ref=_require_nonempty_string(mapping.get("base_ref"), "base_ref"),
            read_paths=_path_list(mapping.get("read_paths"), "read_paths"),
            write_paths=_path_list(mapping.get("write_paths"), "write_paths"),
            deny_paths=_path_list(mapping.get("deny_paths"), "deny_paths"),
            worker_id=_require_nonempty_string(mapping.get("worker_id"), "worker_id"),
            worker_command=_require_argv(mapping.get("worker_command"), "worker_command"),
            timeout_seconds=timeout,
            required_checks=tuple(
                RequiredCheck.from_mapping(item, index)
                for index, item in enumerate(checks_value)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "objective": self.objective,
            "base_ref": self.base_ref,
            "read_paths": list(self.read_paths),
            "write_paths": list(self.write_paths),
            "deny_paths": list(self.deny_paths),
            "worker_id": self.worker_id,
            "worker_command": list(self.worker_command),
            "timeout_seconds": self.timeout_seconds,
            "required_checks": [check.to_dict() for check in self.required_checks],
        }


def load_task_file(path: Path) -> TaskDefinition:
    """Load and validate one YAML task file without writing a task store."""

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TaskValidationError(f"task file not found: {path}") from exc
    except OSError as exc:
        raise TaskValidationError(f"cannot read task file: {path}") from exc
    except yaml.YAMLError as exc:
        raise TaskValidationError(f"invalid YAML in task file: {path}") from exc
    return TaskDefinition.from_mapping(payload)
