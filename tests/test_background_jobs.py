from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.background_jobs import BackgroundJobManager, BackgroundJobStore, NONTERMINAL_STATES
from orchestrator.service_manager import ServiceManager
from tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_background_job_records_success_and_logs(tmp_path: Path):
    manager = BackgroundJobManager(tmp_path / "background_jobs")
    await manager.start()

    record = await manager.start_job(
        agent="zelda",
        cwd=tmp_path,
        argv=[sys.executable, "-c", "print('hello background')"],
        origin={"chat_id": 1, "request_id": "req-test"},
    )
    task = manager._monitor_tasks[record.job_id]
    await task

    saved = manager.get(record.job_id)
    assert saved is not None
    assert saved.state == "succeeded"
    assert saved.returncode == 0
    assert "hello background" in manager.tail(record.job_id)
    assert Path(saved.logs["stdout_path"]).exists()


@pytest.mark.asyncio
async def test_background_job_tail_streams_while_running(tmp_path: Path):
    manager = BackgroundJobManager(tmp_path / "background_jobs")
    await manager.start()

    record = await manager.start_job(
        agent="zelda",
        cwd=tmp_path,
        argv=[sys.executable, "-c", "import time; print('live heartbeat', flush=True); time.sleep(30)"],
        notify_on_failure=False,
    )

    try:
        for _ in range(30):
            if "live heartbeat" in manager.tail(record.job_id):
                break
            await asyncio.sleep(0.1)
        assert "live heartbeat" in manager.tail(record.job_id)
    finally:
        await manager.cancel(record.job_id, grace_seconds=0.1)


@pytest.mark.asyncio
async def test_background_job_records_failure(tmp_path: Path):
    manager = BackgroundJobManager(tmp_path / "background_jobs")
    await manager.start()

    record = await manager.start_job(
        agent="zelda",
        cwd=tmp_path,
        argv=[sys.executable, "-c", "import sys; print('bad'); sys.exit(7)"],
        notify_on_failure=False,
    )
    await manager._monitor_tasks[record.job_id]

    saved = manager.get(record.job_id)
    assert saved is not None
    assert saved.state == "failed"
    assert saved.returncode == 7
    assert saved.error == "process exited with 7"


@pytest.mark.asyncio
async def test_background_job_cancel_terminates_process(tmp_path: Path):
    manager = BackgroundJobManager(tmp_path / "background_jobs")
    await manager.start()

    record = await manager.start_job(
        agent="zelda",
        cwd=tmp_path,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        notify_on_failure=False,
    )

    cancelled = await manager.cancel(record.job_id, grace_seconds=0.1)

    assert cancelled.state == "cancelled"
    assert manager.get(record.job_id).state == "cancelled"


def test_background_job_store_recovery_marks_nonterminal_abandoned(tmp_path: Path):
    store = BackgroundJobStore(tmp_path / "jobs.db")
    now = "2026-07-06T00:00:00+00:00"
    for state in sorted(NONTERMINAL_STATES):
        store.create(
            SimpleNamespace(
                job_id=f"job-{state}",
                state=state,
                agent="zelda",
                command={},
                origin={},
                policy={},
                process={"pid": 123},
                logs={},
                notification={},
                created_at=now,
                updated_at=now,
                ended_at=None,
                returncode=None,
                error=None,
            )
        )

    recovered = store.recover_nonterminal(reason="test_recovery")

    assert len(recovered) == len(NONTERMINAL_STATES)
    assert all(item.state == "abandoned_after_restart" for item in recovered)
    assert all(item.error == "test_recovery" for item in recovered)


@pytest.mark.asyncio
async def test_service_manager_starts_and_restarts_background_jobs(tmp_path: Path):
    kernel = SimpleNamespace(
        paths=SimpleNamespace(bridge_home=tmp_path),
        background_job_manager=None,
    )
    manager = ServiceManager(kernel)

    first = await manager.start_background_jobs()
    assert kernel.background_job_manager is first
    assert (tmp_path / "state" / "background_jobs" / "jobs.db").exists()

    second = await manager.restart_background_jobs()
    assert kernel.background_job_manager is second
    assert second is not first

    await manager.stop_background_jobs()
    assert kernel.background_job_manager is None


@pytest.mark.asyncio
async def test_background_job_tool_uses_live_manager_and_notifies(tmp_path: Path):
    sent: list[dict] = []

    async def send_long_message(**kwargs):
        sent.append(kwargs)
        return 0.0, 1

    runtime = SimpleNamespace(
        name="zelda",
        current_request_meta={
            "request_id": "req-bg-tool",
            "chat_id": 123,
            "source": "background:prompt",
            "summary": "Background smoke",
        },
        send_long_message=send_long_message,
    )
    kernel = SimpleNamespace(runtimes=[runtime], background_job_manager=None)
    runtime.orchestrator = kernel
    manager = BackgroundJobManager(tmp_path / "background_jobs", kernel=kernel)
    await manager.start()
    kernel.background_job_manager = manager

    registry = ToolRegistry(
        allowed_tools=["background_job_start", "background_job_status", "background_job_tail"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={
            "agent_name": "zelda",
            "workspace_dir": str(tmp_path),
            "safety_mode": "read_write",
            "_runtime": runtime,
            "request_id": "req-bg-tool",
            "chat_id": 123,
            "request_source": "background:prompt",
            "request_summary": "Background smoke",
        },
    )

    result = await registry.execute(
        "background_job_start",
        {"argv": [sys.executable, "-c", "print('tool background done')"], "cwd": "."},
        tool_call_id="call-bg-start",
    )

    assert result.is_error is False
    payload = json.loads(result.output)
    job_id = payload["job_id"]
    assert payload["state"] == "running"
    assert payload["follow_up"]["status"] == f"/bg status {job_id}"

    await manager._monitor_tasks[job_id]
    saved = manager.get(job_id)
    assert saved is not None
    assert saved.state == "succeeded"
    assert saved.notification["delivered"] is True
    assert sent and sent[0]["chat_id"] == 123
    assert sent[0]["request_id"] == "req-bg-tool"
    assert "tool background done" in manager.tail(job_id)
