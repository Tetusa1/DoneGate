"""Small, fail-closed Git and worktree adapter for DoneGate."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Sequence

from .models import CreatedWorktree, WorktreeInfo


class GitRepositoryError(RuntimeError):
    """Base error for repository discovery and Git operations."""


class RepositoryNotFoundError(GitRepositoryError):
    """Raised when a path is not an existing Git repository or subdirectory."""


class GitCommandError(GitRepositoryError):
    """Raised when Git cannot execute or rejects a command."""

    def __init__(
        self,
        git_args: Sequence[str],
        returncode: int | None,
        *,
        stdout: str | bytes = "",
        stderr: str | bytes = "",
    ) -> None:
        self.git_args = tuple(git_args)
        self.returncode = returncode
        self.stdout = _decode_output(stdout)
        self.stderr = _decode_output(stderr)
        command = "git " + " ".join(self.git_args)
        status = "could not start" if returncode is None else f"exited with {returncode}"
        detail = self.stderr.strip() or self.stdout.strip()
        message = f"{command} {status}"
        if detail:
            message += f": {detail}"
        super().__init__(message)


class RepositoryDirtyError(GitRepositoryError):
    """Raised when the primary repository or a worktree is dirty."""


class UnsafeBranchNameError(GitRepositoryError):
    """Raised when task or branch input cannot safely become a Git ref."""


class BranchExistsError(GitRepositoryError):
    """Raised when a task branch already exists."""


class WorktreePathExistsError(GitRepositoryError):
    """Raised when a target path exists or is already registered."""


class InvalidBaseRefError(GitRepositoryError):
    """Raised when a base ref cannot be resolved to a commit."""


class WorktreeCreationError(GitRepositoryError):
    """Raised when creation fails and rollback is incomplete or validation fails."""


class WorktreeNotFoundError(GitRepositoryError):
    """Raised when a path is not a registered non-main worktree."""


class WorktreeDirtyError(RepositoryDirtyError):
    """Raised when removing a dirty worktree without force."""


_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_COMMIT_HASH_PATTERN = re.compile(r"^[0-9A-Fa-f]{4,64}$")
_REF_FORBIDDEN_CHARS = set("~^:?*[\\")


def _decode_output(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return os.fsdecode(value)
    return value


def _argument_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise GitRepositoryError(f"{name} must be a non-empty string without NUL")
    if any(ord(char) < 32 for char in value):
        raise GitRepositoryError(f"{name} contains a control character")
    return value


class GitRepository:
    """A narrow subprocess-backed adapter around one existing Git repository."""

    def __init__(self, path: str | Path, *, git_executable: str = "git") -> None:
        candidate = Path(path).expanduser()
        if not candidate.exists() or not candidate.is_dir():
            raise RepositoryNotFoundError(f"repository path does not exist or is not a directory: {path}")
        self._candidate = candidate.resolve()
        self.git_executable = _argument_text(git_executable, "git_executable")
        try:
            result = self._run_git(["rev-parse", "--show-toplevel"], cwd=self._candidate)
        except GitCommandError as exc:
            raise RepositoryNotFoundError(
                f"path is not inside a Git repository: {self._candidate}"
            ) from exc
        root_text = result.stdout.strip()
        if not root_text:
            raise RepositoryNotFoundError(f"Git returned no repository root for: {self._candidate}")
        self.root = Path(root_text).resolve()

    def _run_git(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        git_args = tuple(_argument_text(arg, "git argument") for arg in args)
        workdir = cwd or getattr(self, "root", self._candidate)
        try:
            run_options: dict[str, object] = {
                "cwd": str(workdir),
                "capture_output": True,
                "shell": False,
                "check": False,
            }
            if text:
                run_options.update({"text": True, "encoding": "utf-8", "errors": "replace"})
            else:
                run_options["text"] = False
            result = subprocess.run([self.git_executable, *git_args], **run_options)
        except OSError as exc:
            raise GitCommandError(git_args, None, stderr=str(exc)) from exc
        if check and result.returncode != 0:
            raise GitCommandError(
                git_args,
                result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        return result

    @property
    def worktree_root(self) -> Path:
        """Return the default, repository-local worktree parent."""

        return self.root / ".agent-worktree" / "worktrees"

    def current_head(self) -> str:
        return self._resolve_commit("HEAD")

    def resolve_commit(self, ref: str) -> str:
        """Resolve a commit reference to its full commit hash."""

        return self._resolve_commit(ref)

    def head_at(self, worktree_path: str | Path) -> str:
        """Read HEAD from a registered worktree, not from task metadata."""

        return self._commit_at(self._registered_path(worktree_path))

    def is_clean(self) -> bool:
        result = self._run_git(
            ["status", "--porcelain=v1", "--untracked-files=all"],
        )
        return not bool(result.stdout)

    def is_clean_at(self, worktree_path: str | Path) -> bool:
        """Check a registered worktree for tracked, staged, and untracked changes."""

        return self._is_clean_at(self._registered_path(worktree_path))

    def branch_exists(self, name: str) -> bool:
        branch = self._validate_branch_name(name)
        result = self._run_git(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
        )
        if result.returncode not in (0, 1):
            raise GitCommandError(
                ("show-ref", "--verify", "--quiet", f"refs/heads/{branch}"),
                result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        return result.returncode == 0

    def worktrees(self) -> tuple[WorktreeInfo, ...]:
        result = self._run_git(["worktree", "list", "--porcelain"])
        records: list[WorktreeInfo] = []
        current: dict[str, object] | None = None

        def flush() -> None:
            nonlocal current
            if current is None:
                return
            raw_path = current.get("path")
            if not isinstance(raw_path, str):
                raise GitRepositoryError("Git worktree output omitted a worktree path")
            records.append(
                WorktreeInfo(
                    path=Path(raw_path).resolve(strict=False),
                    head=current.get("head") if isinstance(current.get("head"), str) else None,
                    branch=current.get("branch")
                    if isinstance(current.get("branch"), str)
                    else None,
                    bare=bool(current.get("bare", False)),
                    detached=bool(current.get("detached", False)),
                )
            )
            current = None

        for line in result.stdout.splitlines():
            if not line:
                flush()
                continue
            if line.startswith("worktree "):
                flush()
                current = {"path": line[len("worktree ") :]}
            elif current is None:
                continue
            elif line.startswith("HEAD "):
                current["head"] = line[len("HEAD ") :].strip()
            elif line.startswith("branch "):
                branch_ref = line[len("branch ") :].strip()
                current["branch"] = branch_ref.removeprefix("refs/heads/")
            elif line == "bare":
                current["bare"] = True
            elif line == "detached":
                current["detached"] = True
            # Unknown porcelain fields are intentionally ignored.
        flush()
        return tuple(records)

    def create_worktree(self, task_id: str, base_ref: str = "HEAD") -> CreatedWorktree:
        branch = self.branch_for_task(task_id)
        if not self.is_clean():
            raise RepositoryDirtyError(
                "primary repository is dirty; refusing to create an agent worktree"
            )
        base_commit = self._resolve_commit(base_ref)
        if self.branch_exists(branch):
            raise BranchExistsError(f"task branch already exists: {branch}")

        target = self._target_path(task_id)
        if os.path.lexists(str(target)):
            raise WorktreePathExistsError(f"worktree target already exists: {target}")
        if self._find_worktree(target) is not None:
            raise WorktreePathExistsError(f"worktree target is already registered: {target}")

        created_dirs: tuple[Path, ...] = ()
        branch_created = False
        try:
            created_dirs = self._prepare_parent_dirs(target.parent)
            self._run_git(["branch", branch, base_commit])
            branch_created = True
            self._run_git(["worktree", "add", str(target), branch])

            head_commit = self._commit_at(target)
            if head_commit != base_commit:
                raise WorktreeCreationError(
                    f"new worktree HEAD mismatch: expected {base_commit}, got {head_commit}"
                )
            info = self._find_worktree(target)
            if info is None or info.branch != branch or info.head != head_commit:
                raise WorktreeCreationError(
                    "new worktree was not represented correctly in Git registry"
                )
            return CreatedWorktree(
                task_id=task_id,
                branch=branch,
                worktree_path=target,
                base_ref=base_ref,
                base_commit=base_commit,
                head_commit=head_commit,
            )
        except Exception as exc:
            rollback_errors: list[str] = []
            if branch_created:
                rollback_errors.extend(self._rollback_created_worktree(branch, target))
            self._remove_empty_dirs(created_dirs)
            if rollback_errors:
                detail = "; ".join(rollback_errors)
                raise WorktreeCreationError(
                    f"worktree creation failed: {exc}; rollback incomplete: {detail}"
                ) from exc
            raise

    def changed_files(self, worktree_path: str | Path) -> tuple[str, ...]:
        worktree = self._registered_path(worktree_path)
        result = self._run_git(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=worktree,
            text=False,
        )
        raw = result.stdout
        if not isinstance(raw, bytes):
            raise GitRepositoryError("Git status did not return byte output")

        paths: set[str] = set()
        records = raw.split(b"\0")
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            if len(record) < 4 or record[2:3] != b" ":
                raise GitRepositoryError("unexpected git status --porcelain=v1 -z record")
            status = record[:2].decode("ascii", errors="strict")
            paths.add(self._canonical_status_path(os.fsdecode(record[3:])))
            if "R" in status or "C" in status:
                if index >= len(records) or not records[index]:
                    raise GitRepositoryError("rename/copy status record omitted original path")
                paths.add(self._canonical_status_path(os.fsdecode(records[index])))
                index += 1
        return tuple(sorted(paths))

    def commit_exists(self, commit_hash: str) -> bool:
        if not isinstance(commit_hash, str) or not _COMMIT_HASH_PATTERN.fullmatch(commit_hash):
            return False
        result = self._run_git(["cat-file", "-t", commit_hash], check=False)
        if result.returncode != 0:
            return False
        return result.stdout.strip() == "commit"

    def object_type(self, object_hash: str) -> str | None:
        if not isinstance(object_hash, str) or not _COMMIT_HASH_PATTERN.fullmatch(object_hash):
            return None
        result = self._run_git(["cat-file", "-t", object_hash], check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def changed_files_between(self, base_commit: str, commit: str) -> tuple[str, ...]:
        """Return committed changed paths in a machine-readable commit range.

        Rename and copy records contribute both their old and new paths so
        allowlist and denylist policy cannot hide the source side of a move.
        """

        base = self._resolve_commit(base_commit)
        target = self._resolve_commit(commit)
        result = self._run_git(
            [
                "diff",
                "--name-status",
                "-z",
                "--find-renames",
                "--find-copies",
                "--no-ext-diff",
                base,
                target,
            ],
            text=False,
        )
        raw = result.stdout
        if not isinstance(raw, bytes):
            raise GitRepositoryError("Git diff did not return byte output")
        fields = raw.split(b"\0")
        paths: set[str] = set()
        index = 0
        while index < len(fields):
            status_raw = fields[index]
            index += 1
            if not status_raw:
                continue
            status = os.fsdecode(status_raw)
            count = 2 if status[:1] in {"R", "C"} else 1
            if index + count > len(fields):
                raise GitRepositoryError("Git diff omitted a changed path")
            for _ in range(count):
                path = os.fsdecode(fields[index])
                index += 1
                paths.add(self._canonical_status_path(path))
        return tuple(sorted(paths))

    def is_ancestor(self, base_commit: str, commit: str) -> bool:
        base = self._resolve_commit(base_commit)
        descendant = self._resolve_commit(commit)
        result = self._run_git(
            ["merge-base", "--is-ancestor", base, descendant],
            check=False,
        )
        if result.returncode not in (0, 1):
            raise GitCommandError(
                ("merge-base", "--is-ancestor", base, descendant),
                result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        return result.returncode == 0

    def remove_worktree(self, worktree_path: str | Path) -> WorktreeInfo:
        info = self._find_worktree(worktree_path)
        if info is None or info.path == self.root:
            raise WorktreeNotFoundError(
                f"path is not a registered removable worktree: {worktree_path}"
            )
        if not self._is_clean_at(info.path):
            raise WorktreeDirtyError(
                f"worktree is dirty; refusing to remove it: {info.path}"
            )
        self._run_git(["worktree", "remove", str(info.path)])
        if self._find_worktree(info.path) is not None:
            raise GitRepositoryError(f"Git still reports worktree after removal: {info.path}")
        return info

    def delete_branch(self, branch: str) -> bool:
        validated = self._validate_branch_name(branch)
        if not validated.startswith("donegate/"):
            raise UnsafeBranchNameError(
                "only donegate/ namespace branches may be deleted"
            )
        if not self.branch_exists(validated):
            return False
        if any(info.branch == validated for info in self.worktrees()):
            raise GitRepositoryError(f"branch is still used by a worktree: {validated}")
        self._run_git(["branch", "--delete", validated])
        return True

    def branch_for_task(self, task_id: str) -> str:
        if not isinstance(task_id, str) or not task_id or task_id != task_id.strip():
            raise UnsafeBranchNameError("task_id must be non-empty and contain no surrounding whitespace")
        if not _TASK_ID_PATTERN.fullmatch(task_id):
            raise UnsafeBranchNameError(f"unsafe task_id for branch creation: {task_id!r}")
        if ".." in task_id or task_id.lower().endswith(".lock"):
            raise UnsafeBranchNameError(f"unsafe task_id for branch creation: {task_id!r}")
        return self._validate_branch_name(f"donegate/{task_id}")

    def _resolve_commit(self, ref: str) -> str:
        value = _argument_text(ref, "base_ref")
        result = self._run_git(
            ["rev-parse", "--verify", "--end-of-options", f"{value}^{{commit}}"],
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise InvalidBaseRefError(f"base ref does not resolve to a commit: {ref}")
        return result.stdout.strip()

    def _commit_at(self, worktree: Path) -> str:
        result = self._run_git(
            ["rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"],
            cwd=worktree,
        )
        return result.stdout.strip()

    def _target_path(self, task_id: str) -> Path:
        target = (self.worktree_root / task_id).resolve(strict=False)
        layout_root = self.worktree_root.resolve(strict=False)
        if target.parent != layout_root or not self._is_within(layout_root, self.root):
            raise WorktreePathExistsError("default worktree path escapes repository root")
        return target

    def _prepare_parent_dirs(self, parent: Path) -> tuple[Path, ...]:
        resolved_parent = parent.resolve(strict=False)
        if not self._is_within(resolved_parent, self.root):
            raise GitRepositoryError("worktree parent escapes repository root")
        missing: list[Path] = []
        current = parent
        while not os.path.lexists(str(current)):
            missing.append(current)
            current = current.parent
        created: list[Path] = []
        try:
            for directory in reversed(missing):
                directory.mkdir()
                created.append(directory)
        except OSError as exc:
            self._remove_empty_dirs(tuple(created))
            raise GitRepositoryError(f"cannot create worktree parent: {parent}") from exc
        return tuple(created)

    def _rollback_created_worktree(self, branch: str, target: Path) -> list[str]:
        errors: list[str] = []
        info = self._find_worktree(target)
        if info is not None:
            try:
                if not self._is_clean_at(info.path):
                    errors.append(f"new worktree became dirty: {info.path}")
                else:
                    self._run_git(["worktree", "remove", str(info.path)])
            except Exception as exc:
                errors.append(f"could not remove new worktree: {exc}")
        elif os.path.lexists(str(target)):
            try:
                if target.is_dir() and not any(target.iterdir()):
                    target.rmdir()
                else:
                    errors.append(f"unrecognized non-empty creation path preserved: {target}")
            except OSError as exc:
                errors.append(f"could not remove empty creation path: {exc}")

        try:
            if self.branch_exists(branch):
                if any(item.branch == branch for item in self.worktrees()):
                    errors.append(f"new branch remains in use: {branch}")
                else:
                    self._run_git(["branch", "--delete", branch])
        except Exception as exc:
            errors.append(f"could not safely delete new branch: {exc}")
        return errors

    def _find_worktree(self, path: str | Path) -> WorktreeInfo | None:
        candidate = self._canonical_path(path)
        for info in self.worktrees():
            if self._canonical_path(info.path) == candidate:
                return info
        return None

    def _registered_path(self, path: str | Path) -> Path:
        info = self._find_worktree(path)
        if info is None:
            raise WorktreeNotFoundError(f"path is not registered by Git: {path}")
        return info.path

    def _is_clean_at(self, path: Path) -> bool:
        result = self._run_git(
            ["status", "--porcelain=v1", "--untracked-files=all"],
            cwd=path,
        )
        return not bool(result.stdout)

    def _validate_branch_name(self, name: str) -> str:
        branch = _argument_text(name, "branch")
        if (
            branch != branch.strip()
            or branch.startswith("-")
            or branch.startswith("/")
            or branch.endswith("/")
            or branch.endswith(".")
            or branch.lower().endswith(".lock")
            or ".." in branch
            or "//" in branch
            or "@{" in branch
            or any(char in _REF_FORBIDDEN_CHARS for char in branch)
        ):
            raise UnsafeBranchNameError(f"unsafe Git branch name: {name!r}")
        result = self._run_git(["check-ref-format", "--branch", branch], check=False)
        if result.returncode != 0:
            raise UnsafeBranchNameError(f"invalid Git branch name: {name!r}")
        return branch

    @staticmethod
    def _canonical_path(path: str | Path) -> Path:
        return Path(path).expanduser().resolve(strict=False)

    @staticmethod
    def _is_within(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
        except ValueError:
            return False
        return True

    @staticmethod
    def _canonical_status_path(path: str) -> str:
        if not path or path.startswith(("/", "\\")):
            raise GitRepositoryError(f"Git returned an unsafe changed path: {path!r}")
        normalized = path.replace("\\", "/")
        parts = [part for part in normalized.split("/") if part]
        if not parts or any(part in {".", ".."} for part in parts):
            raise GitRepositoryError(f"Git returned an unsafe changed path: {path!r}")
        return "/".join(parts)

    @staticmethod
    def _remove_empty_dirs(directories: Sequence[Path]) -> None:
        for directory in reversed(tuple(directories)):
            try:
                directory.rmdir()
            except OSError:
                pass
