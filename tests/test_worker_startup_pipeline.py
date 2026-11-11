"""Concurrent Functions startup keeps every independent Core admission."""
import asyncio
from types import SimpleNamespace

import pytest

from orchestrator import function_worker_supervisor as workers


def setup_supervisor(tmp_path, monkeypatch, *, limit=2):
    kernel = SimpleNamespace(
        paths=SimpleNamespace(code_root=tmp_path, bridge_home=tmp_path),
        global_cfg=SimpleNamespace(worker_launch_concurrency=limit),
        runtime_fingerprint=SimpleNamespace(to_dict=lambda: {"dependency_digest": "expected"}),
    )
    supervisor = workers.FunctionWorkerSupervisor(kernel)
    monkeypatch.setattr(supervisor, "topology_snapshot", lambda **kw: {})
    monkeypatch.setattr(workers, "load_runtime_policy", lambda root: object())
    monkeypatch.setattr(workers, "cached_current_runtime_fingerprint",
                        lambda *a, **k: SimpleNamespace(dependency_digest="expected"))
    generation = SimpleNamespace(
        manifest=SimpleNamespace(generation_id="sha256:" + "a"*64, to_dict=lambda: {}))
    return supervisor, generation


@pytest.mark.asyncio
async def test_admission_is_bounded_and_next_agent_launches_before_ready(tmp_path, monkeypatch):
    supervisor, generation = setup_supervisor(tmp_path, monkeypatch)
    active = 0
    peak = 0
    admitted = []
    two_launching = asyncio.Event()
    release_launch = asyncio.Event()
    all_preparing = asyncio.Event()
    release_ready = asyncio.Event()
    prepared = []

    async def launch(bootstrap):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        admitted.append(bootstrap)
        if active == 2:
            two_launching.set()
        try:
            await release_launch.wait()
            return SimpleNamespace(pid=len(admitted)), object()
        finally:
            active -= 1

    class Candidate:
        def __init__(self, **kw):
            self.name = kw["agent_name"]
        def start(self):
            pass
        async def wait_ready(self):
            prepared.append(self.name)
            if len(prepared) == 3:
                all_preparing.set()
            await release_ready.wait()
        async def shutdown(self, **kw):
            pass

    monkeypatch.setattr(workers, "launch_function_process_async", launch)
    monkeypatch.setattr(workers, "FunctionWorkerClient", Candidate)
    tasks = [asyncio.create_task(supervisor.prepare_worker(
        name, generation, generation_root=tmp_path)) for name in ("one", "two", "three")]
    try:
        await asyncio.wait_for(two_launching.wait(), 1)
        assert len(admitted) == 2 and not prepared
        release_launch.set()
        await asyncio.wait_for(all_preparing.wait(), 1)
        assert peak == 2
        assert len(admitted) == 3
        assert not any(task.done() for task in tasks)
        assert len({b["nonce"] for b in admitted}) == 3
        assert {b["agent_name"] for b in admitted} == {"one", "two", "three"}
        assert all(b["runtime"]["dependency_digest"] == "expected" for b in admitted)
        release_ready.set()
        assert len(await asyncio.gather(*tasks)) == 3
    finally:
        release_launch.set()
        release_ready.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_admission_failure_or_waiter_cancel_does_not_strand_slot(tmp_path, monkeypatch, cancel_waiter):
    supervisor, generation = setup_supervisor(tmp_path, monkeypatch, limit=1)
    first_entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def launch(bootstrap):
        calls.append(bootstrap["agent_name"])
        if bootstrap["agent_name"] == "first":
            first_entered.set()
            await release.wait()
            raise RuntimeError("launch failed")
        return SimpleNamespace(pid=123), object()

    class Candidate:
        def __init__(self, **kw):
            pass
        def start(self):
            pass
        async def wait_ready(self):
            pass
        async def shutdown(self, **kw):
            pass

    monkeypatch.setattr(workers, "launch_function_process_async", launch)
    monkeypatch.setattr(workers, "FunctionWorkerClient", Candidate)
    first = asyncio.create_task(supervisor.prepare_worker("first", generation, generation_root=tmp_path))
    waiter = None
    try:
        await asyncio.wait_for(first_entered.wait(), 1)
        waiter = asyncio.create_task(supervisor.prepare_worker("waiter", generation, generation_root=tmp_path))
        await asyncio.sleep(0)
        assert calls == ["first"]
        if cancel_waiter:
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        release.set()
        with pytest.raises(RuntimeError, match="launch failed"):
            await first
        if not cancel_waiter:
            await asyncio.wait_for(waiter, 1)
        await asyncio.wait_for(supervisor.prepare_worker("last", generation, generation_root=tmp_path), 1)
        assert calls == (["first", "last"] if cancel_waiter else ["first", "waiter", "last"])
    finally:
        release.set()
        for task in (first, waiter):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (first, waiter) if t), return_exceptions=True)
