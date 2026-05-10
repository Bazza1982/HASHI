from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator import runtime_terminate


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


class _Orchestrator:
    def __init__(self):
        self.stopped = []

    async def stop_agent(self, name):
        self.stopped.append(name)
        return True, f"stopped {name}"


def _runtime(orchestrator=None):
    replies = []
    runtime = SimpleNamespace(
        name="lin_yueru",
        orchestrator=orchestrator,
        _is_authorized_user=lambda user_id: True,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, replies


@pytest.mark.asyncio
async def test_cmd_terminate_replies_and_stops_agent():
    orchestrator = _Orchestrator()
    runtime, replies = _runtime(orchestrator)

    await runtime_terminate.cmd_terminate(runtime, _update(), _context())
    await asyncio.sleep(0)

    assert replies[-1][0] == "Shutting down."
    assert orchestrator.stopped == ["lin_yueru"]


@pytest.mark.asyncio
async def test_cmd_terminate_without_orchestrator_reports_unavailable():
    runtime, replies = _runtime(None)

    await runtime_terminate.cmd_terminate(runtime, _update(), _context())

    assert replies[-1][0] == "Dynamic lifecycle control is unavailable."
