"""A cancelled native handoff cannot leak the prepared candidate or replay state."""
import asyncio
from pathlib import Path

import pytest

from orchestrator.kernel_process import KernelRuntime, ProcessClient
from orchestrator.runtime_contract import enforce_runtime_contract
from tests.test_kernel_process import release

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_cancel_after_real_candidate_prepare_retains_old_route_and_cleans_candidate(tmp_path, monkeypatch):
    kernel = KernelRuntime(ROOT, tmp_path / "instance", enforce_runtime_contract(ROOT))
    await kernel.start(release(tmp_path))
    old = kernel.active
    await old.peer.request("execute")
    reached = asyncio.Event()
    candidates = []
    prepare = ProcessClient.prepare
    original_request = old.peer.request

    async def observed_prepare(bootstrap):
        client = await prepare(bootstrap)
        candidates.append(client)
        return client

    async def paused_request(method, *args, **kwargs):
        if method == "quiesce":
            reached.set()
            await asyncio.Future()
        return await original_request(method, *args, **kwargs)

    monkeypatch.setattr(ProcessClient, "prepare", observed_prepare)
    monkeypatch.setattr(old.peer, "request", paused_request)
    task = asyncio.create_task(kernel.replace(release(tmp_path, "candidate")))
    try:
        await asyncio.wait_for(reached.wait(), timeout=30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(candidates) == 1
        assert not candidates[0].process.is_alive()
        assert kernel.active is old
        assert (await original_request("inspect"))["count"] == 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for client in candidates:
            await client.close()
        await old.close()
# HASHI-Fingerprint: hfp_5107e005e1c64561a503d4d3ae12fa9f
