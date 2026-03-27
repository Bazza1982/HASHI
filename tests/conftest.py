from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from tests.mocks.test_logger import TestLogger, set_logger


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Run async tests on asyncio via pytest-anyio."""
    return "asyncio"


@pytest.fixture
def logger(tmp_path: Path) -> TestLogger:
    """Provide an isolated logger instance for each test."""
    test_logger = TestLogger(
        log_dir=tmp_path / "logs",
        console_output=False,
        json_output=True,
    )
    set_logger(test_logger)
    return test_logger


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-mark async tests so pytest-anyio executes them correctly."""
    anyio_marker = pytest.mark.anyio
    for item in items:
        obj = getattr(item, "obj", None)
        if obj is not None and inspect.iscoroutinefunction(obj):
            item.add_marker(anyio_marker)
