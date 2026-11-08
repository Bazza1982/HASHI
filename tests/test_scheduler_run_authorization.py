"""Real personal/L0 Scheduler and PAO fixtures; no production services or credentials."""
from __future__ import annotations

import json
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from orchestrator.session_store import SessionStore
from orchestrator.skill_manager import scheduler_job_deletion_revision
from orchestrator.workbench_auth import issue_scheduler_tool_token
from tests.test_workbench_scheduler_api import _server
from tools.registry import ToolRegistry


def active_run(server, *, agent="momo", request="scheduler-p1-run"):
    store = server.session_store
    owner = SessionStore.owner_id_for(server.global_config)
    session = store.ensure_default_session(owner_id=owner, agent_id=agent)
    accepted = store.accept_run(
        session_id=session["session_id"], owner_id=owner, agent_id=agent,
        request_id=request, text="Disposable Scheduler test", source="test",
        idempotency_key=request,
    )
    fence = store.mark_request_running(request, worker_id="isolated-test-worker")
    return dict(run_id=accepted.run_id, owner_id=owner, agent_id=agent,
                session_id=session["session_id"], request_id=request,
                context_generation=session["context_generation"],
                fencing_token=fence, worker_id="isolated-test-worker")


def setup_personal(server, runtime):
    server.admin_token = "disposable-fixture-admin"
    server.global_config.instance_id = server.session_store.instance_id
    runtime.config = SimpleNamespace(privacy_level=0)


def deletion_scope(runtime, **changes):
    job = runtime.skill_manager.get_job("cron", "daily-report")
    scope = dict(operation="activity.scheduler.delete", agent_id="momo", kind="cron",
                 job_id="daily-report", expected_revision=scheduler_job_deletion_revision(job),
                 only_disabled=False)
    scope.update(changes)
    return scope


def credential(server, base, binding, **changes):
    args = dict(instance_id=server.global_config.instance_id, agent_id=binding["agent_id"],
                request_id=binding["request_id"], audience=base,
                tools=["hashi_scheduler_delete"])
    # Before the fix the issuer has only a request claim; keep the same red probe.
    import inspect
    if "run_id" in inspect.signature(issue_scheduler_tool_token).parameters:
        args.update(run_id=binding["run_id"], fencing_token=binding["fencing_token"])
    args.update(changes)
    return issue_scheduler_tool_token(server.admin_token, **args)


@pytest.mark.asyncio
async def test_forged_approval_string_cannot_delete_personal_l0_job(tmp_path):
    server, runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    binding = active_run(server)
    before = (tmp_path / "tasks.json").read_bytes()
    async with TestServer(server.app) as upstream:
        registry = ToolRegistry(
            allowed_tools=["hashi_scheduler_status", "hashi_scheduler_delete"],
            access_root=tmp_path, workspace_dir=runtime.workspace_dir,
            secrets={"workbench_admin_token": server.admin_token},
            audit_context={"agent_name": "momo", "global_config": server.global_config,
                           "request_id": binding["request_id"],
                           "hashi_run_id": binding["run_id"],
                           "hashi_fencing_token": binding["fencing_token"],
                           "workbench_api_base_url": str(upstream.make_url("")).rstrip("/")},
        )
        result = await registry.execute("hashi_scheduler_delete", {
            "job_id": "daily-report", "kind": "cron",
            "authorization": "explicit_user_authorization",
        })
        assert result.is_error, result.output
    assert (tmp_path / "tasks.json").read_bytes() == before


@pytest.mark.asyncio
async def test_stopped_run_cannot_replay_previously_approved_delete(tmp_path):
    server, runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    binding = active_run(server)
    scope = deletion_scope(runtime)
    approval = server.session_store.create_approval(
        run_id=binding["run_id"], owner_id=binding["owner_id"],
        fencing_token=binding["fencing_token"], scope=scope,
    )
    before = (tmp_path / "tasks.json").read_bytes()
    async with TestServer(server.app) as upstream:
        base = str(upstream.make_url("")).rstrip("/")
        token = credential(server, base, binding)
        async with aiohttp.ClientSession() as client:
            async with client.post(base + "/api/v1/approvals/" + approval["approval_id"],
                                   json={"decision": "approved"},
                                   headers={"X-Workbench-Token": server.admin_token}) as response:
                assert response.status == 200, await response.text()
            server.session_store.cancel_run(binding["run_id"], owner_id=binding["owner_id"])
            async with client.delete(base + "/api/agents/momo/scheduler/jobs/daily-report",
                                     json={**scope, "authorization": "explicit_user_authorization"},
                                     headers={"X-Hashi-Scheduler-Token": token}) as response:
                assert response.status in {401, 403}, await response.text()
    assert (tmp_path / "tasks.json").read_bytes() == before


async def owner_decision(client, base, server, approval_id, decision="approved"):
    async with client.post(base + "/api/v1/approvals/" + approval_id,
                           json={"decision": decision},
                           headers={"X-Workbench-Token": server.admin_token}) as response:
        assert response.status == 200, await response.text()


async def delete(client, base, token, scope, *, agent="momo"):
    async with client.delete(base + f"/api/agents/{agent}/scheduler/jobs/" + scope["job_id"],
                             json={**scope, "authorization": "explicit_user_authorization"},
                             headers={"X-Hashi-Scheduler-Token": token}) as response:
        return response.status, await response.json()


@pytest.mark.asyncio
async def test_pending_decision_is_idempotent_and_cannot_be_self_approved(tmp_path):
    server, runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    binding, scope = active_run(server), deletion_scope(runtime)
    before = (tmp_path / "tasks.json").read_bytes()
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        token = credential(server, base, binding)
        status, first = await delete(client, base, token, scope)
        assert status == 403 and first["error_code"] == "approval_required"
        assert first["approval"]["scope"] == scope
        approval_id = first["approval"]["approval_id"]
        for _ in range(2):
            status, again = await delete(client, base, token, scope)
            assert status == 403 and again["approval"]["approval_id"] == approval_id
        # No token, the scoped bearer, and model text all fail the trusted decision boundary.
        for headers in ({}, {"X-Hashi-Scheduler-Token": token},
                        {"Authorization": "Bearer explicit_user_authorization"}):
            async with client.post(base + first["decision_path"],
                                   json={"decision": "approved"}, headers=headers) as response:
                assert response.status == 401
        await owner_decision(client, base, server, approval_id, decision="denied")
        status, denied = await delete(client, base, token, scope)
        assert status == 403 and denied["approval"]["decision"] == "denied"
    assert (tmp_path / "tasks.json").read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancelled", "agent_stop", "revoked", "completed",
                                    "new_attempt", "new_generation"])
async def test_approved_scoped_delete_is_fenced_by_run_lifecycle(tmp_path, ending):
    server, runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    binding, scope = active_run(server), deletion_scope(runtime)
    before = (tmp_path / "tasks.json").read_bytes()
    store = server.session_store
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        token = credential(server, base, binding)
        status, pending = await delete(client, base, token, scope)
        assert status == 403
        await owner_decision(client, base, server, pending["approval"]["approval_id"])
        if ending in {"cancelled", "agent_stop"}:
            store.cancel_run(binding["run_id"], owner_id=binding["owner_id"], reason=ending)
        elif ending == "revoked":
            store.reconcile_incomplete_runs(agent_id="momo")
        elif ending == "completed":
            store.finish_request(binding["request_id"], success=True,
                                 fencing_token=binding["fencing_token"], assistant_text="done")
        else:
            # Isolated durable-state fault: the Run stays running but its authority changes.
            with store._lock, store._connection() as connection:
                if ending == "new_attempt":
                    connection.execute("UPDATE runs SET fencing_token=fencing_token+1,attempt=attempt+1 WHERE run_id=?",
                                       (binding["run_id"],))
                else:
                    connection.execute("UPDATE sessions SET context_generation=context_generation+1 WHERE session_id=?",
                                       (binding["session_id"],))
        status, _ = await delete(client, base, token, scope)
        assert status == 401
        # Replaying while the timestamp remains valid cannot mutate either Agent's tasks.
        status, _ = await delete(client, base, token, scope)
        assert status == 401
    assert (tmp_path / "tasks.json").read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["job", "revision", "condition", "run", "agent"])
async def test_approved_operation_cannot_authorize_a_different_target(tmp_path, mismatch):
    server, runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    binding, scope = active_run(server), deletion_scope(runtime)
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        token = credential(server, base, binding)
        _, pending = await delete(client, base, token, scope)
        await owner_decision(client, base, server, pending["approval"]["approval_id"])
        changed = dict(scope)
        expected = 403
        if mismatch == "job":
            changed["job_id"] = "other-agent"
            expected = 404
        elif mismatch == "revision":
            # A legitimate edit invalidates the earlier exact-definition decision.
            async with client.patch(base + "/api/agents/momo/scheduler/jobs/daily-report",
                                    json={"kind": "cron", "prompt": "edited"},
                                    headers={"X-Workbench-Token": server.admin_token}) as response:
                assert response.status == 200, await response.text()
            status, _ = await delete(client, base, token, scope)
            assert status == 409
            changed = deletion_scope(runtime)
        elif mismatch == "condition":
            changed["only_disabled"] = True
        elif mismatch == "run":
            second = active_run(server, request="second-run")
            token = credential(server, base, second)
        else:
            foreign = active_run(server, agent="lily", request="foreign-run")
            token = credential(server, base, foreign)
            expected = 401
        before = (tmp_path / "tasks.json").read_bytes()
        status, _ = await delete(client, base, token, changed)
        assert status == expected
        assert (tmp_path / "tasks.json").read_bytes() == before


def test_approval_is_one_use_even_if_dispatch_fails(tmp_path):
    server, runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    binding, scope = active_run(server), deletion_scope(runtime)
    store = server.session_store
    approval = store.create_approval(run_id=binding["run_id"], owner_id=binding["owner_id"],
                                     fencing_token=binding["fencing_token"], scope=scope)
    store.decide_approval(approval_id=approval["approval_id"], owner_id=binding["owner_id"],
                          decision="approved")
    with pytest.raises(RuntimeError, match="unknown outcome"):
        with store.hold_operation_approval(binding=binding, scope=scope) as claimed:
            assert claimed["authorized"]
            raise RuntimeError("unknown outcome")
    # A fresh store instance proves reservation was durable, not just an in-memory flag.
    reopened = SessionStore.from_global_config(server.global_config)
    with reopened.hold_operation_approval(binding=binding, scope=scope) as claimed:
        assert not claimed["authorized"] and claimed["state"] == "consumed"


def test_stop_between_reservation_and_effect_prevents_dispatch(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from orchestrator.session_store import StaleFencingToken

    server, runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    binding, scope = active_run(server), deletion_scope(runtime)
    store = server.session_store
    approval = store.create_approval(run_id=binding["run_id"], owner_id=binding["owner_id"],
                                     fencing_token=binding["fencing_token"], scope=scope)
    store.decide_approval(approval_id=approval["approval_id"], owner_id=binding["owner_id"],
                          decision="approved")
    hold, calls = store.hold_active_run, []

    @contextmanager
    def stop_at_dispatch(**claims):
        calls.append(True)
        if len(calls) == 2:
            store.cancel_run(binding["run_id"], owner_id=binding["owner_id"])
        with hold(**claims) as connection:
            yield connection

    monkeypatch.setattr(store, "hold_active_run", stop_at_dispatch)
    with pytest.raises(StaleFencingToken):
        with store.hold_operation_approval(binding=binding, scope=scope):
            pytest.fail("stopped Run dispatched an effect")
    assert runtime.skill_manager.get_job("cron", "daily-report") is not None
    with store._connection() as connection:
        row = connection.execute("SELECT state FROM run_approvals WHERE approval_id=?",
                                 (approval["approval_id"],)).fetchone()
        assert row["state"] == "consumed"


@pytest.mark.asyncio
async def test_normal_personal_scheduler_tools_remain_usable(tmp_path):
    server, runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    binding = active_run(server)
    tools = ["hashi_scheduler_list", "hashi_scheduler_status",
             "hashi_scheduler_create", "hashi_scheduler_update"]
    async with TestServer(server.app) as upstream:
        registry = ToolRegistry(
            allowed_tools=tools, access_root=tmp_path, workspace_dir=runtime.workspace_dir,
            secrets={"workbench_admin_token": server.admin_token},
            audit_context={"agent_name": "momo", "global_config": server.global_config,
                           "request_id": binding["request_id"], "hashi_run_id": binding["run_id"],
                           "hashi_fencing_token": binding["fencing_token"],
                           "workbench_api_base_url": str(upstream.make_url("")).rstrip("/")},
        )
        cases = [
            ("hashi_scheduler_create", {"kind": "cron", "task_id": "disposable",
                                       "schedule": "0 0 * * *", "prompt": "test", "enabled": False}),
            ("hashi_scheduler_list", {}),
            ("hashi_scheduler_update", {"kind": "cron", "job_id": "disposable", "prompt": "changed"}),
            ("hashi_scheduler_status", {"kind": "cron", "job_id": "disposable"}),
        ]
        for tool, args in cases:
            result = await registry.execute(tool, args)
            assert not result.is_error, result.output
            assert json.loads(result.output)["ok"]
        assert runtime.skill_manager.get_job("cron", "disposable")["prompt"] == "changed"



def test_stop_and_effect_share_the_existing_pao_fence(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    server, runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    binding, scope = active_run(server), deletion_scope(runtime)
    store = server.session_store
    approval = store.create_approval(run_id=binding["run_id"], owner_id=binding["owner_id"],
                                     fencing_token=binding["fencing_token"], scope=scope)
    store.decide_approval(approval_id=approval["approval_id"], owner_id=binding["owner_id"],
                          decision="approved")
    other = SessionStore.from_global_config(server.global_config)
    entered, cancel_started, release = threading.Event(), threading.Event(), threading.Event()

    def effect():
        with store.hold_operation_approval(binding=binding, scope=scope) as decision:
            assert decision["authorized"]
            entered.set()
            assert release.wait(10)
            return runtime.skill_manager.delete_owned_job_if_unchanged(
                "cron", "daily-report", agent_name="momo", expected_revision=scope["expected_revision"])

    def stop():
        assert entered.wait(10)
        cancel_started.set()
        return other.cancel_run(binding["run_id"], owner_id=binding["owner_id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatched = pool.submit(effect)
        cancelled = pool.submit(stop)
        try:
            assert entered.wait(10) and cancel_started.wait(10)
            assert not cancelled.done()
        finally:
            release.set()
        assert dispatched.result(timeout=10)[0] is True
        assert cancelled.result(timeout=10)["state"] == "stopped"
    assert runtime.skill_manager.get_job("cron", "daily-report") is None
