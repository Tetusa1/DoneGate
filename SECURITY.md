# Security

## Reporting

Use the repository's security reporting channel once it is published. Do not include credentials, private source, or sensitive logs in a public issue.

## Trust model

The configured `worker_command` is a user-authorized local process. `agent-worktree` is designed to catch accidental or policy-violating Git changes; it is not a hostile-code sandbox. A worker can access anything allowed to the operating-system user, and `read_paths` is not an OS-level restriction.

## Operational guidance

- keep credentials out of task YAML and command arguments;
- review worker stdout, stderr, task definitions, and Git history before sharing artifacts;
- use deny paths for sensitive project files and verify the resulting validation report;
- treat a dirty or ambiguous worktree as data to inspect, not something to force-delete;
- use recovery dry-run before apply mode.

The project does not automatically merge, cherry-pick, reset, clean, stash, or force-delete branches.
