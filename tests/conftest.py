from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Keep subprocess path assertions independent from the Windows user name."""

    if config.getoption("basetemp") is None:
        config.option.basetemp = str(
            Path(tempfile.gettempdir()) / f"donegate-pytest-{os.getpid()}"
        )
