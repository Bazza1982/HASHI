"""HTTP authorization boundary with private personal/L0 Agents and disposable state."""
from __future__ import annotations

import json

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from tests.test_workbench_scheduler_api import _server
from tests.test_scheduler_run_authorization import active_run, credential, setup_personal


OPERATIONS = {
    "create": ("POST", "/scheduler/jobs", "hashi_scheduler_create",
               {"kind": "cron", "task_id": "disposable", "schedule": "0 0 * * *",
                "prompt": "private test", "enabled": False}),
    "update": ("PATCH", "/scheduler/jobs/daily-report", "hashi_scheduler_update",
               {"kind": "cron", "prompt": "unauthorized replacement"}),
    "run": ("POST", "/jobs/run", "hashi_scheduler_rerun",
            {"kind": "cron", "job_id": "daily-report", "authorization": "explicit_user_authorization"}),
    "recovery": ("POST", "/scheduler/recovery/batch-1/resolve", "hashi_scheduler_recovery_resolve",
                 {"action": "skip"}),
    "loop_create": ("POST", "/superloops", "hashi_superloop_create", {"goal": "private test goal"}),
    "loop_update": ("PATCH", "/superloops/{loop}", "hashi_superloop_update",
                    {"action": "task_add", "title": "private next task"}),
    "loop_delete": ("DELETE", "/superloops/{loop}", "hashi_superloop_delete",
                    {"authorization": "explicit_user_authorization"}),
}


def fixture_state(tmp_path):
    server, runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    loop = server._superloop_service("momo").create_quickstart("disposable owner loop")["loop_id"]
    foreign = server._superloop_service("lily").create_quickstart("disposable foreign loop")["loop_id"]
    return server, runtime, loop, foreign


def snapshot(tmp_path, runtime):
    paths = [tmp_path / "tasks.json"]
    paths += list((tmp_path / "superloops").rglob("*"))
    return ({str(p.relative_to(tmp_path)): p.read_bytes() for p in paths if p.is_file()},
            json.dumps(runtime.reruns, sort_keys=True),
            [(r["agent_name"], r["batch_id"], r["action"]) for r in runtime.recovery_resolutions])


async def mutate(client, base, operation, loop, *, headers=None, body=None, agent="momo"):
    method, suffix, _, payload = OPERATIONS[operation]
    payload = {**payload, "requested_by": "hashi_tool_gateway", **(body or {})}
    async with client.request(method, base + "/api/agents/" + agent + suffix.format(loop=loop),
                              json=payload, headers=headers or {}) as response:
        return response.status, await response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", list(OPERATIONS))
@pytest.mark.parametrize("caller", ["anonymous", "stopped_run"])
async def test_untrusted_http_mutations_leave_disposable_state_unchanged(tmp_path, operation, caller):
    server, runtime, loop, _ = fixture_state(tmp_path)
    binding = active_run(server)
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        headers = {}
        if caller == "stopped_run":
            token = credential(server, base, binding, tools=[OPERATIONS[operation][2]])
            server.session_store.cancel_run(binding["run_id"], owner_id=binding["owner_id"])
            headers = {"X-Hashi-Scheduler-Token": token}
        before = snapshot(tmp_path, runtime)
        status, payload = await mutate(client, base, operation, loop, headers=headers)
        after = snapshot(tmp_path, runtime)
        assert status in {401, 403} and after == before, {
            "status": status, "response": payload, "state_changed": after != before}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["run", "loop_delete"])
async def test_live_agent_cannot_forge_policy_required_approval(tmp_path, operation):
    server, runtime, loop, _ = fixture_state(tmp_path)
    binding = active_run(server)
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        token = credential(server, base, binding, tools=[OPERATIONS[operation][2]])
        before = snapshot(tmp_path, runtime)
        status, payload = await mutate(client, base, operation, loop,
                                       headers={"X-Hashi-Scheduler-Token": token})
        assert status == 403 and snapshot(tmp_path, runtime) == before, payload


async def approve(client, base, server, pending):
    assert pending["error_code"] == "approval_required"
    async with client.post(base + pending["decision_path"], json={"decision": "approved"},
                           headers={"X-Workbench-Token": server.admin_token}) as response:
        assert response.status == 200, await response.text()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", list(OPERATIONS))
async def test_authenticated_owner_keeps_normal_management_access(tmp_path, operation):
    server, runtime, loop, _ = fixture_state(tmp_path)
    before = snapshot(tmp_path, runtime)
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        status, result = await mutate(client, base, operation, loop,
                                      headers={"X-Workbench-Token": server.admin_token})
        assert status in {200, 201}, result
    assert snapshot(tmp_path, runtime) != before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", list(OPERATIONS))
@pytest.mark.parametrize("gateway", [False, True])
async def test_live_agent_and_serialized_gateway_keep_scoped_access(tmp_path, operation, gateway):
    from tools.registry import ToolRegistry
    from tools.gateway.context import write_gateway_context, load_gateway_context

    server, runtime, loop, _ = fixture_state(tmp_path)
    binding = active_run(server)
    method, _, tool, body = OPERATIONS[operation]
    args = dict(body)
    if operation.startswith("loop_") and operation != "loop_create":
        args["loop_id"] = loop
    if operation == "create":
        args["task_id"] = "disposable"
    if operation == "update":
        args["job_id"] = "daily-report"
    if operation == "recovery":
        args["batch_id"] = "batch-1"
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        if operation in {"run", "loop_delete"}:
            token = credential(server, base, binding, tools=[tool])
            status, pending = await mutate(client, base, operation, loop,
                                           headers={"X-Hashi-Scheduler-Token": token})
            assert status == 403
            await approve(client, base, server, pending)
        registry = ToolRegistry(
            allowed_tools=[value[2] for value in OPERATIONS.values()],
            access_root=tmp_path, workspace_dir=runtime.workspace_dir,
            secrets={"workbench_admin_token": server.admin_token},
            audit_context={"agent_name": "momo", "global_config": server.global_config,
                           "instance_id": server.global_config.instance_id,
                           "request_id": binding["request_id"], "hashi_run_id": binding["run_id"],
                           "hashi_fencing_token": binding["fencing_token"],
                           "workbench_api_base_url": base},
        )
        if gateway:
            context = tmp_path / "scoped-gateway.json"
            write_gateway_context(registry, context, backend="codex", workbench_api_base_url=base)
            assert server.admin_token not in context.read_text(encoding="utf-8")
            registry = load_gateway_context(context).build_registry()
        before = snapshot(tmp_path, runtime)
        result = await registry.execute(tool, args)
        assert not result.is_error, result.output
        assert snapshot(tmp_path, runtime) != before
        server.session_store.cancel_run(binding["run_id"], owner_id=binding["owner_id"])
        before = snapshot(tmp_path, runtime)
        replay = await registry.execute(tool, args)
        assert replay.is_error
        assert snapshot(tmp_path, runtime) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", list(OPERATIONS))
async def test_scoped_capability_rejects_mismatched_identity_and_tool(tmp_path, operation):
    server, runtime, loop, _ = fixture_state(tmp_path)
    binding = active_run(server)
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        tool = OPERATIONS[operation][2]
        for overrides in [
            {"agent_id": "lily"}, {"instance_id": "OTHER"},
            {"audience": "http://127.0.0.1:1"}, {"tools": ["hashi_scheduler_list"]},
            {"now": 0}, {"fencing_token": binding["fencing_token"] + 1},
        ]:
            token = credential(server, base, binding, tools=[tool], **{
                key: value for key, value in overrides.items() if key != "tools"}) if "tools" not in overrides else credential(
                    server, base, binding, **overrides)
            before = snapshot(tmp_path, runtime)
            status, _ = await mutate(client, base, operation, loop,
                                     headers={"X-Hashi-Scheduler-Token": token})
            assert status == 401
            assert snapshot(tmp_path, runtime) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update", "run", "loop_update", "loop_delete"])
async def test_scoped_mutation_never_takes_another_agents_work(tmp_path, operation):
    server, runtime, loop, foreign = fixture_state(tmp_path)
    binding = active_run(server)
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        token = credential(server, base, binding, tools=[OPERATIONS[operation][2]])
        method, suffix, _, body = OPERATIONS[operation]
        body = {**body, "requested_by": "hashi_tool_gateway"}
        if operation == "create":
            body["task_id"] = "other-agent"
        elif operation in {"update", "run"}:
            suffix = suffix.replace("daily-report", "other-agent")
            body["job_id"] = "other-agent"
        before = snapshot(tmp_path, runtime)
        async with client.request(method, base + "/api/agents/momo" + suffix.format(loop=foreign),
                                  json=body, headers={"X-Hashi-Scheduler-Token": token}) as response:
            assert response.status in {400, 403, 404}
        assert snapshot(tmp_path, runtime) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", list(OPERATIONS))
async def test_run_stop_during_http_body_read_prevents_the_effect(tmp_path, operation, monkeypatch):
    import asyncio
    import orchestrator.workbench_api as api

    server, runtime, loop, _ = fixture_state(tmp_path)
    binding = active_run(server)
    authenticated, release = asyncio.Event(), asyncio.Event()
    original = api.mutation_authority

    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        authenticated.set()
        return result

    monkeypatch.setattr(api, "mutation_authority", observed)
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        method, suffix, tool, body = OPERATIONS[operation]
        token = credential(server, base, binding, tools=[tool])
        encoded = json.dumps({**body, "requested_by": "hashi_tool_gateway"}).encode()
        async def chunks():
            yield encoded[:1]
            await release.wait()
            yield encoded[1:]
        before = snapshot(tmp_path, runtime)
        request = asyncio.create_task(client.request(method,
            base + "/api/agents/momo" + suffix.format(loop=loop), data=chunks(),
            headers={"Content-Type": "application/json", "X-Hashi-Scheduler-Token": token}))
        await asyncio.wait_for(authenticated.wait(), 5)
        server.session_store.cancel_run(binding["run_id"], owner_id=binding["owner_id"])
        release.set()
        response = await asyncio.wait_for(request, 5)
        async with response:
            assert response.status == 401, await response.text()
        assert snapshot(tmp_path, runtime) == before


@pytest.mark.asyncio
async def test_real_recovery_rechecks_run_after_waiting_for_its_lock(tmp_path):
    import asyncio
    from orchestrator.scheduler import TaskScheduler

    server, runtime, loop, _ = fixture_state(tmp_path)
    binding = active_run(server)
    scheduler = TaskScheduler(tasks_path=tmp_path/"tasks.json", state_path=tmp_path/"recovery.json",
                              runtimes=[runtime], authorized_id=123)
    scheduler.state["recovery_batches"] = server.orchestrator.scheduler.state["recovery_batches"]
    assert scheduler._save_state()
    server.orchestrator.scheduler = scheduler
    lock, waiting = scheduler._recovery_lock, asyncio.Event()

    class ObservedLock:
        async def __aenter__(self):
            waiting.set()
            await lock.acquire()
        async def __aexit__(self, *_):
            lock.release()

    await lock.acquire()
    scheduler._recovery_lock = ObservedLock()
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        token = credential(server, base, binding, tools=["hashi_scheduler_recovery_resolve"])
        before = (tmp_path/"recovery.json").read_bytes()
        request = asyncio.create_task(mutate(client, base, "recovery", loop,
                                             headers={"X-Hashi-Scheduler-Token": token}))
        await asyncio.wait_for(waiting.wait(), 5)
        server.session_store.cancel_run(binding["run_id"], owner_id=binding["owner_id"])
        lock.release()
        status, _ = await asyncio.wait_for(request, 5)
        assert status == 401
        assert (tmp_path/"recovery.json").read_bytes() == before
        assert scheduler.state["recovery_batches"]["batch-1"]["status"] == "pending"




@pytest.mark.asyncio
async def test_recovery_stops_between_dispatches_and_never_replays_accepted_work(tmp_path, monkeypatch):
    from orchestrator.scheduler import TaskScheduler

    server, runtime, loop, _ = fixture_state(tmp_path)
    binding = active_run(server)
    scheduler = TaskScheduler(tasks_path=tmp_path/"tasks.json", state_path=tmp_path/"recovery.json",
                              runtimes=[runtime], authorized_id=123)
    scheduler.state["recovery_batches"] = server.orchestrator.scheduler.state["recovery_batches"]
    batch = scheduler.state["recovery_batches"]["batch-1"]
    batch["items"][0]["replay_limit"] = 2
    assert scheduler._save_state()
    server.orchestrator.scheduler = scheduler
    dispatched = []

    async def enqueue_request(**kwargs):
        dispatched.append(kwargs)
        # The first admitted child is independent; stop prevents the next dispatch.
        server.session_store.cancel_run(binding["run_id"], owner_id=binding["owner_id"])
        return True

    monkeypatch.setattr(runtime, "enqueue_request", enqueue_request, raising=False)
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        token = credential(server, base, binding, tools=["hashi_scheduler_recovery_resolve"])
        status, _ = await mutate(client, base, "recovery", loop, body={"action": "rerun_all"},
                                  headers={"X-Hashi-Scheduler-Token": token})
        assert status == 401
        assert len(dispatched) == 1
        persisted = json.loads((tmp_path/"recovery.json").read_text(encoding="utf-8"))
        resolved = persisted["recovery_batches"]["batch-1"]
        assert resolved["status"] == "resolved"
        assert resolved["resolution"]["executed_total"] == 1
        assert resolved["resolution"]["skipped_total"] == 1
        assert resolved["resolution"]["reason"] == "originating_run_stopped"
        before = (tmp_path/"recovery.json").read_bytes()
        # A legitimate owner cannot accidentally re-dispatch an acknowledged occurrence.
        status, result = await mutate(client, base, "recovery", loop, body={"action": "rerun_all"},
                                      headers={"X-Workbench-Token": server.admin_token})
        assert status == 200 and result["state_changed"] is False
        assert len(dispatched) == 1
        assert (tmp_path/"recovery.json").read_bytes() == before


@pytest.mark.asyncio
async def test_scoped_run_admission_never_holds_sqlite_across_worker_await(tmp_path, monkeypatch):
    import asyncio

    server, runtime, loop, _ = fixture_state(tmp_path)
    binding = active_run(server)

    async def queue_child(job, *, kind):
        # Real Run admission writes through another SessionStore connection.
        child = await asyncio.wait_for(asyncio.to_thread(
            server.session_store.accept_run, session_id=binding["session_id"],
            owner_id=binding["owner_id"], agent_id=binding["agent_id"],
            request_id="disposable-scheduler-child", text="private child",
            source="scheduler", idempotency_key="disposable-scheduler-child",
        ), 3)
        runtime.reruns.append(child.run_id)
        return True, "Queued independent child"

    monkeypatch.setattr(runtime, "_run_job_now", queue_child)
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        token = credential(server, base, binding, tools=["hashi_scheduler_rerun"])
        headers = {"X-Hashi-Scheduler-Token": token}
        status, pending = await mutate(client, base, "run", loop, headers=headers)
        assert status == 403
        await approve(client, base, server, pending)
        status, result = await mutate(client, base, "run", loop, headers=headers)
        assert status == 200, result
        assert len(runtime.reruns) == 1
