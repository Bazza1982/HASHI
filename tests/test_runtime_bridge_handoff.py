from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_bridge_handoff


def _update():
    return SimpleNamespace()


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime():
    calls = []

    async def _cmd_bridge_handoff(update, context, mode):
        calls.append((update, context.args, mode))

    return SimpleNamespace(_cmd_bridge_handoff=_cmd_bridge_handoff), calls


@pytest.mark.asyncio
async def test_cmd_transfer_delegates_to_bridge_handoff():
    runtime, calls = _runtime()
    update = _update()

    await runtime_bridge_handoff.cmd_transfer(runtime, update, _context("sunny"))

    assert calls == [(update, ["sunny"], "transfer")]


@pytest.mark.asyncio
async def test_cmd_fork_delegates_to_bridge_handoff():
    runtime, calls = _runtime()
    update = _update()

    await runtime_bridge_handoff.cmd_fork(runtime, update, _context("sunny"))

    assert calls == [(update, ["sunny"], "fork")]
