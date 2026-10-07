from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.function_worker_host import FunctionWorkerHost


def _host(runtime):
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.phase = "ACTIVE"
    host.accepting = True
    host.agent_name = "test-agent"
    host._require_runtime = lambda: runtime
    host.metadata = lambda: {"worker_phase": host.phase}
    host.emit_metadata = _emit
    return host


async def _emit():
    return None


@pytest.mark.asyncio
async def test_optional_persona_text_is_cancelled_without_blocking_quiesce():
    cancelled = asyncio.Event()
    entered = asyncio.Event()

    async def rendering():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    render_task = asyncio.create_task(rendering())
    await entered.wait()
    runtime = SimpleNamespace(
        telegram_connected=True, queue=asyncio.Queue(), is_generating=False,
        _background_tasks=set(), _persona_background_status_tasks={render_task},
        _persona_background_status_inflight={"persona": render_task},
    )
    persisted = []
    runtime._persist_transfer_state = lambda: persisted.append(True)
    host = _host(runtime)
    try:
        result = await host.quiesce(0.1)
        assert result["ok"] and host.phase == "QUIESCED"
        await asyncio.sleep(0)
        assert cancelled.is_set() and render_task.cancelled()
        assert persisted == [True]
        assert runtime._persona_background_status_inflight == {}
        assert runtime._persona_background_status_paused is True
        await host.resume()
        assert runtime._persona_background_status_paused is False
        assert host.accepting and runtime.telegram_connected
    finally:
        render_task.cancel()
        await asyncio.gather(render_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_genuine_background_work_still_prevents_quiesce():
    task = asyncio.create_task(asyncio.Event().wait())
    runtime = SimpleNamespace(
        telegram_connected=True, queue=asyncio.Queue(), is_generating=False,
        _background_tasks={task}, _persona_background_status_tasks=set(),
    )
    host = _host(runtime)
    try:
        with pytest.raises(TimeoutError):
            await host.quiesce(0.1)
        assert host.phase == "ACTIVE" and host.accepting
        assert runtime.telegram_connected and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
