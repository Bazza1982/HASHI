from __future__ import annotations

import inspect
import os
import time
from pathlib import Path

import pytest

# Exercise python-telegram-bot's announced next-major RetryAfter type now. HASHI
# accepts both numeric seconds and timedelta, so this keeps the suite on the
# forward-compatible branch instead of emitting one deprecation per retry.
os.environ.setdefault("PTB_TIMEDELTA", "1")


_PROTOCOL_MANAGER_TESTS = frozenset({
    "test_hchat_freshness.py",
    "test_remote_discovery_contract.py",
    "test_remote_peer_status.py",
    "test_remote_rescue_control.py",
    "test_remote_shared_token.py",
    "test_session_api_freshness.py",
})


@pytest.fixture(autouse=True)
def isolate_protocol_manager_home(request, monkeypatch):
    """ProtocolManager writes under Path.home; tests must never touch operator state."""
    if request.node.path.name not in _PROTOCOL_MANAGER_TESTS:
        return
    root = request.getfixturevalue("tmp_path") / "isolated-protocol-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))


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


@pytest.fixture
def permission_clock() -> float:
    """Shared approval fixtures inject the same stable trusted time."""
    return time.time()
# HASHI-Fingerprint: hfp_94702ec91de742a1937e7aafc4a3aa25
