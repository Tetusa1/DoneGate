# agent-worktree

> A local CLI for running coding agents in isolated Git worktrees with path ownership and evidence-based completion validation.

## Status

`agent-worktree` is in v0.1 development. It validates task files, persists a restart-safe SQLite task state machine, provides a transactional path-lease core, runs generic non-interactive worker processes with persisted execution artifacts, independently validates completion, and provides conservative cleanup and recovery orchestration.

## What

`agent-worktree` is a small local coding-agent worktree task runner. It accepts a provider-neutral command such as `codex`, `claude`, `python`, or another CLI program and gives that worker a structured task definition.

## Why

Coding agents need explicit boundaries because they can:

- modify the wrong project;
- collide with another agent on the same files;
- write outside their assigned scope;
- claim completion without trustworthy Git or test evidence.

The project addresses those risks with isolated worktrees, path ownership, leases, generic worker commands, process evidence, fail-closed validation, and cleanup/recovery. The worker adapter is provider-neutral: it receives argv, uses a validated task worktree as cwd, captures stdout/stderr separately, and never decides whether a task is complete.

## Quick start

With Python 3.11+ and the project dependencies installed:

```bash
python -m agent_worktree --help
agent-worktree task create --file examples/task.yaml
agent-worktree task status --task parser-validation
agent-worktree task status --task parser-validation --json
agent-worktree task validate --task parser-validation --json
agent-worktree task cleanup --task parser-validation --json
agent-worktree recover --dry-run --json
```

`task create` validates, normalizes, and persists a task in `.agent-worktree/state/state.sqlite3` with initial state `pending`. The Python worker API can run a task only after its Git worktree metadata and write lease preconditions are satisfied. Execution logs are written to `.agent-worktree/executions/<execution-id>/`; validation reports and required-check logs are written to `.agent-worktree/validations/<validation-id>/`; recovery audit reports are written to `.agent-worktree/recovery/<recovery-id>/report.json`. stdin is `DEVNULL` and worker/check commands run with `shell=False`.

Worker exit code 0 is not completion. `agent-worktree` independently verifies Git state, path ownership, commit ancestry, required checks, and execution evidence before marking a task completed.

## CLI surface

```text
agent-worktree task create --file TASK_FILE
agent-worktree task run --task TASK_ID
agent-worktree task status --task TASK_ID
agent-worktree task validate --task TASK_ID
agent-worktree task cleanup --task TASK_ID [--remove-branch] [--json]
agent-worktree recover --dry-run
agent-worktree recover --apply [--json]
```

`task create`, `task status`, `task validate`, `task cleanup`, and `recover` are implemented. `task run` remains intentionally unavailable in the public CLI because this repository does not yet expose a complete public worktree/lease orchestration setup path.

## Capability matrix

Implemented:

- task schema and persisted task state transitions;
- Git worktree adapter;
- transactional path leases;
- generic argv-based worker process execution;
- separate, streaming stdout/stderr execution artifacts;
- timeout, cancellation, process-group termination, and restart-readable execution results.
- independent fail-closed completion validation;
- verified worktree branch/HEAD/base ancestry and committed changed-file collection;
- write allowlist and denylist enforcement, with denylist priority;
- orchestrator-owned required checks with bounded timeouts and stdout/stderr artifacts;
- persisted validation reports and SQLite v3-to-v4 migration;
- explicit terminal-task cleanup with lease release, dirty-worktree protection, and optional safe branch deletion;
- report-first recovery for stale leases, missing worker processes, inconsistent task metadata, orphan worktrees, and unregistered managed directories;
- restart-readable recovery reports and conservative apply-only repairs.

Not implemented:

- automatic retry or repair loops;
- automatic merge or cherry-pick;
- provider-specific SDKs, parsers, prompts, or model integrations;
- a public `task run` orchestration command.

Recovery reports first and only applies repairs that are proven safe. Dirty or ambiguous worktrees are preserved. The recovery engine is not a Git cleanup bot: it never uses `git reset`, `git clean`, `stash`, force branch deletion, arbitrary recursive deletion, or foreign-worktree removal. Execution and validation artifacts, task rows, and lease history are preserved as audit evidence.

## Task paths

Task paths are repository-relative patterns. Absolute paths, empty paths, and `..` traversal are rejected at schema-validation time. The lease core accepts exact paths and `directory/**` patterns, detects segment-aware overlap, and persists active, released, and stale lease history in the same SQLite state database.

For writable tasks, validation requires a successful execution, a clean registered worktree on the declared branch, an explicit worker commit claim that matches the independently read HEAD, a new commit descending from the recorded base, an active unexpired lease, and passing required checks. `write_paths == []` is the explicit v0.1 read-only mode: no commit claim is required, but the worktree must remain at the base with no committed or uncommitted changes.

`read_paths` is currently task-declaration and future-policy information; it is not a runtime filesystem sandbox.

## Non-goals

- not an LLM SDK;
- not a prompt framework;
- not a cloud agent platform;
- not a multi-agent chat UI;
- not an auto-merge bot;
- not a financial or research workflow.
