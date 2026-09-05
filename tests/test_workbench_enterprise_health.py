from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.workbench_api import WorkbenchApiServer


class _FakeRequest:
    pass


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
