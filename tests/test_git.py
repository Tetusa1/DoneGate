from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_worktree.git import (
    BranchExistsError,
    GitCommandError,
    GitRepositoryError,
    GitRepository,
    InvalidBaseRefError,
    RepositoryDirtyError,
    RepositoryNotFoundError,
    UnsafeBranchNameError,
    WorktreeDirtyError,
    WorktreePathExistsError,
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


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "project"
    root.mkdir()
    run_git(root, "init", "-b", "main")
    run_git(root, "config", "user.name", "agent-worktree tests")
    run_git(root, "config", "user.email", "agent-worktree-tests@local.invalid")
    (root / ".gitignore").write_text(".agent-worktree/\n", encoding="utf-8")
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (root / "deleted.txt").write_text("delete me\n", encoding="utf-8")
    (root / "rename-source.txt").write_text("rename me\n", encoding="utf-8")
    (root / "space tracked.txt").write_text("spaces\n", encoding="utf-8")
    run_git(root, "add", ".")
    run_git(root, "commit", "-m", "initial test commit")
    return root, run_git(root, "rev-parse", "HEAD").stdout.strip()


def test_repository_discovery_and_nested_path(tmp_path: Path) -> None:
    root, _ = make_repo(tmp_path)
    nested = root / "nested" / "source"
    nested.mkdir(parents=True)

    repo = GitRepository(nested)

    assert repo.root == root.resolve()
    assert repo.current_head()
    assert repo.is_clean()


def test_non_git_directory_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "not-a-repository"
    directory.mkdir()

    with pytest.raises(RepositoryNotFoundError):
        GitRepository(directory)


def test_clean_repository_creates_real_isolated_worktree(tmp_path: Path) -> None:
    root, base = make_repo(tmp_path)
    repo = GitRepository(root)

    created = repo.create_worktree("parser-validation", "HEAD")

    assert created.branch == "agent-worktree/parser-validation"
    assert created.worktree_path == (
        root / ".agent-worktree" / "worktrees" / "parser-validation"
    ).resolve()
    assert created.base_ref == "HEAD"
    assert created.base_commit == base
    assert created.head_commit == base
    assert created.worktree_path.is_dir()
    assert run_git(created.worktree_path, "rev-parse", "HEAD").stdout.strip() == base
    assert repo.branch_exists(created.branch)
    assert any(item.branch == created.branch for item in repo.worktrees())

    repo.remove_worktree(created.worktree_path)
    assert not created.worktree_path.exists()
    assert repo.branch_exists(created.branch)


@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked"])
def test_dirty_primary_repository_is_rejected(tmp_path: Path, dirty_kind: str) -> None:
    root, _ = make_repo(tmp_path)
    repo = GitRepository(root)
    if dirty_kind == "tracked":
        (root / "tracked.txt").write_text("changed\n", encoding="utf-8")
    else:
        (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    with pytest.raises(RepositoryDirtyError):
        repo.create_worktree("dirty-rejected")

    assert not repo.branch_exists("agent-worktree/dirty-rejected")
    assert not (repo.worktree_root / "dirty-rejected").exists()


def test_invalid_base_ref_leaves_no_branch_or_worktree(tmp_path: Path) -> None:
    root, _ = make_repo(tmp_path)
    repo = GitRepository(root)

    with pytest.raises(InvalidBaseRefError):
        repo.create_worktree("invalid-base", "does-not-exist")

    assert not repo.branch_exists("agent-worktree/invalid-base")
    assert not (repo.worktree_root / "invalid-base").exists()


def test_existing_branch_is_preserved(tmp_path: Path) -> None:
    root, _ = make_repo(tmp_path)
    repo = GitRepository(root)
    branch = repo.branch_for_task("already-exists")
    run_git(root, "branch", branch)

    with pytest.raises(BranchExistsError):
        repo.create_worktree("already-exists")

    assert repo.branch_exists(branch)
    assert not (repo.worktree_root / "already-exists").exists()


def test_existing_worktree_path_and_content_are_preserved(tmp_path: Path) -> None:
    root, _ = make_repo(tmp_path)
    repo = GitRepository(root)
    target = repo.worktree_root / "existing-path"
    target.mkdir(parents=True)
    sentinel = target / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(WorktreePathExistsError):
        repo.create_worktree("existing-path")

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not repo.branch_exists("agent-worktree/existing-path")


def test_worktree_registry_is_parsed_from_real_git_output(tmp_path: Path) -> None:
    root, base = make_repo(tmp_path)
    repo = GitRepository(root)
    created = repo.create_worktree("registry-check")

    entries = repo.worktrees()
    root_entry = next(item for item in entries if item.path == root.resolve())
    worktree_entry = next(item for item in entries if item.path == created.worktree_path)

    assert root_entry.branch == "main"
    assert root_entry.head == base
    assert not root_entry.bare
    assert not root_entry.detached
    assert worktree_entry.branch == created.branch
    assert worktree_entry.head == base

    repo.remove_worktree(created.worktree_path)


def test_changed_files_include_statuses_spaces_and_rename(tmp_path: Path) -> None:
    root, _ = make_repo(tmp_path)
    repo = GitRepository(root)
    created = repo.create_worktree("changed-files")
    worktree = created.worktree_path

    (worktree / "tracked.txt").write_text("modified\n", encoding="utf-8")
    (worktree / "deleted.txt").unlink()
    (worktree / "rename-source.txt").rename(worktree / "renamed file.txt")
    (worktree / "new file.txt").write_text("new\n", encoding="utf-8")
    run_git(worktree, "add", "-A", "--", "rename-source.txt", "renamed file.txt")

    changed = set(repo.changed_files(worktree))

    assert {
        "tracked.txt",
        "deleted.txt",
        "rename-source.txt",
        "renamed file.txt",
        "new file.txt",
    }.issubset(changed)

    (worktree / "new file.txt").unlink()
    (worktree / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    run_git(worktree, "reset", "--hard", "HEAD")
    repo.remove_worktree(worktree)


def test_commit_validation_requires_commit_objects(tmp_path: Path) -> None:
    root, base = make_repo(tmp_path)
    repo = GitRepository(root)
    created = repo.create_worktree("commit-check")
    worktree = created.worktree_path

    (worktree / "tracked.txt").write_text("new commit\n", encoding="utf-8")
    run_git(worktree, "add", "tracked.txt")
    run_git(worktree, "commit", "-m", "worktree commit")
    new_commit = run_git(worktree, "rev-parse", "HEAD").stdout.strip()
    blob = run_git(worktree, "rev-parse", "HEAD:tracked.txt").stdout.strip()

    assert repo.commit_exists(base)
    assert repo.commit_exists(new_commit)
    assert not repo.commit_exists("deadbeef")
    assert not repo.commit_exists(blob)
    assert repo.is_ancestor(base, new_commit)
    assert not repo.is_ancestor(new_commit, base)

    repo.remove_worktree(worktree)


def test_dirty_worktree_cannot_be_removed_and_files_are_preserved(tmp_path: Path) -> None:
    root, _ = make_repo(tmp_path)
    repo = GitRepository(root)
    created = repo.create_worktree("dirty-worktree")
    dirty_file = created.worktree_path / "keep-me.txt"
    dirty_file.write_text("preserve", encoding="utf-8")

    with pytest.raises(WorktreeDirtyError):
        repo.remove_worktree(created.worktree_path)

    assert dirty_file.read_text(encoding="utf-8") == "preserve"
    assert repo.branch_exists(created.branch)
    dirty_file.unlink()
    repo.remove_worktree(created.worktree_path)


def test_rollback_removes_only_branch_created_by_failed_operation(tmp_path: Path) -> None:
    root, _ = make_repo(tmp_path)

    class FailingAddRepository(GitRepository):
        def _run_git(self, args, **kwargs):  # type: ignore[no-untyped-def]
            if tuple(args[:2]) == ("worktree", "add"):
                raise GitCommandError(args, 128, stderr="forced integration failure")
            return super()._run_git(args, **kwargs)

    repo = FailingAddRepository(root)
    branch = repo.branch_for_task("rollback")
    target = repo.worktree_root / "rollback"

    with pytest.raises(GitCommandError):
        repo.create_worktree("rollback")

    assert not repo.branch_exists(branch)
    assert not target.exists()
    assert len(repo.worktrees()) == 1


def test_branch_deletion_is_namespace_limited_and_safe(tmp_path: Path) -> None:
    root, _ = make_repo(tmp_path)
    repo = GitRepository(root)
    created = repo.create_worktree("delete-check")

    with pytest.raises(GitRepositoryError):
        repo.delete_branch(created.branch)
    with pytest.raises(UnsafeBranchNameError):
        repo.delete_branch("main")

    repo.remove_worktree(created.worktree_path)
    assert repo.delete_branch(created.branch)
    assert not repo.branch_exists(created.branch)
    assert not repo.delete_branch(created.branch)


@pytest.mark.parametrize(
    "task_id",
    ["", "   ", "--help", "../main", "foo..bar", "foo.lock", "foo bar"],
)
def test_task_id_cannot_inject_or_escape_branch_namespace(tmp_path: Path, task_id: str) -> None:
    root, _ = make_repo(tmp_path)
    repo = GitRepository(root)

    with pytest.raises(UnsafeBranchNameError):
        repo.branch_for_task(task_id)
