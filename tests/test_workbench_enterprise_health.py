from __future__ import annotations

import json
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.workbench_api import WorkbenchApiServer



@pytest.mark.asyncio
@pytest.mark.parametrize("remote_ok", [True, False])
async def test_shared_functions_commit_leaves_remote_lifecycle_untouched(tmp_path, monkeypatch, remote_ok):
    from unittest.mock import AsyncMock
    from orchestrator.runtime_app_host import RuntimeAppHost
    from orchestrator.startup_manager import StartupManager
    from orchestrator import remote_lifecycle

    release = asyncio.Event()
    settings = SimpleNamespace(enabled=True, supervised=False, port=18474)
    async def launch(*args, **kwargs):
        pytest.fail("max shared replacement must not touch Remote lifecycle")
    monkeypatch.setattr(remote_lifecycle, "ensure_remote_started", launch)
    monkeypatch.setattr(remote_lifecycle, "inspect_remote", AsyncMock(return_value={
        "ok": False, "action": "remote_activation_failed", "settings": settings}))
    monkeypatch.delenv("HASHI_REMOTE_ROOT", raising=False)
    app = SimpleNamespace(paths=SimpleNamespace(bridge_home=tmp_path, instance_id="TEST"),
        global_cfg=SimpleNamespace(authorized_id=1, bridge_home=tmp_path, project_root=tmp_path),
        runtimes=[], shared_generation_id="new-functions", api_gateway=None, whatsapp=None,
        _shared_replacement_candidate=True, _handoff_draining=True,
        _runtime_map=lambda: {}, _load_whatsapp_cfg=lambda: ({}, {}),
        function_workers=SimpleNamespace(_telegram_ingress={}, broadcast_topology=AsyncMock()),
        startup_status={"services_ready": True, "ready": True, "issues": [], "agent_states": {}})
    app.startup_manager = StartupManager(app, None)
    server = _server(tmp_path, profile="personal")
    server.orchestrator = app
    host = RuntimeAppHost(None, {})
    host.app = app
    host.task = asyncio.create_task(asyncio.Event().wait())
    activated = _first_connector_activation(host)
    commit = asyncio.create_task(host.commit())
    try:
        result = await asyncio.wait_for(commit, timeout=.4)
        assert result["committed"]
        await asyncio.wait_for(activated.wait(), timeout=1)
        payload = json.loads((await server.handle_health(_FakeRequest())).text)
        assert "remote" not in payload
        assert payload["ready"] is True
        assert payload["degraded"] is False
        assert result["degraded"] is True  # Connections were still pending at commit.
    finally:
        release.set()
        host.stopping.set()
        commit.cancel()
        host.task.cancel()
        if host.connector_task:
            host.connector_task.cancel()
        await asyncio.gather(commit, host.task, *([host.connector_task] if host.connector_task else []),
                             return_exceptions=True)

def _first_connector_activation(host):
    completed = asyncio.Event()
    original = host._activate_connectors
    async def observe():
        try:
            return await original()
        finally:
            completed.set()
    host._activate_connectors = observe
    return completed

class _FakeRequest:
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_connector_commit_health_and_recovery_follow_real_ingress(tmp_path, monkeypatch, persistent):
    from orchestrator.runtime_app_host import RuntimeAppHost
    from orchestrator.startup_manager import StartupManager
    from orchestrator.telegram_ingress import CoreTelegramIngress
    from orchestrator.telegram_delivery_state import telegram_bot_fingerprint
    from orchestrator.function_worker_supervisor import AgentRuntimeHandle, FunctionWorkerSupervisor

    monkeypatch.setattr("orchestrator.telegram_ingress.TELEGRAM_RETRY_SECONDS", 0.01)
    recovery_allowed = asyncio.Event()
    handles = {}
    class WorkerClient:
        def __init__(self, name):
            self.agent_name, self.fail_status = name, False
        async def call(self, method, params, **kwargs):
            if method == "worker.telegram_status":
                if self.fail_status:
                    raise ConnectionResetError("worker IPC disconnected")
                return {"metadata": {**handles[self.agent_name].metadata,
                                     "telegram_ingress_connected": params["connected"]}}
            assert method == "runtime.enqueue_startup_bootstrap"
            return True
    reports = []
    class Bot:
        def __init__(self, name): self.name, self.attempts, self.polls = name, 0, 0
        async def initialize(self):
            self.attempts += 1
            if self.name == "alpha" and (persistent or not recovery_allowed.is_set()):
                raise OSError("connection unavailable")
        async def delete_webhook(self, **kwargs): pass
        async def shutdown(self): pass
        async def get_updates(self, **kwargs):
            self.polls += 1
            if self.polls == 1:
                return []
            await asyncio.Event().wait()

    app = SimpleNamespace(paths=SimpleNamespace(bridge_home=tmp_path, instance_id="TEST"),
        runtimes=[], shared_generation_id="test", api_gateway=None, whatsapp=None,
        _handoff_draining=True, _runtime_map=lambda: handles,
        _load_whatsapp_cfg=lambda: ({}, {}), global_cfg=SimpleNamespace(authorized_id=1),
        startup_status={"agent_order": ["alpha", "beta"], "agent_states": {n:"local" for n in ["alpha", "beta"]},
            "services_ready": True, "ready": False, "issues": [
                {"code": "agent_telegram_unavailable", "severity": "warning", "details": {"agents": ["alpha", "beta"]}}]})
    for name in ["alpha", "beta"]:
        handles[name] = AgentRuntimeHandle(app, WorkerClient(name),
            {"worker_phase": "ACTIVE", "worker_accepting": True,
             "telegram_connected": True, "telegram_outbound_connected": True,
             "telegram_bot_fingerprint": telegram_bot_fingerprint(name)})
    app.function_workers = FunctionWorkerSupervisor(app)
    ingress = app.function_workers._telegram_ingress
    snapshot = app.function_workers.telegram_ingress_snapshot
    app.startup_manager = StartupManager(app, None)
    def report():
        reports.append({name: snapshot(name) for name in handles})
        app.startup_manager.show_startup_status()
    app._report_startup = report
    for name in handles:
        async def status(connected, name=name):
            await app.function_workers.set_worker_telegram_status(name, connected)
        ingress[name] = CoreTelegramIngress(agent_name=name, token=name,
            handle_lookup=handles.get, status_callback=status, bot_factory=Bot)
    server = _server(tmp_path, profile="personal")
    server.orchestrator = app
    host = RuntimeAppHost(None, {})
    host.app = app
    host.task = asyncio.create_task(asyncio.Event().wait())
    async def wait_connected(name):
        for _ in range(200):
            if snapshot(name)["connected"]:
                return
            await asyncio.sleep(0.01)
        pytest.fail(f"{name} did not report a successful poll")
    activated = _first_connector_activation(host)
    try:
        result = await host.commit()
        assert result["committed"] and result["degraded"]
        await asyncio.wait_for(activated.wait(), timeout=1)
        assert reports
        assert reports[0]["beta"]["running"]
        assert not reports[0]["alpha"]["connected"]
        payload = json.loads((await server.handle_health(_FakeRequest())).text)
        assert payload["degraded"] and payload["ready"]
        affected = next(i for i in payload["issues"] if i["code"] == "agent_telegram_unavailable")
        assert "alpha" in affected["details"]["agents"]
        await wait_connected("beta")
        assert ingress["beta"].bot.polls >= 1
        payload = json.loads((await server.handle_health(_FakeRequest())).text)
        affected = next(i for i in payload["issues"] if i["code"] == "agent_telegram_unavailable")
        assert affected["details"]["agents"] == ["alpha"]
        if persistent:
            await host._activate_connectors()
            assert app.startup_status["ready"]
            assert app.startup_status["degraded"]
        else:
            # The ingress owns connection recovery after nonblocking start;
            # activation retry is reserved for failure to start a connector.
            recovery_allowed.set()
            await wait_connected("alpha")
            assert ingress["alpha"].bot.attempts >= 2
            payload = json.loads((await server.handle_health(_FakeRequest())).text)
            assert payload["ready"] and not payload["degraded"] and not payload["issues"]
            assert all(item["connected"] and item["running"] for item in reports[-1].values())
            # A subsequent real ingress transition must update the same health owner.
            await ingress["beta"]._set_connected(False)
            assert app.startup_status["degraded"] and app.startup_status["ready"]
            await ingress["beta"]._set_connected(True)
            assert app.startup_status["ready"] and not app.startup_status["issues"]
            # Stale Worker metadata must not conceal an actual ingress failure.
            handles["beta"].client.fail_status = True
            await ingress["beta"]._set_connected(False)
            assert handles["beta"].telegram_connected  # IPC could not update the Worker.
            assert app.startup_status["degraded"] and app.startup_status["ready"]
            handles["beta"].client.fail_status = False
            await ingress["beta"]._set_connected(True)
            assert app.startup_status["ready"] and not app.startup_status["issues"]
            # Connector recovery cannot erase an independent service failure.
            remote_issue = {"code": "remote_supervisor_unavailable", "severity": "warning"}
            app.startup_status["issues"].append(remote_issue)
            await ingress["beta"]._set_connected(False)
            await ingress["beta"]._set_connected(True)
            assert app.startup_status["issues"] == [remote_issue]
            assert app.startup_status["degraded"] and not app.startup_status["ready"]
    finally:
        host.stopping.set()
        host.task.cancel()
        if host.connector_task:
            host.connector_task.cancel()
        await asyncio.gather(
            host.task,
            *([host.connector_task] if host.connector_task else []),
            return_exceptions=True,
        )
        warning_task = app.function_workers._telegram_status_warning_task
        if warning_task:
            warning_task.cancel()
            await asyncio.gather(warning_task, return_exceptions=True)
        for item in ingress.values():
            await item.stop(notify_status=False)


@pytest.mark.asyncio
async def test_health_clears_recovered_connector_activation_errors(tmp_path):
    from orchestrator.startup_manager import StartupManager

    snapshots = {
        name: {"configured": True, "running": False, "connected": False}
        for name in ("alpha", "beta")
    }
    handles = {
        name: SimpleNamespace(
            telegram_connected=False,
            backend_ready=True,
            client=SimpleNamespace(closed=False),
            metadata={
                "worker_phase": "ACTIVE",
                "worker_accepting": True,
                "telegram_connector_permanent_failure": (
                    "invalid_token" if name == "alpha" else None
                ),
            },
        )
        for name in snapshots
    }
    ingress = {
        name: SimpleNamespace(
            permanent_failure_reason="invalid_token" if name == "alpha" else None
        )
        for name in snapshots
    }
    app = SimpleNamespace(
        runtimes=[],
        api_gateway=None,
        _runtime_map=lambda: handles,
        function_workers=SimpleNamespace(
            _telegram_ingress=ingress,
            telegram_ingress_snapshot=lambda name: dict(snapshots[name]),
        ),
        startup_status={
            "phase": "degraded",
            "services_ready": True,
            "agent_order": list(handles),
            "agent_states": {name: "local" for name in handles},
            "failed_agents": 0,
            "issues": [],
        },
    )
    app.startup_manager = StartupManager(app, None)
    app.startup_manager.reconcile_connector_status({
        "alpha": "InvalidToken: credential rejected",
        "beta": "TimeoutError: Telegram unavailable",
    }, publish=False)
    server = _server(tmp_path, profile="personal")
    server.orchestrator = app

    failed = json.loads((await server.handle_health(_FakeRequest())).text)
    assert failed["startup"]["connector_errors"] == {
        "alpha": "InvalidToken: credential rejected",
        "beta": "TimeoutError: Telegram unavailable",
    }
    assert "connector_activation" in {issue["code"] for issue in failed["issues"]}

    # Correcting alpha's Token re-arms its connector; current readiness then
    # owns the old activation error rather than preserving its history.
    handles["alpha"].metadata["telegram_connector_permanent_failure"] = None
    ingress["alpha"].permanent_failure_reason = None
    handles["alpha"].telegram_connected = True
    snapshots["alpha"].update(running=True, connected=True)
    app.startup_manager.reconcile_connector_status(publish=False)

    partially_recovered = json.loads(
        (await server.handle_health(_FakeRequest())).text
    )
    assert partially_recovered["startup"]["connector_errors"] == {
        "beta": "TimeoutError: Telegram unavailable"
    }
    activation = next(
        issue for issue in partially_recovered["issues"]
        if issue["code"] == "connector_activation"
    )
    assert activation["details"] == {
        "beta": "TimeoutError: Telegram unavailable"
    }

    handles["beta"].telegram_connected = True
    snapshots["beta"].update(running=True, connected=True)
    app.startup_manager.reconcile_connector_status(publish=False)

    recovered = json.loads((await server.handle_health(_FakeRequest())).text)
    assert recovered["ready"] is True
    assert recovered["degraded"] is False
    assert recovered["startup"]["connector_errors"] == {}
    assert not recovered["issues"]


def _server(tmp_path: Path, *, profile: str) -> WorkbenchApiServer:
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {"deployment_profile": profile, "organization_id": "ORG-001"},
                "agents": [],
            }
        ),
        encoding="utf-8",
    )
    global_config = SimpleNamespace(
        deployment_profile=profile,
        organization_id="ORG-001",
        bridge_home=tmp_path,
        workbench_port=18800,
        project_root=tmp_path,
    )
    return WorkbenchApiServer(config_path=config_path, global_config=global_config)


@pytest.mark.asyncio
async def test_enterprise_health_includes_governance_services(tmp_path):
    server = _server(tmp_path, profile="enterprise")

    response = await server.handle_health(_FakeRequest())

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["enterprise"]["ok"] is True
    assert payload["enterprise"]["profile"] == "enterprise"
    assert payload["enterprise"]["organization_id"] == "ORG-001"
    assert payload["enterprise"]["services"] == {
        "identity": True,
        "channel_registry": True,
        "audit_ledger": True,
        "policy_evaluator": True,
    }


@pytest.mark.asyncio
async def test_personal_health_keeps_legacy_shape_without_enterprise_block(tmp_path):
    server = _server(tmp_path, profile="personal")

    response = await server.handle_health(_FakeRequest())

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is True
    assert "enterprise" not in payload


@pytest.mark.asyncio
async def test_health_warns_when_online_after_function_release_fallback(tmp_path):
    server = _server(tmp_path, profile="personal")
    server.orchestrator = SimpleNamespace(
        instance_id="HASHI1",
        api_gateway=None,
        runtimes=[],
        startup_status={"ready": True, "degraded": False, "phase": "ready", "issues": []},
        shared_generation_id="sha256:previous",
        function_release_adoption={
            "status": "fallback",
            "reason_code": "source_uncommitted",
        },
    )

    payload = json.loads((await server.handle_health(_FakeRequest())).text)

    assert payload["ready"] is True
    assert payload["degraded"] is True
    assert payload["shared_functions"]["adoption"]["status"] == "fallback"
    assert payload["issues"][0]["code"] == "function_release_fallback"
    assert "New code was not adopted" in payload["issues"][0]["summary"]


@pytest.mark.asyncio
async def test_health_exposes_runtime_contract_and_active_generation(tmp_path):
    server = _server(tmp_path, profile="personal")
    runtime = SimpleNamespace(
        runtime_id="cpython-3.12/core-2/function-2/worker-1/cpython-312/x86_64",
        python="3.12.13",
        platform_abi="cpython-312-x86_64-linux-gnu",
        core_api=2,
        function_api=2,
        worker_model="per-agent-process",
        worker_protocol=1,
        generation_schema=2,
        dependency_digest="sha256:" + "a" * 64,
        core_source_digest="sha256:" + "b" * 64,
    )
    worker = SimpleNamespace(
        name="agent1",
        startup_success=True,
        is_function_worker_proxy=True,
        worker_pid=2468,
        generation_id="sha256:" + "c" * 64,
        metadata={"worker_phase": "ACTIVE", "worker_accepting": True},
        client=SimpleNamespace(process=SimpleNamespace(is_alive=lambda: True)),
    )
    server.orchestrator = SimpleNamespace(
        instance_id="HASHI3",
        api_gateway=None,
        runtime_fingerprint=runtime,
        runtimes=[worker],
        function_workers=SimpleNamespace(
            telegram_ingress_snapshot=lambda _name: {
                "running": True,
                "connected": True,
                "offset": 99,
            }
        ),
        function_generation={
            "generation_id": "sha256:" + "c" * 64,
            "worker_model": "per-agent-process",
            "worker_protocol": 1,
        },
    )

    response = await server.handle_health(_FakeRequest())
    payload = json.loads(response.text)

    assert payload["runtime"] == {
        "id": runtime.runtime_id,
        "python": "3.12.13",
        "platform_abi": "cpython-312-x86_64-linux-gnu",
        "core_api": 2,
        "function_api": 2,
        "worker_model": "per-agent-process",
        "worker_protocol": 1,
        "generation_schema": 2,
        "dependency_digest": "sha256:" + "a" * 64,
        "core_source_digest": "sha256:" + "b" * 64,
    }
    assert payload["function_generation"] == {
        "generation_id": "sha256:" + "c" * 64,
        "worker_model": "per-agent-process",
        "worker_protocol": 1,
    }
    assert payload["function_workers"] == [
        {
            "agent": "agent1",
            "pid": 2468,
            "generation_id": "sha256:" + "c" * 64,
            "phase": "ACTIVE",
            "adopted_at": None,
            "accepting": True,
            "alive": True,
            "telegram_ingress": {
                "running": True,
                "connected": True,
                "offset": 99,
            },
        }
    ]


@pytest.mark.asyncio
async def test_version_endpoint_returns_the_shared_structured_payload(
    tmp_path, monkeypatch
):
    server = _server(tmp_path, profile="personal")
    server.orchestrator = object()
    expected = {
        "ok": True,
        "schema_version": 1,
        "instance": {"id": "HASHI3"},
        "state": {"code": "running_matches_source", "reasons": []},
    }
    seen = {}

    def collect(subject, *, agent_name=None):
        seen["subject"] = subject
        seen["agent_name"] = agent_name
        return expected

    monkeypatch.setattr(
        "orchestrator.version_info.collect_version_payload", collect
    )

    response = await server.handle_version(
        SimpleNamespace(query={"agent": "akane"})
    )

    assert json.loads(response.text) == expected
    assert seen == {"subject": server.orchestrator, "agent_name": "akane"}


def test_whatsapp_channel_health_uses_transport_connection_state(tmp_path):
    server = _server(tmp_path, profile="personal")
    transport = SimpleNamespace(
        _client=object(),
        is_connected=lambda: False,
    )
    server.orchestrator = SimpleNamespace(whatsapp=transport)

    assert server._is_whatsapp_available() is False

    transport.is_connected = lambda: True
    assert server._is_whatsapp_available() is True


@pytest.mark.asyncio
async def test_health_distinguishes_core_liveness_from_complete_startup(tmp_path):
    server = _server(tmp_path, profile="personal")
    server.orchestrator = SimpleNamespace(
        instance_id="HASHI3",
        api_gateway=None,
        runtimes=[],
        startup_status={
            "phase": "starting_workers",
            "ready": False,
            "completed": 2,
            "ready_agents": 2,
            "total": 6,
            "agent_percent": 33,
            "percent": 40,
            "elapsed_seconds": 8.4,
        },
    )

    response = await server.handle_health(_FakeRequest())
    payload = json.loads(response.text)

    assert payload["ok"] is True
    assert payload["ready"] is False
    assert payload["degraded"] is False
    assert payload["status"] == "starting_workers"
    assert payload["issues"] == []
    assert payload["startup"]["completed"] == 2
    assert payload["startup"]["total"] == 6
    assert payload["startup"]["percent"] == 40


@pytest.mark.asyncio
async def test_health_explains_degraded_remote_without_failing_liveness(tmp_path):
    server = _server(tmp_path, profile="personal")
    issue = {
        "code": "remote_supervisor_unavailable",
        "component": "remote",
        "severity": "warning",
        "summary": "HASHI2 Remote/HChat is unavailable; local startup will continue.",
        "impact": "Remote/HChat cannot connect to HASHI2.",
        "automatic_retry": False,
        "actions": ["Install and start the instance supervisor."],
    }
    remote = {
        "available": False,
        "enabled": True,
        "supervised": True,
        "action": "supervisor_unavailable",
        "port": 8767,
        "service_name": "hashi-remote-hashi2.service",
    }
    server.orchestrator = SimpleNamespace(
        instance_id="HASHI2",
        api_gateway=None,
        runtimes=[],
        startup_status={
            "phase": "degraded",
            "ready": False,
            "degraded": True,
            "services_ready": True,
            "issues": [issue],
        },
        remote_lifecycle_status=remote,
    )

    response = await server.handle_health(_FakeRequest())
    payload = json.loads(response.text)

    assert payload["ok"] is True
    assert payload["ready"] is False
    assert payload["degraded"] is True
    assert payload["status"] == "degraded"
    assert payload["issues"] == [issue]
    assert payload["remote"] == remote


@pytest.mark.asyncio
async def test_health_is_read_only_and_does_not_reconcile_remote(tmp_path):
    server = _server(tmp_path, profile="personal")
    owner = SimpleNamespace(
        instance_id="HASHI3",
        api_gateway=None,
        runtimes=[],
        _handoff_draining=False,
        startup_status={
            "phase": "degraded",
            "services_ready": True,
            "ready": False,
            "degraded": True,
            "issues": [
                {
                    "code": "remote_already_running_degraded",
                    "component": "remote",
                    "severity": "warning",
                }
            ],
        },
    )

    class _Manager:
        def __init__(self):
            self.remote_calls = 0

        async def reconcile_remote_status(self, *, publish=True):
            self.remote_calls += 1
            owner.startup_status.update(
                phase="ready",
                ready=True,
                degraded=False,
                issues=[],
            )

        def reconcile_connector_status(self, *, publish=True):
            return None

    manager = _Manager()
    owner.startup_manager = manager
    server.orchestrator = owner

    response = await server.handle_health(_FakeRequest())
    payload = json.loads(response.text)

    assert manager.remote_calls == 0
    assert payload["ready"] is False
    assert payload["degraded"] is True
    assert payload["status"] == "degraded"
    assert payload["issues"][0]["component"] == "remote"


@pytest.mark.asyncio
async def test_shared_handoff_rejects_new_http_work_and_can_resume(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    server = _server(tmp_path, profile="personal")
    request_idle = asyncio.Event()
    request_idle.set()
    owner = SimpleNamespace(_handoff_draining=False, _handoff_requests_idle=request_idle)
    server.orchestrator = owner
    entered, finish = asyncio.Event(), asyncio.Event()

    async def work(request):
        entered.set()
        await finish.wait()
        return web.json_response({"saved": True})

    server.app.router.add_post("/test-handoff-work", work)
    async with TestClient(TestServer(server.app)) as client:
        active = asyncio.create_task(client.post("/test-handoff-work"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert owner._handoff_requests == 1
        owner._handoff_draining = True
        assert request_idle.is_set() is False
        rejected = await client.post("/test-handoff-work")
        assert rejected.status == 503
        finish.set()
        assert (await active).status == 200
        assert owner._handoff_requests == 0
        owner._handoff_draining = False
        assert request_idle.is_set() is True
        assert (await client.post("/test-handoff-work")).status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_health_after_local_clone_removal_keeps_unrelated_failures(tmp_path, failed):
    from orchestrator.startup_manager import StartupManager

    states = {"local-clone": "local", "removed-clone": "local"}
    issues = [{"code": "agent_telegram_unavailable", "severity": "warning",
               "details": {"agents": list(states)}}]
    if failed:
        states["failed-agent"] = "failed"
        issues.append({"code": "agent_startup_failed", "severity": "warning"})
    app = SimpleNamespace(
        runtimes=[], api_gateway=None,
        _runtime_map=lambda: {"local-clone": SimpleNamespace(telegram_connected=False)},
        function_workers=SimpleNamespace(telegram_ingress_snapshot=lambda name: {"configured": False}),
        startup_status={"phase": "degraded", "services_ready": True,
                        "agent_order": list(states), "agent_states": states,
                        "agent_reasons": {"removed-clone": "Telegram unavailable",
                                          "local-clone": "Telegram unavailable"},
                        "total": len(states), "completed": len(states),
                        "ready_agents": 2, "failed_agents": int(failed), "issues": issues})
    app.startup_manager = StartupManager(app, None)
    # Connector lifecycle owners reconcile state; health only projects it.
    app.startup_manager.reconcile_connector_status(publish=False)
    server = _server(tmp_path, profile="personal")
    server.orchestrator = app
    payload = json.loads((await server.handle_health(_FakeRequest())).text)
    assert payload["ready"] is (not failed)
    assert payload["degraded"] is failed
    assert [issue["code"] for issue in payload["issues"]] == (["agent_startup_failed"] if failed else [])
    startup = payload["startup"]
    assert "removed-clone" not in startup["agent_states"]
    assert "removed-clone" not in startup["agent_order"]
    assert not startup["agent_reasons"]
    assert startup["total"] == startup["completed"] == 1 + int(failed)
    assert startup["ready_agents"] == 1
    assert startup["local_agents"] == 1
# HASHI-Fingerprint: hfp_69f2e2d4d7ed4ad3b26bc89af0558695



@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["personal", "enterprise"])
async def test_draining_health_is_read_only_and_does_not_admit_new_work(tmp_path, monkeypatch, profile):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    server = _server(tmp_path, profile=profile)
    owner = SimpleNamespace(
        _handoff_draining=True,
        shared_generation_id="retiring",
        kernel_pid=12345,
        runtimes=[
            SimpleNamespace(name="ready-agent", startup_success=True),
            SimpleNamespace(name="not-started", startup_success=False),
        ],
    )
    server.orchestrator = owner
    called = []

    async def effect(request):
        called.append(True)
        return web.json_response({"ok": True})

    def forbidden_health():
        pytest.fail("draining health must not initialize enterprise storage")

    server.app.router.add_post("/test-new-effect", effect)
    monkeypatch.setattr(server, "_enterprise_health_payload", forbidden_health)
    async with TestClient(TestServer(server.app)) as client:
        response = await client.get("/api/health")
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        health = await response.json()
        assert health["ok"] is True and health["ready"] is False
        assert health["status"] == "draining"
        assert health["kernel_pid"] == 12345
        assert health["agents"] == ["ready-agent"]
        assert health["shared_functions"]["generation_id"] == "retiring"
        assert health["shared_functions"]["draining"] is True
        assert (await client.get("/api/version")).status == 503
        assert (await client.post("/test-new-effect")).status == 503
        assert called == []
        owner._handoff_draining = False
        assert (await client.post("/test-new-effect")).status == 200
        assert called == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["sessions", "consumer", "primary", "projection", "feed"])
async def test_handoff_tracks_get_requests_that_can_write(tmp_path, monkeypatch, operation):
    from threading import Event

    from aiohttp.test_utils import TestClient, TestServer
    from orchestrator.session_store import SessionStore
    from tests.test_workbench_scheduler_api import _server as scheduler_server
    from tests.test_scheduler_run_authorization import setup_personal

    server, runtime = scheduler_server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    owner_id = SessionStore.owner_id_for(server.global_config)
    session = server.session_store.ensure_default_session(owner_id=owner_id, agent_id="momo")
    consumer = server.session_store.create_event_consumer(
        session_id=session["session_id"], owner_id=owner_id,
    )
    entered, release = Event(), Event()
    if operation == "feed":
        import orchestrator.frontend_projection as frontend_projection

        original_feed = frontend_projection.poll_frontend_feed

        def slow_feed(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release pending feed")
            return original_feed(*args, **kwargs)

        monkeypatch.setattr(frontend_projection, "poll_frontend_feed", slow_feed)
    method = {"sessions": "list_sessions", "consumer": "poll_event_consumer"}.get(operation, "resolve_primary_session")
    original = getattr(server.session_store, method)

    def slow(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test did not release pending read")
        return original(*args, **kwargs)

    if operation != "feed":
        monkeypatch.setattr(server.session_store, method, slow)
    idle = asyncio.Event()
    idle.set()
    owner = SimpleNamespace(
        _handoff_draining=False, _shared_committed=True,
        _handoff_requests_idle=idle, runtimes=[runtime],
    )
    server.orchestrator = owner
    headers = {"X-Workbench-Token": server.admin_token}
    path = {
        "sessions": "/api/v1/sessions",
        "consumer": f"/api/v1/sessions/{session['session_id']}/events?consumer_id={consumer['consumer_id']}",
        "primary": "/api/v1/agents/momo/primary-session",
        "projection": "/api/chat-projection/momo",
        "feed": f"/api/v2/frontend/sessions/{session['session_id']}/feed",
    }[operation]
    async with TestClient(TestServer(server.app)) as client:
        pending = asyncio.create_task(client.get(path, headers=headers))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            assert owner._handoff_requests == 1
            assert not idle.is_set()
            owner._handoff_draining = True
            assert (await client.post("/api/v1/sessions", headers=headers, json={})).status == 503
        finally:
            release.set()
        assert (await pending).status == 200
        assert owner._handoff_requests == 0
        assert idle.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["personal", "enterprise"])
async def test_committed_drain_allows_owner_run_status_only_without_new_work(tmp_path, profile):
    from aiohttp.test_utils import TestClient, TestServer
    from orchestrator.enterprise import IdentityService
    from orchestrator.session_store import SessionStore
    from tests.test_workbench_scheduler_api import _server as scheduler_server
    from tests.test_scheduler_run_authorization import setup_personal

    server, runtime = scheduler_server(tmp_path, persistent_session_v1=True)
    setup_personal(server, runtime)
    headers = {"X-Workbench-Token": server.admin_token}
    foreign_headers = None
    if profile == "enterprise":
        identity = IdentityService.from_path(tmp_path / "test-enterprise.sqlite")
        identity.create_organization(org_id="ORG-001", name="Fixture")
        for name in ("alice", "bob"):
            identity.create_user(org_id="ORG-001", email=f"{name}@example.invalid",
                                 display_name=name, password="fixture-only-password", user_id=name)
        headers = {"Authorization": "Bearer " + identity.create_session(user_id="alice").token}
        foreign_headers = {"Authorization": "Bearer " + identity.create_session(user_id="bob").token}
        server.identity_service = identity
        server.global_config.deployment_profile = "enterprise"
        server.global_config.organization_id = "ORG-001"
        owner_id = "enterprise:alice"
    else:
        owner_id = SessionStore.owner_id_for(server.global_config)
    store = server.session_store
    session = store.ensure_default_session(owner_id=owner_id, agent_id="momo")
    accepted = store.accept_run(
        session_id=session["session_id"], owner_id=owner_id, agent_id="momo",
        request_id="read-only-drain-fixture", text="fixture", source="test",
        idempotency_key="read-only-drain-fixture",
    )
    store.mark_request_running("read-only-drain-fixture", worker_id="test-worker")
    # Listing this legacy placeholder normally repairs its title; drain must not.
    with store._connection() as connection:
        connection.execute(
            "UPDATE sessions SET title='New session', title_source='system' WHERE session_id=?",
            (session["session_id"],),
        )
    before = store.get_run(accepted.run_id, owner_id=owner_id)
    before_session = store.get_session(session["session_id"], owner_id=owner_id)
    before_events = store.events(session["session_id"], owner_id=owner_id)
    owner = SimpleNamespace(_handoff_draining=True, _shared_committed=True, runtimes=[runtime])
    server.orchestrator = owner
    base = f"/api/v1/sessions/{session['session_id']}"
    route = f"{base}/runs/{accepted.run_id}"
    async with TestClient(TestServer(server.app)) as client:
        response = await client.get(route, headers=headers)
        assert response.status == 200, await response.text()
        assert (await response.json())["run"]["run_id"] == accepted.run_id
        for path in ("/api/v1/sessions", base, base + "/snapshot", base + "/messages", base + "/events"):
            response = await client.get(path, headers=headers)
            assert response.status == 200, (path, await response.text())
            assert response.headers["Cache-Control"] == "no-store"
            assert (await client.get(path)).status == 401
            if foreign_headers:
                foreign = await client.get(path, headers=foreign_headers)
                if path == "/api/v1/sessions":
                    assert foreign.status == 200
                    assert (await foreign.json())["sessions"] == []
                else:
                    assert foreign.status == 404
        wrong_session = f"/api/v1/sessions/other/runs/{accepted.run_id}"
        assert (await client.get(wrong_session, headers=headers)).status == 404
        assert (await client.get(route)).status == 401
        if foreign_headers:
            assert (await client.get(route, headers=foreign_headers)).status == 404
        # Consumer polling mutates its cursor even though it is a GET.
        assert (await client.get(base + "/events?consumer_id=fixture", headers=headers)).status == 503
        assert (await client.post(route + "/cancel", headers=headers, json={})).status == 503
        assert (await client.post(base + "/runs", headers=headers, json={})).status == 503
        assert store.get_run(accepted.run_id, owner_id=owner_id) == before
        assert store.get_session(session["session_id"], owner_id=owner_id) == before_session
        assert store.events(session["session_id"], owner_id=owner_id) == before_events
        owner._shared_committed = False
        assert (await client.get(route, headers=headers)).status == 503
        assert (await client.get(base + "/snapshot", headers=headers)).status == 503
        assert (await client.get("/api/v1/sessions", headers=headers)).status == 503
        owner._handoff_draining = False
        assert (await client.get("/api/v1/sessions", headers=headers)).status == 200
        assert store.get_session(session["session_id"], owner_id=owner_id)["title_source"] == "maintenance"
