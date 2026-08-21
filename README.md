# agent-worktree

> A local CLI for running coding agents in isolated Git worktrees with path ownership and evidence-based completion validation.

## Status

`agent-worktree` is in v0.1 development. This slice validates task files, persists a restart-safe SQLite task state machine, and provides a transactional path-lease core. The Git worktree runtime, worker execution, evidence collection, completion validation, and recovery engine are planned but intentionally not implemented yet.

## What

`agent-worktree` is a small local coding-agent worktree task runner. It accepts a provider-neutral command such as `codex`, `claude`, `python`, or another CLI program and gives that worker a structured task definition.

## Why

Coding agents need explicit boundaries because they can:

- modify the wrong project;
- collide with another agent on the same files;
- write outside their assigned scope;
- claim completion without trustworthy Git or test evidence.

The planned project addresses those risks with isolated worktrees, path ownership, leases, generic worker commands, evidence collection, fail-closed validation, and cleanup/recovery. The lease manager currently provides the reusable path-ownership primitive; it is not wired into worker runtime commands yet.

## Quick start

With Python 3.11+ and the project dependencies installed:

```bash
python -m agent_worktree --help
agent-worktree task create --file examples/task.yaml
agent-worktree task status --task parser-validation
agent-worktree task status --task parser-validation --json
```

`task create` validates, normalizes, and persists a task in `.agent-worktree/state/state.sqlite3` with initial state `pending`. It does not create a worktree or start a worker. `task status` reads the persisted record and supports human-readable or JSON output.

## CLI surface

```text
agent-worktree task create --file TASK_FILE
agent-worktree task run --task TASK_ID
agent-worktree task status --task TASK_ID
agent-worktree task validate --task TASK_ID
agent-worktree task cleanup --task TASK_ID
agent-worktree recover --dry-run
agent-worktree recover --apply
```

Only `task create` and `task status` are implemented in this development task. The other commands fail explicitly with `NOT_IMPLEMENTED_IN_TASK_01` rather than claiming a successful runtime operation.

## Task paths

Task paths are repository-relative patterns. Absolute paths, empty paths, and `..` traversal are rejected at schema-validation time. The lease core accepts exact paths and `directory/**` patterns, detects segment-aware overlap, and persists active, released, and stale lease history in the same SQLite state database.

## Non-goals

- not an LLM SDK;
- not a prompt framework;
- not a cloud agent platform;
- not a multi-agent chat UI;
- not an auto-merge bot;
- not a financial or research workflow.
