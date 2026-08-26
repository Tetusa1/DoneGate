"""Offline deterministic worker used by the README and CLI integration tests."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Make and commit one deterministic demo change.")
    parser.add_argument("--path", default="src/message.txt")
    args = parser.parse_args()
    target = Path(args.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("updated by demo worker\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", str(target)], check=True, shell=False)
    subprocess.run(["git", "commit", "-m", "demo worker update"], check=True, shell=False)
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True, encoding="utf-8", shell=False
    ).strip()
    print(f"DONEGATE_COMMIT: {commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
