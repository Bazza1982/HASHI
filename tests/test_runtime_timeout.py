from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_timeout


class _Backend:
    DEFAULT_IDLE_TIMEOUT_SEC = 300
    DEFAULT_HARD_TIMEOUT_SEC = 1800

    def __init__(self, extra=None):
        self.config = SimpleNamespace(extra=extra)


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime(extra=None):
    replies = []
    backend = _Backend(extra=extra)
    runtime = SimpleNamespace(
        name="lin_yueru",
        backend=None,
        backend_manager=SimpleNamespace(current_backend=backend),
        _is_authorized_user=lambda user_id: True,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, backend, replies


@pytest.mark.asyncio
async def test_cmd_timeout_shows_current_values():
    runtime, _backend, replies = _runtime(extra={"idle_timeout_sec": 1800, "hard_timeout_sec": 7200})

    await runtime_timeout.cmd_timeout(runtime, _update(), _context())

    assert "Timeout — lin_yueru" in replies[-1][0]
    assert "30 min" in replies[-1][0]
    assert replies[-1][1]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_cmd_timeout_reset_clears_overrides():
    runtime, backend, replies = _runtime(extra={"idle_timeout_sec": 1800, "hard_timeout_sec": 7200, "process_timeout": 99})

    await runtime_timeout.cmd_timeout(runtime, _update(), _context("reset"))

    assert backend.config.extra == {}
    assert "Timeout reset to defaults" in replies[-1][0]


@pytest.mark.asyncio
async def test_cmd_timeout_updates_values():
    runtime, backend, replies = _runtime(extra={})

    await runtime_timeout.cmd_timeout(runtime, _update(), _context("15", "45"))

    assert backend.config.extra["idle_timeout_sec"] == 900
    assert backend.config.extra["hard_timeout_sec"] == 2700
    assert "idle=15 min, hard=45 min" in replies[-1][0]
