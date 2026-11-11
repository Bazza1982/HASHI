"""Fresh runtime checks must not stall the Functions control/IPC loop."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from orchestrator import function_worker_supervisor as workers


def setup(tmp_path, monkeypatch, scan):
    kernel = SimpleNamespace(
        paths=SimpleNamespace(code_root=tmp_path, bridge_home=tmp_path),
        runtime_fingerprint=SimpleNamespace(to_dict=lambda: {"dependency_digest": "old"}),
    )
    supervisor = workers.FunctionWorkerSupervisor(kernel)
    monkeypatch.setattr(supervisor, "topology_snapshot", lambda **kw: {})
    monkeypatch.setattr(workers, "load_runtime_policy", lambda root: ("policy", root))
    monkeypatch.setattr(workers, "cached_current_runtime_fingerprint", scan)
    launched = []

    async def launch(bootstrap):
        launched.append(bootstrap)
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
    generation = SimpleNamespace(
        manifest=SimpleNamespace(generation_id="sha256:" + "a" * 64, to_dict=lambda: {}))
    return supervisor, generation, launched


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_control_loop_runs_during_freshness_scan_and_cancel_never_launches(tmp_path, monkeypatch, cancel):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    scan_calls = []

    def scan(policy, *, code_root):
        scan_calls.append((policy, code_root))
        entered.set()
        assert release.wait(1), "Functions control loop was blocked by the disk scan"
        finished.set()
        return SimpleNamespace(dependency_digest="fresh")

    supervisor, generation, launched = setup(tmp_path, monkeypatch, scan)
    task = asyncio.create_task(supervisor.prepare_worker("one", generation, generation_root=tmp_path))
    try:
        async def wait_entered():
            while not entered.is_set():
                await asyncio.sleep(0)
        await asyncio.wait_for(wait_entered(), 1)
        await asyncio.sleep(0)  # an unrelated control callback can run now
        assert not finished.is_set()
        assert not launched
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        release.set()
        if not cancel:
            await asyncio.wait_for(task, 1)
            assert len(launched) == 1
            assert launched[0]["runtime"]["dependency_digest"] == "fresh"
        else:
            await asyncio.to_thread(finished.wait, 1)
            await asyncio.sleep(0)
            assert not launched
        assert scan_calls == [(("policy", tmp_path), tmp_path)]
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_freshness_scan_fails_closed_and_next_worker_checks_again(tmp_path, monkeypatch):
    calls = []

    def scan(policy, *, code_root):
        calls.append(code_root)
        if len(calls) == 1:
            raise OSError("metadata unreadable")
        return SimpleNamespace(dependency_digest="fresh")

    supervisor, generation, launched = setup(tmp_path, monkeypatch, scan)
    with pytest.raises(OSError, match="metadata unreadable"):
        await supervisor.prepare_worker("failed", generation, generation_root=tmp_path)
    assert not launched
    await supervisor.prepare_worker("next", generation, generation_root=tmp_path)
    assert calls == [tmp_path, tmp_path]
    assert len(launched) == 1 and launched[0]["runtime"]["dependency_digest"] == "fresh"
