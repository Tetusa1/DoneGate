"""Filesystem-backed process execution artifacts.

This module records process evidence only.  It intentionally does not inspect
Git changes, commits, tests, or task completion claims.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


_EXECUTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class ArtifactError(RuntimeError):
    """Base error for execution artifact operations."""


class UnsafeArtifactPathError(ArtifactError):
    """Raised when an execution artifact path would escape the repository."""


class ArtifactAlreadyExistsError(ArtifactError):
    """Raised when an execution artifact directory already exists."""


class ArtifactNotFoundError(ArtifactError):
    """Raised when an execution artifact does not exist."""


class ValidationArtifact:
    """Filesystem locations for one completion-validation report."""

    def __init__(self, directory: Path, report_path: Path, checks_directory: Path) -> None:
        self.directory = directory
        self.report_path = report_path
        self.checks_directory = checks_directory

    def to_dict(self) -> dict[str, str]:
        return {
            "directory": str(self.directory),
            "report_path": str(self.report_path),
            "checks_directory": str(self.checks_directory),
        }


@dataclass(frozen=True)
class ExecutionArtifact:
    directory: Path
    metadata_path: Path
    stdout_path: Path
    stderr_path: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "directory": str(self.directory),
            "metadata_path": str(self.metadata_path),
            "stdout_path": str(self.stdout_path),
            "stderr_path": str(self.stderr_path),
        }


def artifact_paths(repo_root: str | Path, execution_id: str) -> ExecutionArtifact:
    root = Path(repo_root).expanduser().resolve()
    safe_id = _safe_execution_id(execution_id)
    executions_root = (root / ".agent-worktree" / "executions").resolve(strict=False)
    try:
        executions_root.relative_to(root)
    except ValueError as exc:
        raise UnsafeArtifactPathError("execution artifact root escapes repository") from exc
    directory = (executions_root / safe_id).resolve(strict=False)
    if directory.parent != executions_root:
        raise UnsafeArtifactPathError("execution artifact path escapes repository")
    return ExecutionArtifact(
        directory=directory,
        metadata_path=directory / "metadata.json",
        stdout_path=directory / "stdout.log",
        stderr_path=directory / "stderr.log",
    )


def create_execution_artifact(repo_root: str | Path, execution_id: str) -> ExecutionArtifact:
    artifact = artifact_paths(repo_root, execution_id)
    if artifact.directory.exists():
        raise ArtifactAlreadyExistsError(f"execution artifact already exists: {execution_id}")
    try:
        artifact.directory.parent.mkdir(parents=True, exist_ok=True)
        artifact.directory.mkdir(exist_ok=False)
        artifact.stdout_path.touch(exist_ok=False)
        artifact.stderr_path.touch(exist_ok=False)
    except FileExistsError as exc:
        raise ArtifactAlreadyExistsError(f"execution artifact already exists: {execution_id}") from exc
    except OSError as exc:
        raise ArtifactError(f"cannot create execution artifact: {artifact.directory}") from exc
    return artifact


def validation_artifact_paths(repo_root: str | Path, validation_id: str) -> ValidationArtifact:
    root = Path(repo_root).expanduser().resolve()
    safe_id = _safe_execution_id(validation_id)
    validations_root = (root / ".agent-worktree" / "validations").resolve(strict=False)
    try:
        validations_root.relative_to(root)
    except ValueError as exc:
        raise UnsafeArtifactPathError("validation artifact root escapes repository") from exc
    directory = (validations_root / safe_id).resolve(strict=False)
    if directory.parent != validations_root:
        raise UnsafeArtifactPathError("validation artifact path escapes repository")
    return ValidationArtifact(
        directory=directory,
        report_path=directory / "report.json",
        checks_directory=directory / "checks",
    )


def create_validation_artifact(repo_root: str | Path, validation_id: str) -> ValidationArtifact:
    artifact = validation_artifact_paths(repo_root, validation_id)
    if artifact.directory.exists():
        raise ArtifactAlreadyExistsError(
            f"validation artifact already exists: {validation_id}"
        )
    try:
        artifact.directory.parent.mkdir(parents=True, exist_ok=True)
        artifact.directory.mkdir(exist_ok=False)
        artifact.checks_directory.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise ArtifactAlreadyExistsError(
            f"validation artifact already exists: {validation_id}"
        ) from exc
    except OSError as exc:
        raise ArtifactError(f"cannot create validation artifact: {artifact.directory}") from exc
    return artifact


def write_validation_report(
    artifact: ValidationArtifact, report: Mapping[str, Any]
) -> Path:
    return write_execution_metadata(artifact.report_path, report)


def read_validation_report(artifact_or_repo: ValidationArtifact | str | Path, validation_id: str | None = None) -> dict[str, Any]:
    if isinstance(artifact_or_repo, ValidationArtifact):
        target = artifact_or_repo.report_path
    elif validation_id is not None:
        target = validation_artifact_paths(artifact_or_repo, validation_id).report_path
    else:
        target = Path(artifact_or_repo).expanduser().resolve(strict=False)
        if target.name != "report.json":
            target = target / "report.json"
    if not target.is_file():
        raise ArtifactNotFoundError(f"validation report not found: {target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"cannot read validation report: {target}") from exc
    if not isinstance(payload, dict):
        raise ArtifactError(f"validation report must be an object: {target}")
    return payload


def write_execution_metadata(
    artifact: ExecutionArtifact | str | Path,
    metadata: Mapping[str, Any],
) -> Path:
    target = _metadata_path(artifact)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(dict(metadata), stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except (OSError, TypeError, ValueError) as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ArtifactError(f"cannot write execution metadata: {target}") from exc
    return target


def read_execution_metadata(
    artifact_or_repo: ExecutionArtifact | str | Path,
    execution_id: str | None = None,
) -> dict[str, Any]:
    if execution_id is None:
        target = _metadata_path(artifact_or_repo)
    else:
        target = artifact_paths(artifact_or_repo, execution_id).metadata_path
    if not target.is_file():
        raise ArtifactNotFoundError(f"execution metadata not found: {target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"cannot read execution metadata: {target}") from exc
    if not isinstance(payload, dict):
        raise ArtifactError(f"execution metadata must be an object: {target}")
    return payload


def _metadata_path(artifact: ExecutionArtifact | str | Path) -> Path:
    if isinstance(artifact, ExecutionArtifact):
        return artifact.metadata_path
    candidate = Path(artifact).expanduser().resolve(strict=False)
    if candidate.name in {"metadata.json", "report.json"}:
        return candidate
    return candidate / "metadata.json"


def _safe_execution_id(value: str) -> str:
    if not isinstance(value, str) or not _EXECUTION_ID_PATTERN.fullmatch(value):
        raise UnsafeArtifactPathError("execution_id is not a safe path component")
    return value
