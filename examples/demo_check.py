"""Offline required check for the deterministic demo workflow."""

from pathlib import Path


def main() -> int:
    expected = "updated by demo worker\n"
    actual = Path("src/message.txt").read_text(encoding="utf-8")
    if actual != expected:
        raise SystemExit(f"unexpected demo content: {actual!r}")
    print("demo check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
