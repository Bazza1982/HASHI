from __future__ import annotations

import inspect

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Run async tests on asyncio via pytest-anyio."""
    return "asyncio"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-mark async tests so pytest-anyio executes them correctly."""
    anyio_marker = pytest.mark.anyio
    contract_marker = pytest.mark.contract
    for item in items:
        if {"contract", "ete"}.intersection(item.path.parts):
            item.add_marker(contract_marker)
        obj = getattr(item, "obj", None)
        if (
            obj is not None
            and inspect.iscoroutinefunction(obj)
            and item.get_closest_marker("asyncio") is None
        ):
            item.add_marker(anyio_marker)
