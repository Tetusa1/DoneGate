# Contributing

Install the development environment with:

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
```

Run `python -m pytest` before submitting a change. Add focused tests for behavior changes and keep at least one realistic CLI/integration path covered when changing orchestration. Tests must use deterministic local commands; do not call Codex, Claude, OpenAI, DeepSeek, or other network model APIs.

Keep the provider-neutral core small, preserve fail-closed behavior, and do not add automatic merge, cherry-pick, retry, or repair behavior without a separate design decision.
