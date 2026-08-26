# DoneGate

`DoneGate` runs coding-agent tasks in isolated Git worktrees.

Coding agents can touch the wrong files, collide with another agent, or report success without verifiable evidence. `DoneGate` gives each task a managed worktree and path lease, records worker execution evidence, and independently validates Git changes before declaring completion.

Worker exit code 0 is not completion. Completion requires independent checks of:

- the registered branch and Git commit;
- committed changed files and write-path ownership;
- denylist rules;
- the active lease;
- required checks; and
- execution evidence.

This is a local developer tool, not a host-security sandbox or a cloud agent platform.

## Before you start

Use `DoneGate` inside an existing Git repository with Python 3.11+ and Git available on `PATH`. The repository must have at least one commit so Git can resolve the task's base ref, and its primary working tree should be clean before creating or running a task.

Check the host repository first:

```bash
python --version
git --version
git status
git log -1 --oneline
git config user.name
git config user.email
```

If this is a new repository with no commit yet, initialize it once and create its first commit; do not reinitialize an existing repository:

```bash
git init
git add .
git commit -m "initial commit"
```

If a worker is expected to commit and either identity value is missing, configure this repository locally (not with `--global`):

```bash
git config user.name "Your Name"
git config user.email "you@example.com"
```

`DoneGate` does not automatically run `git init`, change Git identity, stash, reset, clean, or commit the primary checkout. Resolve or commit changes reported by `git status` before running a task.

## Installation

Python 3.11 or newer and Git are required.

```bash
python -m venv .venv
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
donegate --version
```

The runtime dependency is PyYAML. The `dev` extra supplies pytest and local package-build tooling.

## Quick Start: fully offline

The repository includes a deterministic worker that needs no model, API key, or network service. Run these commands from a clean clone:

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
donegate task create --file examples/demo-task.yaml
donegate task run --task demo-message --json
donegate task status --task demo-message --json
donegate task validate --task demo-message --json
donegate task cleanup --task demo-message --json
donegate recover --dry-run --json
```

The host Git repository must ignore `.agent-worktree/` because it stores local state, execution evidence, and managed worktrees. Add that entry to the host repository's `.gitignore` before creating a task; this repository already includes it.

The demo worker edits `src/message.txt`, commits it in the isolated worktree, prints a `DONEGATE_COMMIT: <commit>` claim, and runs `examples/demo_check.py` as a required check. The primary checkout remains untouched; cleanup removes only the managed worktree and releases its lease. The task row and execution/validation evidence remain in `.agent-worktree/`.

`DoneGate` does not depend on Codex, Claude, or any model provider. A real local coding-agent command can be used as `worker_command` when it follows the same commit-claim contract.

## CLI reference

```text
donegate --version
donegate task create --file TASK_FILE
donegate task run --task TASK_ID [--json]
donegate task status --task TASK_ID [--json]
donegate task validate --task TASK_ID [--json]
donegate task cleanup --task TASK_ID [--remove-branch] [--json]
donegate recover --dry-run [--json]
donegate recover --apply [--json]
```

Human-readable `task run` streams worker stdout/stderr live while preserving execution logs. JSON mode keeps stdout machine-readable and stores worker logs in the execution artifacts.

JSON mode writes one machine-readable document to stdout. Human diagnostics go to stderr on failure. Exit codes are stable for the v0.1 CLI: `0` means success, `1` means the task did not complete or validation failed, `2` means invalid usage or a refused task precondition, and `3` means infrastructure failure.

## Task format

Tasks are YAML documents with `schema_version`, `task_id`, `objective`, `base_ref`, `read_paths`, `write_paths`, `deny_paths`, `worker_id`, `worker_command`, `timeout_seconds`, and `required_checks`. See `examples/demo-task.yaml` and `examples/task.yaml`.

`write_paths` is the declared change allowlist. `deny_paths` has priority. `read_paths` is currently descriptive policy metadata; it is not an OS filesystem sandbox.

## How validation works

For writable tasks, validation requires a successful worker execution, a clean registered worktree on the declared branch, a commit claim that matches the independently read HEAD, a new descendant commit from the recorded base, an active unexpired lease, changes inside the allowlist and outside the denylist, and passing required checks. A successful worker process is not enough.

The worker should print its commit claim as one line on stdout:

```text
DONEGATE_COMMIT: 0123456789abcdef...
```

The runner stores that claim with execution metadata; the validator independently verifies it. Read-only tasks use `write_paths: []` and must leave the worktree at the base with no changes.

## Path ownership and leases

The task runner creates `<repo>/.agent-worktree/worktrees/<task-id>` and a namespaced `donegate/<task-id>` branch. Writable tasks acquire transactional, segment-aware path leases before the worker starts. Overlapping active leases are rejected. Lease history is retained as SQLite audit data.

## Cleanup and recovery

Explicit cleanup is allowed only for `completed`, `failed`, or `blocked` tasks. It refuses dirty worktrees, running executions, and active validations. It releases the task lease, removes only a clean registered managed worktree, and keeps the task row and evidence. Branches are preserved by default; `--remove-branch` uses safe namespace-limited branch deletion.

Recovery is report-first. `recover --dry-run` makes no task, lease, process, branch, or worktree repair. `recover --apply` only handles proven-safe local cases such as stale leases, dead persisted worker processes, and clean exact managed orphan worktrees. Dirty, foreign, unregistered, live, or ambiguous resources are preserved. Recovery never uses reset, clean, stash, force branch deletion, arbitrary recursive deletion, automatic merge, or automatic cherry-pick.

## Security model and limitations

This project protects against accidental or policy-violating changes by a trusted local worker. It is not a malicious-process containment boundary:

- the worker command is explicitly authorized by the user;
- the worker can technically access anything permitted to the operating-system user;
- `read_paths` is not filesystem-enforced;
- path ownership validates Git changes and leases, not OS ACLs;
- there is no full sandbox, remote worker coordination, prompt-safety system, or guarantee against hostile worker code;
- the tool is local-machine oriented;
- task definitions, logs, and worker output may contain sensitive project data; review them before sharing.

Do not put credentials in task files or worker arguments. Worker command metadata redacts common secret flag values, but stdout, stderr, task files, and Git history remain the user's responsibility.

## Development

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
python -m pytest
python -m build
```

Tests use only local deterministic workers and do not call model APIs or cloud services. The project targets Python 3.11+ and supports Windows and Linux through the same subprocess/Git abstractions. CI covers Windows and Ubuntu on Python 3.11 and 3.12.

## Provenance and license

This repository is a clean-room generic reimplementation of infrastructure patterns from an internal workflow prototype. It contains no domain data, private project code, or vendored third-party source. Runtime dependencies are Python standard library plus PyYAML; development tools are listed separately in `pyproject.toml`.

The project is released under the MIT License. See `LICENSE`, `PROVENANCE.md`, `CHANGELOG.md`, `SECURITY.md`, and `CONTRIBUTING.md`.

## Non-goals

- no LLM or provider SDK;
- no web UI, cloud service, or distributed execution;
- no automatic retry, repair loop, merge, or cherry-pick;
- no workflow DSL or plugin marketplace;
- no financial, research, or domain-specific workflow.
