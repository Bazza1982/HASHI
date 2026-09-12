from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.capability_broker import CapabilityBrokerError
from orchestrator.workbench_api import WorkbenchApiServer


class _Request:
    def __init__(self, payload: dict, headers: dict | None = None) -> None:
        self.payload = payload
        self.headers = headers or {}

    async def json(self):
        return self.payload


class _Registration:
    def to_dict(self):
        return {
            "instance_id": "HASHI3",
            "capability_id": "cap-1",
            "capability_kind": "computer_control",
        }


class _Broker:
    def __init__(self) -> None:
        self.register_calls = []
        self.heartbeat_calls = []

    def register(self, payload, *, bootstrap_token):
        self.register_calls.append((payload, bootstrap_token))
        if bootstrap_token != "bootstrap-secret":
            raise CapabilityBrokerError("capability bootstrap authentication failed")
        return _Registration()

    def heartbeat(self, capability_id, **kwargs):
        self.heartbeat_calls.append((capability_id, kwargs))
        if kwargs.get("worker_token") != "worker-secret":
            raise CapabilityBrokerError("capability heartbeat authentication failed")
        return _Registration()

    def status(self):
        return {
            "instance_id": "HASHI3",
            "capabilities": [_Registration().to_dict()],
            "leases": [],
        }


def _server(tmp_path: Path) -> tuple[WorkbenchApiServer, _Broker, list[str]]:
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {
                    "instance_id": "HASHI3",
                    "deployment_profile": "personal",
                },
                "agents": [],
            }
        ),
        encoding="utf-8",
    )
    global_config = SimpleNamespace(
        instance_id="HASHI3",
        deployment_profile="personal",
        organization_id=None,
        bridge_home=tmp_path,
        project_root=tmp_path,
        workbench_port=18804,
    )
    broker = _Broker()
    broadcasts = []

    async def broadcast_topology():
        broadcasts.append("broadcast")

    orchestrator = SimpleNamespace(
        capability_broker=broker,
        function_workers=SimpleNamespace(broadcast_topology=broadcast_topology),
        runtimes=[],
    )
    server = WorkbenchApiServer(
        config_path,
        global_config,
        orchestrator=orchestrator,
    )
    return server, broker, broadcasts


@pytest.mark.asyncio
async def test_registration_requires_bootstrap_and_broadcasts_topology(tmp_path):
    server, broker, broadcasts = _server(tmp_path)
    payload = {"capability_id": "cap-1"}

    denied = await server.handle_device_capability_register(
        _Request(payload, {"X-HASHI-Capability-Bootstrap": "wrong"})
    )
    accepted = await server.handle_device_capability_register(
        _Request(
            payload,
            {"X-HASHI-Capability-Bootstrap": "bootstrap-secret"},
        )
    )

    assert denied.status == 403
    assert accepted.status == 201
    assert json.loads(accepted.text)["registration"]["instance_id"] == "HASHI3"
    assert broker.register_calls[-1] == (payload, "bootstrap-secret")
    assert broadcasts == ["broadcast"]


@pytest.mark.asyncio
async def test_heartbeat_requires_worker_bearer_token(tmp_path):
    server, broker, broadcasts = _server(tmp_path)
    payload = {
        "capability_id": "cap-1",
        "identity": {"instance_id": "HASHI3"},
        "ttl_seconds": 45,
    }

    denied = await server.handle_device_capability_heartbeat(
        _Request(payload, {"Authorization": "Bearer wrong"})
    )
    accepted = await server.handle_device_capability_heartbeat(
        _Request(payload, {"Authorization": "Bearer worker-secret"})
    )

    assert denied.status == 403
    assert accepted.status == 200
    assert broker.heartbeat_calls[-1][1]["ttl_seconds"] == 45.0
    # Both a rejected heartbeat that may have evicted an unhealthy Worker and
    # a successful lease extension refresh the Function-side catalogue.
    assert broadcasts == ["broadcast", "broadcast"]


@pytest.mark.asyncio
async def test_status_uses_canonical_broker_snapshot(tmp_path):
    server, _broker, _broadcasts = _server(tmp_path)

    response = await server.handle_device_capability_status(_Request({}))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["instance_id"] == "HASHI3"
    assert payload["capabilities"][0]["capability_kind"] == "computer_control"
