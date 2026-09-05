from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.capability_broker import (
    CAPABILITY_PROTOCOL_VERSION,
    CapabilityBroker,
    CapabilityBrokerError,
    CapabilityLeaseConflict,
)
from orchestrator.service_endpoints import ServiceEndpoint


def _kernel(tmp_path: Path, instance_id: str = "HASHI3") -> SimpleNamespace:
    return SimpleNamespace(
        global_cfg=SimpleNamespace(instance_id=instance_id),
        paths=SimpleNamespace(bridge_home=tmp_path, instance_id=instance_id),
    )


def _workbench(instance_id: str = "HASHI3", port: int = 19432) -> ServiceEndpoint:
    return ServiceEndpoint(
        service="workbench",
        instance_id=instance_id,
        scheme="http",
        host="172.29.144.7",
        port=port,
        revision=1,
        published_at=time.time(),
    )


def _payload(
    token: str,
    *,
    kind: str = "computer_control",
    capability_id: str = "cap-computer",
    instance_id: str = "HASHI3",
    port: int = 49321,
    device_id: str = "device-a",
    session_id: str = "user-a:interactive",
    actions: tuple[str, ...] = ("click", "screenshot"),
) -> dict:
    return {
        "schema_version": 1,
        "capability_id": capability_id,
        "capability_kind": kind,
        "instance_id": instance_id,
        "device_id": device_id,
        "user_session_id": session_id,
        "platform": "windows",
        "transport_kind": "authenticated_http_loopback",
        "negotiated_endpoint": f"http://127.0.0.1:{port}",
        "protocol_version": CAPABILITY_PROTOCOL_VERSION,
        "supported_actions": list(actions),
        "worker_pid_and_generation": {
            "pid": 4242,
            "generation": "worker-generation-a",
        },
        "authorization_key_id": hashlib.sha256(token.encode()).hexdigest()[:24],
        "health_and_expiry": {"ttl_seconds": 90},
        "worker_token": token,
    }


def _health(registration) -> dict:
    return {
        "ok": True,
        "protocol_version": CAPABILITY_PROTOCOL_VERSION,
        "identity": {
            "instance_id": registration.instance_id,
            "capability_id": registration.capability_id,
            "device_id": registration.device_id,
            "user_session_id": registration.user_session_id,
        },
        "capability_kind": registration.capability_kind,
        "supported_actions": list(registration.supported_actions),
    }


def _started_broker(tmp_path: Path) -> tuple[CapabilityBroker, str]:
    broker = CapabilityBroker(_kernel(tmp_path))
    receipt = broker.start(_workbench())
    bootstrap = json.loads(broker.bootstrap_path.read_text(encoding="utf-8"))
    assert receipt["registration_url"] == (
        "http://172.29.144.7:19432/api/device-capabilities/register"
    )
    assert "bootstrap_token" not in receipt
    broker.set_registration_probe_for_testing(lambda registration, _token: _health(registration))
    broker._schedule_cleanup = lambda *_args, **_kwargs: None
    return broker, bootstrap["bootstrap_token"]


def test_bootstrap_uses_actual_nondefault_workbench_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "orchestrator.capability_broker.discover_worker_callback_hosts",
        lambda: ("172.29.144.1", "127.0.0.1"),
    )
    broker = CapabilityBroker(_kernel(tmp_path))

    broker.start(_workbench(port=24567))

    saved = json.loads(broker.bootstrap_path.read_text(encoding="utf-8"))
    assert saved["instance_id"] == "HASHI3"
    assert saved["registration_url"].startswith("http://172.29.144.7:24567/")
    assert saved["worker_callback_hosts"] == ["172.29.144.1", "127.0.0.1"]
    assert len(saved["bootstrap_token"]) >= 48


def test_registration_requires_bootstrap_auth_and_live_identity_handshake(tmp_path):
    broker, bootstrap_token = _started_broker(tmp_path)
    token = "w" * 64

    with pytest.raises(CapabilityBrokerError, match="bootstrap authentication"):
        broker.register(_payload(token), bootstrap_token="wrong")

    broker.set_registration_probe_for_testing(
        lambda registration, _token: {
            **_health(registration),
            "identity": {
                **_health(registration)["identity"],
                "instance_id": "HASHI1",
            },
        }
    )
    with pytest.raises(CapabilityBrokerError, match="identity mismatch"):
        broker.register(_payload(token), bootstrap_token=bootstrap_token)


def test_wrong_instance_registration_is_rejected_before_publication(tmp_path):
    broker, bootstrap_token = _started_broker(tmp_path)

    with pytest.raises(CapabilityBrokerError, match="cross-instance"):
        broker.register(
            _payload("x" * 64, instance_id="HASHI1"),
            bootstrap_token=bootstrap_token,
        )

    assert broker.status()["capabilities"] == []


@pytest.mark.asyncio
async def test_invoke_verifies_identity_releases_lease_and_redacts_argument_values(tmp_path):
    broker, bootstrap_token = _started_broker(tmp_path)
    token = "a" * 64
    registration = broker.register(
        _payload(token, actions=("type",)),
        bootstrap_token=bootstrap_token,
    )
    received = []

    async def transport(record, worker_token, payload, _timeout):
        received.append((record, worker_token, payload))
        return {
            "ok": True,
            "identity": {
                "instance_id": record.instance_id,
                "capability_id": record.capability_id,
                "device_id": record.device_id,
                "user_session_id": record.user_session_id,
            },
            "result": "typed",
        }

    broker.set_transport_for_testing(transport)

    result = await broker.invoke(
        "computer_control",
        "type",
        {"text": "do-not-log-this-secret"},
        agent_id="agent1",
        task_id="task-1",
        request_id="request-1",
    )

    assert result == "typed"
    assert received[0][0] == registration
    assert received[0][1] == token
    assert received[0][2]["lease"]["task_id"] == "task-1"
    assert broker.status()["leases"] == []
    audit = broker.audit_path.read_text(encoding="utf-8")
    assert "do-not-log-this-secret" not in audit
    assert '"argument_names": ["text"]' in audit


@pytest.mark.asyncio
async def test_browser_observation_receives_control_lease(tmp_path):
    broker, bootstrap_token = _started_broker(tmp_path)
    token = "f" * 64
    broker.register(
        _payload(
            token,
            kind="browser_control",
            capability_id="cap-browser-read",
            actions=("get_text",),
        ),
        bootstrap_token=bootstrap_token,
    )
    received = []

    async def transport(record, _worker_token, payload, _timeout):
        received.append(payload)
        return {
            "ok": True,
            "identity": _health(record)["identity"],
            "result": "page text",
        }

    broker.set_transport_for_testing(transport)

    assert await broker.invoke(
        "browser_control",
        "get_text",
        {"url": "https://example.invalid"},
        agent_id="agent1",
        task_id="task-browser-read",
    ) == "page text"
    assert received[0]["lease"]["task_id"] == "task-browser-read"


@pytest.mark.asyncio
async def test_failed_mutation_schedules_best_effort_input_cleanup(tmp_path):
    broker, bootstrap_token = _started_broker(tmp_path)
    token = "j" * 64
    broker.register(
        _payload(token, actions=("click",)),
        bootstrap_token=bootstrap_token,
    )
    cleanups = []
    broker._schedule_cleanup = lambda registration, worker_token, **kwargs: cleanups.append(
        (registration, worker_token, kwargs)
    )

    async def transport(record, _worker_token, _payload, _timeout):
        return {
            "ok": False,
            "identity": _health(record)["identity"],
            "error": "input rejected",
        }

    broker.set_transport_for_testing(transport)

    with pytest.raises(CapabilityBrokerError, match="input rejected"):
        await broker.invoke(
            "computer_control",
            "click",
            {"x": 1, "y": 2},
            agent_id="agent1",
            task_id="task-failed",
        )

    assert len(cleanups) == 1
    assert cleanups[0][1] == token
    assert cleanups[0][2]["reason"] == "action-failed"


def test_two_agents_cannot_hold_same_device_session_write_lease(tmp_path):
    broker, bootstrap_token = _started_broker(tmp_path)
    registration = broker.register(
        _payload("b" * 64),
        bootstrap_token=bootstrap_token,
    )
    first = broker.acquire_lease(
        registration,
        agent_id="agent1",
        task_id="task-a",
    )

    with pytest.raises(CapabilityLeaseConflict):
        broker.acquire_lease(
            registration,
            agent_id="agent2",
            task_id="task-b",
        )

    assert broker.cancel_task(agent_id="agent1", task_id="task-a") == 1
    second = broker.acquire_lease(
        registration,
        agent_id="agent2",
        task_id="task-b",
    )
    assert second.lease_id != first.lease_id


def test_different_window_labels_still_share_one_physical_device_lease(tmp_path):
    broker, bootstrap_token = _started_broker(tmp_path)
    registration = broker.register(
        _payload("g" * 64),
        bootstrap_token=bootstrap_token,
    )
    broker.acquire_lease(
        registration,
        agent_id="agent1",
        task_id="task-a",
        window_id="window-a",
    )

    with pytest.raises(CapabilityLeaseConflict):
        broker.acquire_lease(
            registration,
            agent_id="agent2",
            task_id="task-b",
            window_id="window-b",
        )


def test_browser_to_computer_handoff_preserves_device_session_and_owner(tmp_path):
    broker, bootstrap_token = _started_broker(tmp_path)
    browser = broker.register(
        _payload(
            "c" * 64,
            kind="browser_control",
            capability_id="cap-browser",
            port=49322,
            actions=("click", "upload"),
        ),
        bootstrap_token=bootstrap_token,
    )
    computer = broker.register(
        _payload(
            "d" * 64,
            kind="computer_control",
            capability_id="cap-computer",
            port=49323,
            actions=("click", "window_focus"),
        ),
        bootstrap_token=bootstrap_token,
    )
    source = broker.acquire_lease(
        browser,
        agent_id="agent1",
        task_id="upload-task",
        window_id="tab-7",
    )

    target = broker.handoff(
        source.lease_id,
        target_capability_kind="computer_control",
        agent_id="agent1",
        task_id="upload-task",
        window_id="dialog-9",
    )

    assert target.capability_id == computer.capability_id
    assert target.device_id == source.device_id
    assert target.user_session_id == source.user_session_id
    assert target.agent_id == source.agent_id
    assert target.task_id == source.task_id
    assert source.lease_id not in {row["lease_id"] for row in broker.status()["leases"]}


@pytest.mark.asyncio
async def test_explicit_lease_can_invoke_handoff_and_release_as_one_task(tmp_path):
    broker, bootstrap_token = _started_broker(tmp_path)
    browser = broker.register(
        _payload(
            "h" * 64,
            kind="browser_control",
            capability_id="cap-browser-flow",
            port=49330,
            actions=("upload",),
        ),
        bootstrap_token=bootstrap_token,
    )
    computer = broker.register(
        _payload(
            "i" * 64,
            kind="computer_control",
            capability_id="cap-computer-flow",
            port=49331,
            actions=("window_focus",),
        ),
        bootstrap_token=bootstrap_token,
    )
    requests = []

    async def transport(record, _worker_token, payload, _timeout):
        requests.append((record.capability_id, payload["lease"]["lease_id"]))
        return {
            "ok": True,
            "identity": _health(record)["identity"],
            "result": "ok",
        }

    broker.set_transport_for_testing(transport)
    source = broker.acquire_for_task(
        "browser_control",
        action="upload",
        agent_id="agent1",
        task_id="upload-flow",
        window_id="tab-7",
    )
    assert source.capability_id == browser.capability_id
    assert await broker.invoke(
        "browser_control",
        "upload",
        {},
        agent_id="agent1",
        task_id="upload-flow",
        lease_id=source.lease_id,
    ) == "ok"
    assert {row["lease_id"] for row in broker.status()["leases"]} == {
        source.lease_id
    }

    target = broker.handoff(
        source.lease_id,
        target_capability_kind="computer_control",
        agent_id="agent1",
        task_id="upload-flow",
        window_id="file-dialog",
    )
    assert target.capability_id == computer.capability_id
    assert await broker.invoke(
        "computer_control",
        "window_focus",
        {"window_id": "file-dialog"},
        agent_id="agent1",
        task_id="upload-flow",
        lease_id=target.lease_id,
    ) == "ok"
    assert broker.release_task_lease(
        target.lease_id,
        agent_id="agent1",
        task_id="upload-flow",
    ) is True
    assert broker.status()["leases"] == []
    assert requests == [
        (browser.capability_id, source.lease_id),
        (computer.capability_id, target.lease_id),
    ]


def test_heartbeat_revalidates_health_and_removes_unhealthy_capability(tmp_path):
    broker, bootstrap_token = _started_broker(tmp_path)
    token = "e" * 64
    registration = broker.register(
        _payload(token),
        bootstrap_token=bootstrap_token,
    )
    lease = broker.acquire_lease(
        registration,
        agent_id="agent1",
        task_id="task-a",
    )
    broker.set_registration_probe_for_testing(
        lambda _registration, _token: {"ok": False}
    )

    with pytest.raises(CapabilityBrokerError, match="heartbeat health check failed"):
        broker.heartbeat(
            registration.capability_id,
            worker_token=token,
            identity=_health(registration)["identity"],
        )

    status = broker.status()
    assert status["capabilities"] == []
    assert status["leases"] == []
    audit = broker.audit_path.read_text(encoding="utf-8")
    assert lease.lease_id in audit
    assert "capability_unhealthy" in audit
