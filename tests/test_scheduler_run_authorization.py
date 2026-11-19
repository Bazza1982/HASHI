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
async def test_expired_scheduler_gateway_lease_renews_only_for_the_live_run_and_tool(tmp_path, monkeypatch):
    import time
    from orchestrator.workbench_auth import scheduler_tool_token_claims
    from tools.gateway.context import load_gateway_context, write_gateway_context

    server, runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    binding = active_run(server)
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        old = credential(server, base, binding, tools=["hashi_scheduler_list"], now=time.time() - 3700)
        headers = {"X-Hashi-Scheduler-Token": old}
        async with client.get(base + "/api/agents/momo/scheduler/jobs", headers=headers) as response:
            assert response.status == 401
        async with client.post(base + "/api/agents/momo/scheduler/token/renew", headers=headers,
                               json={"tool": "hashi_scheduler_list"}) as response:
            assert response.status == 200, await response.text()
            renewed = (await response.json())["token"]
        common = dict(instance_id=server.global_config.instance_id, agent_id="momo", audience=base)
        assert scheduler_tool_token_claims(server.admin_token, renewed, tool="hashi_scheduler_list", **common)
        assert scheduler_tool_token_claims(server.admin_token, renewed, tool="hashi_scheduler_create", **common) is None

        # A gateway has only the old scoped token, not the signing key. Renewal
        # happens before the read and its token never appears in the tool result.
        registry = ToolRegistry(allowed_tools=["hashi_scheduler_list"], access_root=tmp_path,
                                workspace_dir=runtime.workspace_dir, secrets={"scheduler_tool_token": old},
                                audit_context={"agent_name": "momo", "workbench_api_base_url": base})
        gateway_path = tmp_path / "expired-gateway.json"
        write_gateway_context(registry, gateway_path, backend="codex", workbench_api_base_url=base)
        assert old in gateway_path.read_text(encoding="utf-8")
        assert server.admin_token not in gateway_path.read_text(encoding="utf-8")
        gateway = load_gateway_context(gateway_path).build_registry()
        result = await gateway.execute("hashi_scheduler_list", {})
        assert not result.is_error and json.loads(result.output)["ok"], result.output
        assert old not in result.output and renewed not in result.output and server.admin_token not in result.output
        old_loop = credential(server, base, binding, tools=["hashi_superloop_list"], now=time.time() - 3700)
        loop_registry = ToolRegistry(allowed_tools=["hashi_superloop_list"], access_root=tmp_path,
                                     workspace_dir=runtime.workspace_dir, secrets={"scheduler_tool_token": old_loop},
                                     audit_context={"agent_name": "momo", "workbench_api_base_url": base})
        loop_result = await loop_registry.execute("hashi_superloop_list", {})
        assert not loop_result.is_error and json.loads(loop_result.output)["ok"], loop_result.output
        assert old_loop not in loop_result.output and server.admin_token not in loop_result.output

        calls = []
        original_upsert = runtime.skill_manager.upsert_cron_job
        def upsert_once(**kwargs):
            calls.append(kwargs["task_id"])
            return original_upsert(**kwargs)
        monkeypatch.setattr(runtime.skill_manager, "upsert_cron_job", upsert_once)
        old_create = credential(server, base, binding, tools=["hashi_scheduler_create"], now=time.time() - 3700)
        create_registry = ToolRegistry(allowed_tools=["hashi_scheduler_create"], access_root=tmp_path,
                                       workspace_dir=runtime.workspace_dir, secrets={"scheduler_tool_token": old_create},
                                       audit_context={"agent_name": "momo", "workbench_api_base_url": base})
        args = {"kind": "cron", "task_id": "renewed-fixture", "schedule": "0 0 * * *",
                "prompt": "fixture only", "enabled": False}
        created = await create_registry.execute("hashi_scheduler_create", args)
        assert not created.is_error and json.loads(created.output)["ok"], created.output
        assert calls == ["renewed-fixture"]

        for name, token, tool in [
            ("momo", old, "hashi_scheduler_create"),
            ("lily", old, "hashi_scheduler_list"),
            ("momo", old + "x", "hashi_scheduler_list"),
            ("momo", credential(server, "http://127.0.0.1:1", binding,
                                tools=["hashi_scheduler_list"], now=time.time() - 3700), "hashi_scheduler_list"),
            ("momo", credential(server, base, binding, tools=["hashi_scheduler_list"],
                                fencing_token=binding["fencing_token"] + 1, now=time.time() - 3700), "hashi_scheduler_list"),
        ]:
            async with client.post(base + f"/api/agents/{name}/scheduler/token/renew",
                                   headers={"X-Hashi-Scheduler-Token": token}, json={"tool": tool}) as response:
                assert response.status in {401, 403}

        server.session_store.cancel_run(binding["run_id"], owner_id=binding["owner_id"])
        stopped = await create_registry.execute("hashi_scheduler_create", args)
        assert stopped.is_error and calls == ["renewed-fixture"]
        async with client.post(base + "/api/agents/momo/scheduler/token/renew", headers=headers,
                               json={"tool": "hashi_scheduler_list"}) as response:
            assert response.status in {401, 403}


@pytest.mark.asyncio
async def test_failed_lease_renewal_does_not_dispatch_a_scheduler_change(monkeypatch):
    import time
    from unittest.mock import AsyncMock
    from tools import hashi_scheduler

    expired = issue_scheduler_tool_token("fixture-secret", instance_id="TEST", agent_id="momo",
                                         request_id="request", audience="http://127.0.0.1:9",
                                         tools=["hashi_scheduler_create"], run_id="run", fencing_token=1,
                                         now=time.time() - 3700)
    request = AsyncMock(return_value=(503, {"ok": False, "error_code": "scheduler_delegation_unavailable"}))
    monkeypatch.setattr(hashi_scheduler, "_request_json", request)
    result = await hashi_scheduler.execute_hashi_scheduler_tool(
        "hashi_scheduler_create", {"kind": "cron", "task_id": "fixture"}, credential=expired,
        audit_context={"agent_name": "momo", "workbench_api_base_url": "http://127.0.0.1:9"},
    )
    assert "scheduler_delegation_unavailable" in result
    assert request.await_count == 1
    assert request.await_args.args[1].endswith("/scheduler/token/renew")
    assert expired not in result


def test_scheduler_lease_accepts_small_clock_skew_but_not_expired_effects():
    from orchestrator.workbench_auth import scheduler_tool_token_claims

    scope = dict(instance_id="TEST", agent_id="momo", audience="http://127.0.0.1:9",
                 tool="hashi_scheduler_list")
    token = issue_scheduler_tool_token("fixture-secret", instance_id=scope["instance_id"],
                                       agent_id=scope["agent_id"], request_id="request",
                                       audience=scope["audience"], tools=[scope["tool"]],
                                       run_id="run", fencing_token=1, now=100)
    assert scheduler_tool_token_claims("fixture-secret", token, now=99, **scope)
    assert scheduler_tool_token_claims("fixture-secret", token, now=3700, **scope) is None


@pytest.mark.asyncio
async def test_governed_scheduler_delegation_binds_user_policy_and_live_run(tmp_path):
    import time
    from orchestrator.enterprise import EnterpriseRole, IdentityService, PolicyEvaluator
    from orchestrator.workbench_auth import scheduler_delegation_key
    from tools.hashi_scheduler import scheduler_tool_credential

    server, runtime = _server(tmp_path, persistent_session_v1=True)
    server.global_config.deployment_profile = "enterprise"
    server.global_config.organization_id = "ORG-001"
    server.global_config.instance_id = server.session_store.instance_id
    signing_key = scheduler_delegation_key(tmp_path, create=True)
    identity = IdentityService.from_path(tmp_path / "state" / "enterprise.sqlite")
    identity.create_organization(org_id="ORG-001", name="Fixture")
    project = identity.create_project(org_id="ORG-001", name="Fixture", project_id="project-1")
    for user_id in ("allowed", "denied", "outsider", "retired"):
        user = identity.create_user(org_id="ORG-001", email=f"{user_id}@example.invalid",
                                    display_name=user_id, password="fixture-password", user_id=user_id)
        if user_id != "outsider":
            identity.assign_project_role(user_id=user.id, project_id=project.id,
                                         role=EnterpriseRole.INDIVIDUAL_USER)
    server.identity_service = identity
    server.config_path.write_text(json.dumps({"global": {}, "agents": [
        {"name": "momo", "project_id": project.id},
    ]}), encoding="utf-8")
    policy = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    policy.add_rule(action="tool.execute", resource="tool:hashi_scheduler_list", effect="deny",
                    scope_type="user", scope_id="denied")

    def run_for(user):
        owner = f"enterprise:{user}"
        session = server.session_store.create_session(owner_id=owner, agent_id="momo")
        request = f"scheduler-{user}"
        run = server.session_store.accept_run(session_id=session["session_id"], owner_id=owner,
                                              agent_id="momo", request_id=request, text="test",
                                              source="test", idempotency_key=request)
        fence = server.session_store.mark_request_running(request, worker_id="fixture-worker")
        return {"owner_id": owner, "request_id": request, "hashi_run_id": run.run_id,
                "hashi_fencing_token": fence}

    runs = {name: run_for(name) for name in ("allowed", "denied", "outsider", "retired")}
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")

        def token_for(run, **overrides):
            context = {"agent_name": "momo", "global_config": server.global_config,
                       "workbench_api_base_url": base, **run, **overrides}
            return scheduler_tool_credential({},
                                             context, {"hashi_scheduler_list"})

        allowed_token = token_for(runs["allowed"])
        assert allowed_token and server.admin_token not in allowed_token

        def old_token_for(run):
            return issue_scheduler_tool_token(
                signing_key, instance_id=server.global_config.instance_id, agent_id="momo",
                request_id=run["request_id"], audience=base, tools=["hashi_scheduler_list"],
                run_id=run["hashi_run_id"], fencing_token=run["hashi_fencing_token"],
                owner_id=run["owner_id"], now=time.time() - 3700,
            )

        async def renew(token):
            async with client.post(base + "/api/agents/momo/scheduler/token/renew",
                                   headers={"X-Hashi-Scheduler-Token": token},
                                   json={"tool": "hashi_scheduler_list"}) as response:
                return response.status, await response.json()

        assert (await renew(old_token_for(runs["allowed"])))[0] == 200
        assert (await renew(old_token_for(runs["denied"])))[0] in {401, 403}
        assert (await renew(old_token_for(runs["outsider"])))[0] in {401, 403}

        async def listed(token, path="/api/agents/momo/scheduler/jobs"):
            async with client.get(base + path, headers={"X-Hashi-Scheduler-Token": token}) as response:
                return response.status, await response.json()

        status, payload = await listed(allowed_token)
        assert status == 200 and payload["count"] == 1
        registry = ToolRegistry(
            allowed_tools=["hashi_scheduler_list", "hashi_scheduler_create"],
            access_root=tmp_path, workspace_dir=runtime.workspace_dir,
            secrets={},
            audit_context={"agent_name": "momo", "global_config": server.global_config,
                           "workbench_api_base_url": base, **runs["allowed"]},
        )
        result = await registry.execute("hashi_scheduler_create", {
            "kind": "cron", "task_id": "governed-test", "schedule": "0 0 * * *",
            "prompt": "fixture only", "enabled": False,
        })
        assert not result.is_error and json.loads(result.output)["ok"], result.output
        assert runtime.skill_manager.get_job("cron", "governed-test")["prompt"] == "fixture only"
        from tools.gateway.context import load_gateway_context, write_gateway_context

        gateway_path = tmp_path / "governed-gateway.json"
        write_gateway_context(registry, gateway_path, backend="codex", workbench_api_base_url=base)
        gateway_data = gateway_path.read_text(encoding="utf-8")
        assert signing_key not in gateway_data
        assert server.admin_token not in gateway_data
        gateway = load_gateway_context(gateway_path).build_registry()
        gateway_result = await gateway.execute("hashi_scheduler_list", {})
        assert not gateway_result.is_error and json.loads(gateway_result.output)["ok"], gateway_result.output
        policy.add_rule(action="tool.execute", resource="tool:hashi_scheduler_create", effect="deny",
                        scope_type="project", scope_id=project.id)
        blocked = await registry.execute("hashi_scheduler_create", {
            "kind": "cron", "task_id": "policy-blocked", "schedule": "0 0 * * *",
            "prompt": "fixture only", "enabled": False,
        })
        assert blocked.is_error
        assert runtime.skill_manager.get_job("cron", "policy-blocked") is None
        assert (await listed(token_for(runs["denied"])))[0] in {401, 403}
        assert (await listed(token_for(runs["outsider"])))[0] in {401, 403}
        assert (await listed(token_for(runs["allowed"], owner_id="enterprise:denied")))[0] in {401, 403}
        assert (await listed(allowed_token, "/api/agents/lily/scheduler/jobs"))[0] in {401, 403}
        assert (await listed(allowed_token, "/api/agents/momo/scheduler/status?kind=cron&job_id=daily-report"))[0] in {401, 403}
        assert (await listed("invalid"))[0] in {401, 403}
        assert (await listed(token_for(runs["allowed"], workbench_api_base_url="http://127.0.0.1:1")))[0] in {401, 403}
        retired_token = token_for(runs["retired"])
        assert (await listed(retired_token))[0] == 200
        identity.deactivate_user(user_id="retired")
        assert (await listed(retired_token))[0] in {401, 403}
        assert (await renew(old_token_for(runs["retired"])))[0] in {401, 403}

        server.session_store.cancel_run(runs["allowed"]["hashi_run_id"], owner_id="enterprise:allowed")
        assert (await listed(allowed_token))[0] in {401, 403}
        assert (await renew(old_token_for(runs["allowed"])))[0] in {401, 403}


@pytest.mark.asyncio
async def test_missing_governed_delegation_reports_capability_before_dispatch(tmp_path, monkeypatch):
    from tools import hashi_scheduler
    from tools.gateway.context import load_gateway_context, write_gateway_context

    config = SimpleNamespace(deployment_profile="enterprise", bridge_home=tmp_path, instance_id="TEST")
    registry = ToolRegistry(
        allowed_tools=["hashi_scheduler_list"], access_root=tmp_path, workspace_dir=tmp_path, secrets={},
        audit_context={"agent_name": "momo", "global_config": config,
                       "workbench_api_base_url": "http://127.0.0.1:9", "owner_id": "enterprise:user",
                       "request_id": "run", "hashi_run_id": "run", "hashi_fencing_token": 1},
    )

    async def no_dispatch(*args, **kwargs):
        pytest.fail("missing delegation must not make an unauthenticated API call")

    monkeypatch.setattr(hashi_scheduler, "_request_json", no_dispatch)
    gateway_path = tmp_path / "gateway.json"
    write_gateway_context(registry, gateway_path, backend="codex", workbench_api_base_url="http://127.0.0.1:9")
    for candidate in (registry, load_gateway_context(gateway_path).build_registry()):
        result = await candidate.execute("hashi_scheduler_list", {})
        assert result.is_error and "scheduler_delegation_unavailable" in result.output


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



@pytest.mark.asyncio
async def test_cancelled_async_mutation_admission_never_dispatches_late(tmp_path, monkeypatch):
    import asyncio
    import threading
    from contextlib import contextmanager

    from orchestrator.agent_mutation import invoke_async_mutation

    server, runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    binding = active_run(server)
    entered, release = threading.Event(), threading.Event()
    original = server.session_store.hold_active_run
    dispatched = []

    @contextmanager
    def stalled_fence(**claims):
        entered.set()
        assert release.wait(5)
        with original(**claims) as connection:
            yield connection

    async def callback():
        dispatched.append(True)

    monkeypatch.setattr(server.session_store, "hold_active_run", stalled_fence)
    task = asyncio.create_task(invoke_async_mutation(server, binding, callback))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert dispatched == []


@pytest.mark.asyncio
async def test_authenticated_owner_superloop_delete_needs_no_magic_body_but_run_does_not_self_grant(tmp_path):
    server, _runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, _runtime)
    binding = active_run(server)
    service = server._superloop_service("momo")
    loop_id = service.create_quickstart("synthetic fixture")["loop_id"]
    other_id = server._superloop_service("lily").create_quickstart("other fixture")["loop_id"]
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        url = base + f"/api/agents/momo/superloops/{loop_id}"
        async with client.delete(url, json={}) as response:
            assert response.status == 401
        token = credential(server, base, binding, tools=["hashi_superloop_delete"])
        async with client.delete(url, json={},
                                 headers={"X-Hashi-Scheduler-Token": token}) as response:
            assert response.status == 403
            pending = await response.json()
            assert pending["error_code"] == "approval_required"
        async with client.delete(url, json={"authorization": "explicit_user_authorization"},
                                 headers={"X-Hashi-Scheduler-Token": token}) as response:
            assert response.status == 403
            assert (await response.json())["approval"]["approval_id"] == pending["approval"]["approval_id"]
        registry = ToolRegistry(
            allowed_tools=["hashi_superloop_delete"], access_root=tmp_path,
            workspace_dir=_runtime.workspace_dir, secrets={"scheduler_tool_token": token},
            audit_context={"agent_name": "momo", "workbench_api_base_url": base},
        )
        result = await registry.execute("hashi_superloop_delete", {"loop_id": loop_id})
        assert result.is_error and "[approval_required]" in result.output
        assert pending["approval"]["approval_id"] in result.output
        assert service.get_loop(loop_id)["loop"]["status"] != "deleted"
        async with client.delete(url, json={},
                                 headers={"X-Workbench-Token": server.admin_token}) as response:
            assert response.status == 200, await response.text()
            assert (await response.json())["status"] == "deleted"
        async with client.delete(base + f"/api/agents/momo/superloops/{other_id}", json={},
                                 headers={"X-Workbench-Token": server.admin_token}) as response:
            assert response.status == 404
        assert server._superloop_service("lily").get_loop(other_id)["loop"]["status"] != "deleted"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["hashi_scheduler_delete", "hashi_scheduler_rerun"])
async def test_scoped_scheduler_operation_uses_pao_decision_not_a_magic_string(tmp_path, tool):
    import re

    server, runtime = _server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    binding = active_run(server)
    original = runtime.skill_manager.get_job("cron", "daily-report")
    async with TestServer(server.app) as upstream, aiohttp.ClientSession() as client:
        base = str(upstream.make_url("")).rstrip("/")
        token = credential(server, base, binding, tools=[tool])
        registry = ToolRegistry(
            allowed_tools=[tool], access_root=tmp_path, workspace_dir=runtime.workspace_dir,
            secrets={"scheduler_tool_token": token},
            audit_context={"agent_name": "momo", "workbench_api_base_url": base},
        )
        args = {"kind": "cron", "job_id": "daily-report"}
        pending = await registry.execute(tool, args)
        assert pending.is_error and "[approval_required]" in pending.output, pending.output
        match = re.search(r"approval_[0-9a-f]{32}", pending.output)
        assert match, pending.output
        assert runtime.skill_manager.get_job("cron", "daily-report") == original
        assert runtime.reruns == []
        await owner_decision(client, base, server, match.group())
        approved = await registry.execute(tool, args)
        assert not approved.is_error, approved.output
        if tool == "hashi_scheduler_delete":
            assert runtime.skill_manager.get_job("cron", "daily-report") is None
            assert runtime.skill_manager.get_job("cron", "other-agent")["agent"] == "lily"
        else:
            assert len(runtime.reruns) == 1
            repeated = await registry.execute(tool, args)
            assert not repeated.is_error, repeated.output
            assert len(runtime.reruns) == 1
