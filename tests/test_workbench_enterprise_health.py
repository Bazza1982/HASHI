from __future__ import annotations

import json
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.workbench_api import WorkbenchApiServer


class _FakeRequest:
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_connector_commit_health_and_recovery_follow_real_ingress(tmp_path, monkeypatch, persistent):
    from orchestrator.runtime_app_host import RuntimeAppHost
    from orchestrator.startup_manager import StartupManager
    from orchestrator.telegram_ingress import CoreTelegramIngress
    from orchestrator.function_worker_supervisor import AgentRuntimeHandle, FunctionWorkerSupervisor

    monkeypatch.setattr("orchestrator.runtime_app_host.CONNECTOR_RETRY_SECONDS", 0.01)
    handles = {}
    class WorkerClient:
        def __init__(self, name):
            self.agent_name, self.fail_status = name, False
        async def call(self, method, params, **kwargs):
            if method == "worker.telegram_status":
                if self.fail_status:
                    raise ConnectionResetError("worker IPC disconnected")
                return {"metadata": {"worker_phase": "ACTIVE", "worker_accepting": True,
                                     "telegram_connected": params["connected"]}}
            assert method == "runtime.enqueue_startup_bootstrap"
            return True
    reports = []
    class Bot:
        def __init__(self, name): self.name, self.attempts = name, 0
        async def initialize(self):
            self.attempts += 1
            if self.name == "alpha" and (persistent or self.attempts == 1):
                raise OSError("connection unavailable")
        async def delete_webhook(self, **kwargs): pass
        async def shutdown(self): pass
        async def get_updates(self, **kwargs): await asyncio.Event().wait()

    app = SimpleNamespace(paths=SimpleNamespace(bridge_home=tmp_path, instance_id="TEST"),
        runtimes=[], shared_generation_id="test", api_gateway=None, whatsapp=None,
        _handoff_draining=True, _runtime_map=lambda: handles,
        _load_whatsapp_cfg=lambda: ({}, {}), global_cfg=SimpleNamespace(authorized_id=1),
        startup_status={"agent_order": ["alpha", "beta"], "agent_states": {n:"local" for n in ["alpha", "beta"]},
            "services_ready": True, "ready": False, "issues": [
                {"code": "agent_telegram_unavailable", "severity": "warning", "details": {"agents": ["alpha", "beta"]}}]})
    for name in ["alpha", "beta"]:
        handles[name] = AgentRuntimeHandle(app, WorkerClient(name),
            {"worker_phase": "ACTIVE", "worker_accepting": True, "telegram_connected": False})
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
    try:
        result = await host.commit()
        assert result["degraded"] and len(reports) == 1
        assert reports[0]["beta"]["connected"] and reports[0]["beta"]["running"]
        assert not reports[0]["alpha"]["connected"]
        payload = json.loads((await server.handle_health(_FakeRequest())).text)
        assert payload["degraded"] and not payload["ready"]
        affected = next(i for i in payload["issues"] if i["code"] == "agent_telegram_unavailable")
        assert affected["details"]["agents"] == ["alpha"]
        if persistent:
            await host._activate_connectors()
            assert not app.startup_status["ready"]
            assert app.startup_status["degraded"]
        else:
            await asyncio.wait_for(host.connector_task, timeout=2)
            payload = json.loads((await server.handle_health(_FakeRequest())).text)
            assert payload["ready"] and not payload["degraded"] and not payload["issues"]
            assert all(item["connected"] and item["running"] for item in reports[-1].values())
            # A subsequent real ingress transition must update the same health owner.
            await ingress["beta"]._set_connected(False)
            assert app.startup_status["degraded"] and not app.startup_status["ready"]
            await ingress["beta"]._set_connected(True)
            assert app.startup_status["ready"] and not app.startup_status["issues"]
            # Stale Worker metadata must not conceal an actual ingress failure.
            handles["beta"].client.fail_status = True
            await ingress["beta"]._set_connected(False)
            assert handles["beta"].telegram_connected  # IPC could not update the Worker.
            assert app.startup_status["degraded"] and not app.startup_status["ready"]
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
            "accepting": True,
            "alive": True,
            "telegram_ingress": {
                "running": True,
                "connected": True,
                "offset": 99,
            },
        }
    ]


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
async def test_shared_handoff_rejects_new_http_work_and_can_resume(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    server = _server(tmp_path, profile="personal")
    owner = SimpleNamespace(_handoff_draining=False)
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
        rejected = await client.post("/test-handoff-work")
        assert rejected.status == 503
        finish.set()
        assert (await active).status == 200
        assert owner._handoff_requests == 0
        owner._handoff_draining = False
        assert (await client.post("/test-handoff-work")).status == 200
