from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from orchestrator.her_v2.interfaces import TurnControl, TurnStopped
from orchestrator.out_of_band_control import AgentControlLane


@pytest.mark.asyncio
async def test_agent_control_lane_interrupts_on_dedicated_thread():
    caller_thread = threading.get_ident()
    observed: dict[str, object] = {}

    class Backend:
        def interrupt_nowait(self, reason):
            observed["thread"] = threading.get_ident()
            observed["reason"] = reason
            return 1

    runtime = SimpleNamespace(
        name="worker-agent",
        backend_manager=SimpleNamespace(current_backend=Backend()),
    )
    lane = AgentControlLane(runtime)
    try:
        result = await lane.interrupt("USER_STOP")
    finally:
        await asyncio.to_thread(lane.close)

    assert result.interrupted == 1
    assert result.worker_thread_id == observed["thread"]
    assert result.worker_thread_id != caller_thread
    assert observed["reason"] == "USER_STOP"


@pytest.mark.asyncio
async def test_turn_control_accepts_thread_safe_stop_signal():
    control = TurnControl("turn-1")
    started = asyncio.Event()

    async def operation():
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(control.run_cancellable(operation()))
    await started.wait()
    await asyncio.to_thread(control.stop, "USER_STOP")

    with pytest.raises(TurnStopped, match="USER_STOP"):
        await asyncio.wait_for(task, timeout=1.0)
    assert control.stopped is True
