"""Scoped scheduler decisions must remain bound to a live Run until admission."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import MethodType
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.function_worker_host import FunctionWorkerHost
from orchestrator.session_store import IdempotencyConflict, SessionConflict, SessionStore
from orchestrator.skill_manager import SchedulerJobChanged, scheduler_job_deletion_revision
from tests.test_scheduler_run_authorization import (
    active_run,
    credential,
    owner_decision,
    setup_personal,
)
from tests.test_fingerprint_admission import runtime_at
from tests.test_function_worker_supervisor import _Client, _Kernel, _handle
from tests.test_workbench_scheduler_api import _server


@pytest.mark.asyncio
async def test_cancel_between_approval_and_scheduler_admission_does_not_queue(tmp_path):
    server, runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    parent = active_run(server)
    reached_admission = asyncio.Event()
    resume = asyncio.Event()
    admitted = []

    runtime._run_job_now = MethodType(FlexibleAgentRuntime._run_job_now, runtime)
    runtime._primary_chat_id = lambda: 123

    async def enqueue_request(*, prompt, **kwargs):
        reached_admission.set()
        await resume.wait()
        session = server.session_store.ensure_agent_activity_session(
            owner_id=parent["owner_id"], agent_id="momo"
        )
        args = dict(
            session_id=session["session_id"], owner_id=parent["owner_id"],
            agent_id="momo", request_id="scheduler-child", text=prompt,
            source="scheduler", idempotency_key=kwargs.get("idempotency_key") or "scheduler-child",
        )
        if kwargs.get("operation_admission") is not None:
            args["operation_admission"] = kwargs["operation_admission"]
            args["parent_run_id"] = kwargs["request_metadata"]["parent_run_id"]
            scope = kwargs["operation_admission"]["scope"]
            args["operation_revision_guard"] = lambda: runtime.skill_manager.hold_owned_job_revision(
                scope["kind"], scope["job_id"], agent_name="momo",
                expected_revision=scope["expected_revision"],
            )
        accepted = server.session_store.accept_run(**args)
        admitted.append(accepted.run_id)
        return accepted.request_id

    runtime.enqueue_request = enqueue_request
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        url = base + "/api/agents/momo/jobs/run"
        token = credential(server, base, parent, tools=["hashi_scheduler_rerun"])
        headers = {"X-Hashi-Scheduler-Token": token}
        payload = {"kind": "cron", "job_id": "daily-report"}
        async with client.post(url, json=payload, headers=headers) as pending:
            assert pending.status == 403, await pending.text()
            approval_id = (await pending.json())["approval"]["approval_id"]
        await owner_decision(client, base, server, approval_id)
        request = asyncio.create_task(client.post(url, json=payload, headers=headers))
        await asyncio.wait_for(reached_admission.wait(), timeout=10)
        server.session_store.cancel_run(parent["run_id"], owner_id=parent["owner_id"])
        resume.set()
        async with await request as result:
            assert result.status != 200, await result.text()
        assert admitted == []


def test_scheduler_decision_admission_is_atomic_and_idempotent(tmp_path):
    server, runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    binding = active_run(server)
    store = server.session_store
    job = runtime.skill_manager.get_job("cron", "daily-report")
    scope = {"operation": "activity.scheduler.run", "agent_id": "momo",
             "kind": "cron", "job_id": job["id"],
             "expected_revision": scheduler_job_deletion_revision(job)}
    with store.hold_operation_approval(binding=binding, scope=scope, consume=False) as pending:
        assert not pending["authorized"]
    approval_id = pending["approval_id"]
    store.decide_approval(approval_id=approval_id, owner_id=binding["owner_id"],
                          decision="approved")
    admission = {"binding": binding, "scope": scope, "approval_id": approval_id}
    session = store.ensure_agent_activity_session(owner_id=binding["owner_id"], agent_id="momo")
    args = dict(session_id=session["session_id"], owner_id=binding["owner_id"], agent_id="momo",
                request_id="child-request", text=job["prompt"], source="scheduler",
                idempotency_key=f"pao-scheduler:{approval_id}",
                parent_run_id=binding["run_id"], operation_admission=admission,
                operation_revision_guard=lambda: runtime.skill_manager.hold_owned_job_revision(
                    "cron", job["id"], agent_name="momo",
                    expected_revision=scope["expected_revision"],
                ))

    other = store.ensure_agent_activity_session(owner_id="other-user", agent_id="momo")
    with pytest.raises(SessionConflict):
        store.accept_run(**{**args, "session_id": other["session_id"], "owner_id": "other-user"})
    with store.hold_operation_approval(binding=binding, scope=scope, consume=False) as still_approved:
        assert still_approved["authorized"]

    second = SessionStore(store.db_path, instance_id=store.instance_id,
                          attachment_root=store.attachment_files_root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda candidate: candidate.accept_run(**args), (store, second)))
    assert results[0].run_id == results[1].run_id
    assert sorted(result.replayed for result in results) == [False, True]
    with pytest.raises(IdempotencyConflict):
        store.accept_run(**{**args, "text": "a different task"})


def test_scheduler_admission_failure_rolls_back_approval_and_child(tmp_path, monkeypatch):
    server, runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    binding = active_run(server)
    store = server.session_store
    job = runtime.skill_manager.get_job("cron", "daily-report")
    scope = {"operation": "activity.scheduler.run", "agent_id": "momo", "kind": "cron",
             "job_id": job["id"], "expected_revision": scheduler_job_deletion_revision(job)}
    with store.hold_operation_approval(binding=binding, scope=scope, consume=False) as pending:
        approval_id = pending["approval_id"]
    store.decide_approval(approval_id=approval_id, owner_id=binding["owner_id"],
                          decision="approved")
    session = store.ensure_agent_activity_session(owner_id=binding["owner_id"], agent_id="momo")
    args = dict(session_id=session["session_id"], owner_id=binding["owner_id"], agent_id="momo",
                request_id="rollback-child", text=job["prompt"], source="scheduler",
                idempotency_key=f"pao-scheduler:{approval_id}", parent_run_id=binding["run_id"],
                operation_admission={"binding": binding, "scope": scope, "approval_id": approval_id},
                operation_revision_guard=lambda: runtime.skill_manager.hold_owned_job_revision(
                    "cron", job["id"], agent_name="momo",
                    expected_revision=scope["expected_revision"],
                ))
    original = store._append_event

    def fail_after_insert(connection, **event):
        if event.get("kind") == "run.accepted":
            raise RuntimeError("fixture disk failure")
        return original(connection, **event)

    monkeypatch.setattr(store, "_append_event", fail_after_insert)
    with pytest.raises(RuntimeError, match="fixture disk failure"):
        store.accept_run(**args)
    monkeypatch.setattr(store, "_append_event", original)
    with store.hold_operation_approval(binding=binding, scope=scope, consume=False) as decision:
        assert decision["authorized"]
    assert store.find_run_by_idempotency(
        session_id=session["session_id"], owner_id=binding["owner_id"],
        idempotency_key=args["idempotency_key"],
    ) is None
    assert not store.accept_run(**args).replayed


def test_task_edit_waits_until_approved_child_commit(tmp_path, monkeypatch):
    server, runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    binding = active_run(server)
    store = server.session_store
    manager = runtime.skill_manager
    job = manager.get_job("cron", "daily-report")
    scope = {"operation": "activity.scheduler.run", "agent_id": "momo", "kind": "cron",
             "job_id": job["id"], "expected_revision": scheduler_job_deletion_revision(job)}
    with store.hold_operation_approval(binding=binding, scope=scope, consume=False) as pending:
        approval_id = pending["approval_id"]
    store.decide_approval(approval_id=approval_id, owner_id=binding["owner_id"],
                          decision="approved")
    session = store.ensure_agent_activity_session(owner_id=binding["owner_id"], agent_id="momo")
    args = dict(session_id=session["session_id"], owner_id=binding["owner_id"], agent_id="momo",
                request_id="serialized-child", text=job["prompt"], source="scheduler",
                idempotency_key=f"pao-scheduler:{approval_id}", parent_run_id=binding["run_id"],
                operation_admission={"binding": binding, "scope": scope, "approval_id": approval_id},
                operation_revision_guard=lambda: manager.hold_owned_job_revision(
                    "cron", job["id"], agent_name="momo",
                    expected_revision=scope["expected_revision"],
                ))
    reached, release, writer_started, writer_done = (threading.Event() for _ in range(4))
    original = store._append_event

    def delayed_commit(connection, **event):
        if event.get("kind") == "run.accepted":
            reached.set()
            if not release.wait(10):
                raise TimeoutError("test did not release commit")
        return original(connection, **event)

    monkeypatch.setattr(store, "_append_event", delayed_commit)

    def edit():
        tasks = manager._load_tasks()
        tasks["crons"][0]["prompt"] = "modified after child admission"
        writer_started.set()
        manager._save_tasks(tasks)
        writer_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        child = pool.submit(store.accept_run, **args)
        try:
            assert reached.wait(10)
            update = pool.submit(edit)
            assert writer_started.wait(10)
            assert not writer_done.wait(0.2)
        finally:
            release.set()
        assert not child.result(timeout=10).replayed
        update.result(timeout=10)
    assert writer_done.is_set()
    assert manager.get_job("cron", job["id"])["prompt"] == "modified after child admission"


@pytest.mark.parametrize("location", ["tasks", "active"])
def test_heartbeat_revision_guard_handles_both_stores_and_rejects_duplicates(tmp_path, location):
    _server_instance, runtime = _server(tmp_path, persistent_session_v1=True)
    manager = runtime.skill_manager
    job = {"id": "heartbeat-fixture", "agent": "momo", "enabled": True,
           "interval_seconds": 600, "prompt": "fixture"}
    if location == "tasks":
        tasks = manager._load_tasks()
        tasks["heartbeats"].append(job)
        manager._save_tasks(tasks)
    else:
        manager._save_active_heartbeats([job])
    expected = scheduler_job_deletion_revision(job)
    with manager.hold_owned_job_revision(
        "heartbeat", job["id"], agent_name="momo", expected_revision=expected
    ):
        pass
    if location == "tasks":
        manager._save_active_heartbeats([job])
    else:
        tasks = manager._load_tasks()
        tasks["heartbeats"].append(job)
        manager._save_tasks(tasks)
    with pytest.raises(SchedulerJobChanged):
        with manager.hold_owned_job_revision(
            "heartbeat", job["id"], agent_name="momo", expected_revision=expected
        ):
            pass


@pytest.mark.asyncio
async def test_changed_job_cannot_reuse_approval(tmp_path):
    server, fixture = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, fixture)
    binding = active_run(server)
    job = fixture.skill_manager.get_job("cron", "daily-report")
    admission = {
        "binding": binding, "approval_id": "fixture-only",
        "scope": {"operation": "activity.scheduler.run", "agent_id": "momo", "kind": "cron",
                  "job_id": job["id"], "expected_revision": scheduler_job_deletion_revision(job)},
    }
    runtime = SimpleNamespace(name="momo", enqueue_request=AsyncMock())
    ok, message = await FlexibleAgentRuntime._run_job_now(
        runtime, {**job, "prompt": "a different task"}, kind="cron",
        operation_admission=admission,
    )
    assert not ok and "no longer matches" in message
    runtime.enqueue_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_runtime_admits_one_owner_bound_child_and_replays(tmp_path):
    server, fixture = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, fixture)
    binding = active_run(server)
    job = fixture.skill_manager.get_job("cron", "daily-report")
    scope = {"operation": "activity.scheduler.run", "agent_id": "momo", "kind": "cron",
             "job_id": job["id"], "expected_revision": scheduler_job_deletion_revision(job)}
    with server.session_store.hold_operation_approval(
        binding=binding, scope=scope, consume=False
    ) as pending:
        approval_id = pending["approval_id"]
    server.session_store.decide_approval(
        approval_id=approval_id, owner_id=binding["owner_id"], decision="approved"
    )
    runtime = runtime_at(tmp_path, False, instance_id=server.session_store.instance_id, agent="momo")
    runtime.session_store = server.session_store
    runtime.skill_manager = fixture.skill_manager
    runtime._primary_chat_id = lambda: 123
    runtime._run_job_now = MethodType(FlexibleAgentRuntime._run_job_now, runtime)
    admission = {"binding": binding, "scope": scope, "approval_id": approval_id}
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.runtime, host.phase, host.accepting = runtime, "ACTIVE", True
    async def responder(method, params):
        return await host.handle_request(method, json.loads(json.dumps(params)))
    client = _Client("momo", 101, responder=responder)
    handle = _handle(_Kernel(), client)

    for _ in range(2):
        ok, message = await handle._run_job_now(job, kind="cron", operation_admission=admission)
        assert ok, message
    assert client.calls == ["runtime.run_job_now_scoped", "runtime.run_job_now_scoped"]
    assert runtime.queue.qsize() == 1
    child = runtime.queue.get_nowait()
    run = server.session_store.get_run(child.run_id, owner_id=binding["owner_id"])
    assert run["parent_run_id"] == binding["run_id"]


@pytest.mark.asyncio
async def test_real_runtime_rechecks_parent_at_transaction_after_cancel(tmp_path, monkeypatch):
    from orchestrator import runtime_session

    server, fixture = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, fixture)
    binding = active_run(server)
    job = fixture.skill_manager.get_job("cron", "daily-report")
    scope = {"operation": "activity.scheduler.run", "agent_id": "momo", "kind": "cron",
             "job_id": job["id"], "expected_revision": scheduler_job_deletion_revision(job)}
    with server.session_store.hold_operation_approval(
        binding=binding, scope=scope, consume=False
    ) as pending:
        approval_id = pending["approval_id"]
    server.session_store.decide_approval(
        approval_id=approval_id, owner_id=binding["owner_id"], decision="approved"
    )
    runtime = runtime_at(tmp_path, False, instance_id=server.session_store.instance_id, agent="momo")
    runtime.session_store = server.session_store
    runtime.skill_manager = fixture.skill_manager
    runtime._primary_chat_id = lambda: 123
    admission = {"binding": binding, "scope": scope, "approval_id": approval_id}
    reached, release = threading.Event(), threading.Event()
    original = runtime_session.accept_request

    def delayed(*args, **kwargs):
        reached.set()
        if not release.wait(10):
            raise TimeoutError("test did not release admission")
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime_session, "accept_request", delayed)
    task = asyncio.create_task(FlexibleAgentRuntime._run_job_now(
        runtime, job, kind="cron", operation_admission=admission
    ))
    try:
        assert await asyncio.to_thread(reached.wait, 10)
        server.session_store.cancel_run(binding["run_id"], owner_id=binding["owner_id"])
    finally:
        release.set()
    ok, message = await task
    assert not ok and "stopped" in message
    assert runtime.queue.empty()


@pytest.mark.asyncio
async def test_changed_persisted_job_is_not_admitted_after_approval(tmp_path):
    server, fixture = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, fixture)
    binding = active_run(server)
    job = fixture.skill_manager.get_job("cron", "daily-report")
    scope = {"operation": "activity.scheduler.run", "agent_id": "momo", "kind": "cron",
             "job_id": job["id"], "expected_revision": scheduler_job_deletion_revision(job)}
    with server.session_store.hold_operation_approval(
        binding=binding, scope=scope, consume=False
    ) as pending:
        approval_id = pending["approval_id"]
    server.session_store.decide_approval(
        approval_id=approval_id, owner_id=binding["owner_id"], decision="approved"
    )
    changed = fixture.skill_manager._load_tasks()
    changed["crons"][0]["prompt"] = "changed after approval"
    fixture.skill_manager._save_tasks(changed)
    runtime = runtime_at(tmp_path, False, instance_id=server.session_store.instance_id, agent="momo")
    runtime.session_store = server.session_store
    runtime.skill_manager = fixture.skill_manager
    runtime._primary_chat_id = lambda: 123

    ok, _message = await FlexibleAgentRuntime._run_job_now(
        runtime, job, kind="cron",
        operation_admission={"binding": binding, "scope": scope, "approval_id": approval_id},
    )
    assert not ok
    assert runtime.queue.empty()
    with server.session_store.hold_operation_approval(
        binding=binding, scope=scope, consume=False
    ) as decision:
        assert decision["authorized"]


@pytest.mark.asyncio
async def test_legacy_worker_cannot_ignore_scoped_admission():
    effects = []

    async def legacy_responder(method, params):
        if method == "runtime.run_job_now":
            # The old Worker accepts this method but ignores unfamiliar fields.
            effects.append(params["job"])
            return [True, "queued"]
        raise RuntimeError("unknown Worker method")

    handle = _handle(_Kernel(), _Client("momo", 101, responder=legacy_responder))
    with pytest.raises(RuntimeError, match="unknown Worker method"):
        await handle._run_job_now(
            {"id": "job", "agent": "momo", "prompt": "fixture"},
            kind="cron",
            operation_admission={"approval_id": "fixture"},
        )
    assert effects == []


@pytest.mark.asyncio
async def test_http_retry_returns_the_same_real_worker_child(tmp_path):
    server, fixture = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, fixture)
    parent = active_run(server)
    runtime = runtime_at(tmp_path, False, instance_id=server.session_store.instance_id, agent="momo")
    runtime.session_store = server.session_store
    runtime.skill_manager = fixture.skill_manager
    runtime._primary_chat_id = lambda: 123
    runtime._run_job_now = MethodType(FlexibleAgentRuntime._run_job_now, runtime)
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.runtime, host.phase, host.accepting = runtime, "ACTIVE", True

    async def responder(method, params):
        return await host.handle_request(method, json.loads(json.dumps(params)))

    kernel = _Kernel()
    kernel.skill_manager, kernel.global_cfg = fixture.skill_manager, server.global_config
    handle = _handle(kernel, _Client("momo", 101, responder=responder))
    server.orchestrator.runtimes = [handle]
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        url = base + "/api/agents/momo/jobs/run"
        token = credential(server, base, parent, tools=["hashi_scheduler_rerun"])
        headers = {"X-Hashi-Scheduler-Token": token}
        payload = {"kind": "cron", "job_id": "daily-report"}
        async with client.post(url, json=payload, headers=headers) as pending:
            assert pending.status == 403, await pending.text()
            approval_id = (await pending.json())["approval"]["approval_id"]
        await owner_decision(client, base, server, approval_id)
        for _ in range(2):
            async with client.post(url, json=payload, headers=headers) as response:
                assert response.status == 200, await response.text()
        assert runtime.queue.qsize() == 1
        child = runtime.queue.get_nowait()
        assert server.session_store.get_run(child.run_id)["parent_run_id"] == parent["run_id"]
        server.session_store.cancel_run(child.run_id, owner_id=parent["owner_id"])
        async with client.post(url, json=payload, headers=headers) as child_stopped:
            assert child_stopped.status == 409, await child_stopped.text()
            assert "stopped" in (await child_stopped.json())["message"]
        assert runtime.queue.empty()
        server.session_store.cancel_run(parent["run_id"], owner_id=parent["owner_id"])
        async with client.post(url, json=payload, headers=headers) as stopped:
            assert stopped.status in {401, 403}, await stopped.text()
        assert runtime.queue.empty()


@pytest.mark.asyncio
async def test_consumed_legacy_decision_without_receipt_does_not_rerun(tmp_path):
    server, fixture = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, fixture)
    binding = active_run(server)
    job = fixture.skill_manager.get_job("cron", "daily-report")
    scope = {"operation": "activity.scheduler.run", "agent_id": "momo", "kind": "cron",
             "job_id": job["id"], "expected_revision": scheduler_job_deletion_revision(job)}
    with server.session_store.hold_operation_approval(
        binding=binding, scope=scope, consume=False
    ) as pending:
        approval_id = pending["approval_id"]
    server.session_store.decide_approval(
        approval_id=approval_id, owner_id=binding["owner_id"], decision="approved"
    )
    # Model a prior unknown legacy dispatch which consumed its decision without
    # atomically admitting a canonical child receipt.
    with server.session_store.hold_operation_approval(binding=binding, scope=scope) as used:
        assert used["authorized"]
    runtime = runtime_at(tmp_path, False, instance_id=server.session_store.instance_id, agent="momo")
    runtime.session_store = server.session_store
    runtime.skill_manager = fixture.skill_manager
    runtime._primary_chat_id = lambda: 123
    ok, message = await FlexibleAgentRuntime._run_job_now(
        runtime, job, kind="cron",
        operation_admission={"binding": binding, "scope": scope, "approval_id": approval_id},
    )
    assert not ok and "no matching task receipt" in message
    assert runtime.queue.empty()
