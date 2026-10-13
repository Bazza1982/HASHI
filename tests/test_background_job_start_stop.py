from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from orchestrator.background_jobs import BackgroundJobManager


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_pending", [False, True])
async def test_stop_during_spawn_reaps_old_job_and_allows_new_work(
    tmp_path: Path, monkeypatch, cancel_pending: bool
):
    manager = BackgroundJobManager(tmp_path / "background_jobs")
    await manager.start()
    entered = asyncio.Event()
    release = asyncio.Event()
    spawn = asyncio.create_subprocess_exec
    children = []
    child_argv = [sys.executable, "-c", "import time; time.sleep(30)"]

    async def delayed_spawn(*args, **kwargs):
        if list(args) == child_argv:
            entered.set()
            await release.wait()
        process = await spawn(*args, **kwargs)
        if list(args) == child_argv:
            children.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    starting = asyncio.create_task(
        manager.start_job(
            agent="zelda", cwd=tmp_path, argv=child_argv,
            notify_on_failure=False, trigger_agent_on_failure=False,
        )
    )
    cancelling = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        record = manager.list(agent="zelda")[0]
        assert record.state == "starting"
        manager.stop_fences.advance("zelda")
        if cancel_pending:
            cancelling = asyncio.create_task(
                manager.cancel(record.job_id, grace_seconds=0.1)
            )
            for _ in range(20):
                if manager.get(record.job_id).state != "starting":
                    break
                await asyncio.sleep(0)
        release.set()
        result = await asyncio.wait_for(starting, timeout=10)
        if cancelling is not None:
            cancelled = await asyncio.wait_for(cancelling, timeout=10)
            assert cancelled.state == "cancelled"
        assert result.state == "cancelled"
        assert children and all(child.returncode is not None for child in children)
        assert manager.get(record.job_id).state == "cancelled"

        fresh = await manager.start_job(
            agent="zelda", cwd=tmp_path,
            argv=[sys.executable, "-c", "print('after stop')"],
            notify_on_complete=False, trigger_agent_on_complete=False,
        )
        monitor = manager._monitor_tasks.get(fresh.job_id)
        if monitor is not None:
            await asyncio.wait_for(monitor, timeout=10)
        assert manager.get(fresh.job_id).state == "succeeded"
        assert fresh.origin["agent_stop_epoch"] == 1
    finally:
        release.set()
        await asyncio.gather(starting, *([cancelling] if cancelling else []),
                             return_exceptions=True)
        await manager.stop()
        for child in children:
            if child.returncode is None:
                await manager._terminate_process_group(child, grace_seconds=0.1)


@pytest.mark.asyncio
async def test_cancelled_start_caller_cannot_orphan_spawned_child(tmp_path: Path, monkeypatch):
    manager = BackgroundJobManager(tmp_path / "background_jobs")
    await manager.start()
    entered = asyncio.Event()
    release = asyncio.Event()
    spawn = asyncio.create_subprocess_exec
    children = []
    child_argv = [sys.executable, "-c", "import time; time.sleep(30)"]

    async def delayed_return(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        if list(args) == child_argv:
            children.append(process)
            entered.set()
            await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_return)
    starting = asyncio.create_task(
        manager.start_job(
            agent="zelda", cwd=tmp_path, argv=child_argv,
            notify_on_failure=False, trigger_agent_on_failure=False,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        job_id = manager.list(agent="zelda")[0].job_id
        starting.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(starting, timeout=10)
        assert children and all(child.returncode is not None for child in children)
        assert manager.get(job_id).state == "cancelled"
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)
        await manager.stop()
        for child in children:
            if child.returncode is None:
                await manager._terminate_process_group(child, grace_seconds=0.1)

@pytest.mark.asyncio
async def test_agent_stop_cancels_all_jobs_through_worker_facade(tmp_path: Path):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from orchestrator import runtime_control
    from orchestrator.background_jobs import BackgroundJobRecord, NONTERMINAL_STATES
    from orchestrator.function_worker_supervisor import FunctionWorkerSupervisor

    manager = BackgroundJobManager(tmp_path / "background_jobs")
    await manager.start()
    created = datetime(2026, 10, 14, tzinfo=timezone.utc)
    for index in range(1003):
        manager.store.create(BackgroundJobRecord(
            job_id=f"job-{index:04}", state="starting",
            agent="other" if index == 1002 else "zelda",
            command={}, origin={"agent_stop_epoch": 1 if index == 1001 else 0},
            policy={}, process={}, logs={}, notification={},
            created_at=(created + timedelta(seconds=index)).isoformat(),
            updated_at="2026-10-14T00:00:00+00:00",
        ))
    supervisor = SimpleNamespace(kernel=SimpleNamespace(background_job_manager=manager))

    async def remote_list(**kwargs):
        return await FunctionWorkerSupervisor._background_job_request(
            supervisor, "core.background_jobs.list", kwargs
        )

    async def remote_cancel(job_id, **kwargs):
        return await FunctionWorkerSupervisor._background_job_request(
            supervisor, "core.background_jobs.cancel", {"job_id": job_id, **kwargs}
        )

    runtime = SimpleNamespace(
        name="zelda",
        background_job_manager=SimpleNamespace(list=remote_list, cancel=remote_cancel),
    )
    cancelled = await runtime_control._cancel_managed_background_jobs(
        runtime, before_agent_stop_epoch=1
    )
    assert cancelled == 1001
    remaining = manager.list(states=NONTERMINAL_STATES, limit=-1)
    assert {item.job_id for item in remaining} == {"job-1001", "job-1002"}
