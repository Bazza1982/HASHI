from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import types
from contextlib import contextmanager
from pathlib import Path
from urllib import request as urllib_request
from urllib.error import HTTPError

import pytest

import tools.device_control_worker as device_worker
from tools.windows_helper import win32 as windows_win32

from tools.device_control_worker import (
    CAPABILITY_PROTOCOL_VERSION,
    DeviceWorkerBootstrapPending,
    DeviceWorkerServer,
    DeviceWorkerError,
    DeviceWorkerState,
    select_worker_bind_host,
)


def _state(tmp_path: Path) -> DeviceWorkerState:
    calls = []

    async def execute(action, arguments):
        calls.append((action, arguments))
        return {"action": action, "argument_names": sorted(arguments)}

    state = DeviceWorkerState(
        bridge_home=tmp_path,
        capability_kind="browser_control",
        instance_id="HASHI3",
        device_id="device-test",
        user_session_id="user-test:interactive",
        worker_token="worker-token-" + "x" * 48,
        bind_host="127.0.0.1",
        advertise_host="127.0.0.1",
        wsl_distro=None,
        logger=logging.getLogger(f"test-device-worker-{id(tmp_path)}"),
        executor=execute,
    )
    state.test_calls = calls
    state.health = lambda: {
        "ok": True,
        "protocol_version": CAPABILITY_PROTOCOL_VERSION,
        "identity": state.identity,
        "capability_kind": state.capability_kind,
        "supported_actions": sorted(state.supported_actions),
    }
    return state


@pytest.mark.asyncio
async def test_browser_worker_stays_on_its_registered_extension(tmp_path, monkeypatch):
    state = _state(tmp_path)
    state.executor = None
    observed = []

    async def execute(_action, args):
        observed.append(args)
        return "ok"

    monkeypatch.setattr(device_worker, "_execute_browser_action", execute)
    with pytest.raises(DeviceWorkerError, match="cdp_url is not allowed"):
        await state.execute("get_text", {"cdp_url": "http://127.0.0.1:9222"})
    assert await state.execute("get_text", {"url": "https://example.test"}) == "ok"
    assert observed[0]["bridge_backend"] == "extension"
    assert observed[0]["_bound_browser_worker"] is True


def test_browser_workers_have_distinct_registration_and_status_identity(tmp_path):
    chrome = _state(tmp_path)
    edge = _state(tmp_path)
    chrome.browser_id = "chrome"
    edge.browser_id = "edge"
    edge.browser_name = "Microsoft Edge"
    edge.bound_port = 49322
    assert chrome.capability_id != edge.capability_id
    assert chrome.status_path != edge.status_path
    registration = device_worker._registration_payload(edge)
    assert registration["browser_id"] == "edge"
    assert registration["browser_name"] == "Microsoft Edge"


def _write_bootstrap(
    tmp_path: Path,
    *,
    instance_id: str = "HASHI3",
    callback_hosts: list[str] | None = None,
) -> None:
    path = tmp_path / "state" / "device_control" / "bootstrap.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "protocol_version": CAPABILITY_PROTOCOL_VERSION,
                "instance_id": instance_id,
                "registration_url": "http://127.0.0.1:18804/register",
                "heartbeat_url": "http://127.0.0.1:18804/heartbeat",
                "bootstrap_token": "b" * 64,
                "worker_callback_hosts": callback_hosts
                or ["172.29.144.1", "127.0.0.1"],
            }
        ),
        encoding="utf-8",
    )


def test_auto_bind_selects_first_core_published_host_that_is_local(tmp_path):
    _write_bootstrap(tmp_path)
    checked = []

    def can_bind(host: str) -> bool:
        checked.append(host)
        return host == "172.29.144.1"

    assert select_worker_bind_host(
        tmp_path,
        "HASHI3",
        "auto",
        can_bind=can_bind,
    ) == "172.29.144.1"
    assert checked == ["172.29.144.1"]


def test_auto_bind_rejects_callback_receipt_from_another_instance(tmp_path):
    _write_bootstrap(tmp_path, instance_id="HASHI1")

    with pytest.raises(DeviceWorkerError, match="another instance"):
        select_worker_bind_host(
            tmp_path,
            "HASHI3",
            "auto",
            can_bind=lambda _host: True,
        )


def test_auto_bind_follows_replaced_core_gateway_receipt(tmp_path):
    _write_bootstrap(tmp_path, callback_hosts=["172.29.144.1", "127.0.0.1"])
    first = select_worker_bind_host(
        tmp_path,
        "HASHI3",
        "auto",
        can_bind=lambda _host: True,
    )
    _write_bootstrap(tmp_path, callback_hosts=["172.30.0.1", "127.0.0.1"])
    second = select_worker_bind_host(
        tmp_path,
        "HASHI3",
        "auto",
        can_bind=lambda _host: True,
    )

    assert first == "172.29.144.1"
    assert second == "172.30.0.1"


def test_auto_bind_waits_when_core_bootstrap_is_not_ready(tmp_path):
    with pytest.raises(DeviceWorkerBootstrapPending, match="unavailable"):
        select_worker_bind_host(
            tmp_path,
            "HASHI3",
            "auto",
            can_bind=lambda _host: True,
        )


def test_non_loopback_worker_reports_authenticated_host_gateway_transport(tmp_path):
    state = _state(tmp_path)
    state.advertise_host = "172.29.144.1"
    state.bound_port = 49321

    payload = device_worker._registration_payload(state)

    assert payload["transport_kind"] == "authenticated_http_host_gateway"


def test_embedded_launch_receipt_is_published_only_after_authenticated_registration(tmp_path, monkeypatch):
    state = _state(tmp_path)
    state.browser_provider = "embedded"
    state.launch_id = "7c14ee98-373c-45a0-b243-eb7f08217f43"
    state.bound_port = 49123
    responses = {"ok": True, "registration": {"instance_id": "OTHER"}}
    calls = []

    def register(_url, **kwargs):
        calls.append(kwargs)
        return responses

    monkeypatch.setattr(device_worker, "_http_json", register)
    bootstrap = {"registration_url": "http://127.0.0.1/register", "bootstrap_token": "b" * 64}
    with pytest.raises(DeviceWorkerError, match="identity mismatch"):
        device_worker._register(state, bootstrap)
    assert not state.status_path.exists()
    responses["registration"]["instance_id"] = state.instance_id
    device_worker._register(state, bootstrap)
    receipt = json.loads(state.status_path.read_text(encoding="utf-8"))
    assert receipt["launch_id"] == state.launch_id
    assert receipt["provider_id"] == "embedded"
    assert receipt["pid"] == os.getpid()
    assert receipt["registered_at"] > 0
    assert calls[-1]["headers"]["X-HASHI-Capability-Bootstrap"] == bootstrap["bootstrap_token"]
    # A launch receipt correlates a local process; it is not a capability grant.
    assert "launch_id" not in calls[-1]["payload"]


@contextmanager
def _running_server(tmp_path: Path):
    state = _state(tmp_path)
    server = DeviceWorkerServer(("127.0.0.1", 0), state)
    state.bound_port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _request(
    state: DeviceWorkerState,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib_request.Request(
        state.endpoint + path,
        data=data,
        headers={
            "Authorization": f"Bearer {token or state.worker_token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib_request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def _action_payload(
    state: DeviceWorkerState,
    *,
    request_id: str,
    action: str,
    lease: dict | None = None,
) -> dict:
    return {
        "protocol_version": CAPABILITY_PROTOCOL_VERSION,
        "request_id": request_id,
        "identity": state.identity,
        "agent_id": "agent1",
        "task_id": "task-1",
        "action": action,
        "args": {"selector": "#safe", "_authorized_roots": [str(state.bridge_home)]},
        "lease": lease,
    }


def _lease(state: DeviceWorkerState) -> dict:
    return {
        "lease_id": "lease-1",
        "capability_id": state.capability_id,
        "capability_kind": state.capability_kind,
        "instance_id": state.instance_id,
        "device_id": state.device_id,
        "user_session_id": state.user_session_id,
        "window_id": None,
        "agent_id": "agent1",
        "task_id": "task-1",
        "issued_at": time.time(),
        "expires_at": time.time() + 30,
    }


def test_health_requires_worker_token_and_returns_full_identity(tmp_path):
    with _running_server(tmp_path) as state:
        status, denied = _request(state, "/health", token="wrong")
        ok_status, health = _request(state, "/health")

    assert status == 401
    assert denied["ok"] is False
    assert ok_status == 200
    assert health["identity"] == state.identity
    assert health["protocol_version"] == CAPABILITY_PROTOCOL_VERSION


def test_desktop_failure_preserves_safe_rpc_and_bounded_diagnostics(tmp_path, caplog):
    from orchestrator.desktop_contract import DesktopError
    from types import SimpleNamespace

    with _running_server(tmp_path) as state:
        state.capability_kind = "computer_control"
        caplog.set_level(logging.INFO, logger=state.logger.name)
        def denied(*_args):
            native = OSError("private-input-text-and-window-title")
            native.winerror = 0
            native.desktop_diagnostics = {"input_desktop_interactive": True,
                "thread_on_input_desktop": True, "window_station_visible": True,
                "cursor_at_requested_position": False, "input_text": "private-input-text"}
            raise DesktopError("desktop_input_failed", 503) from native
        state.manual_desktop = lambda: SimpleNamespace(handle=denied)
        payload = _action_payload(state, request_id="desktop-safe-failure", action="desktop_input")
        payload.update(worker_generation=device_worker._generation_id("computer_control"),
            actor={"type": "user", "id": "owner-safe"}, desktop_session_id="session-safe")
        status, result = _request(state, "/action", method="POST", payload=payload)
    assert status == 200 and result["status"] == 503
    assert result["error_code"] == "desktop_input_failed"
    assert "private-input" not in json.dumps(result)
    rows = [json.loads(record.message) for record in caplog.records
            if '"event": "desktop_action_failed"' in record.message]
    assert len(rows) == 1
    assert rows[0]["request_id"] == "desktop-safe-failure"
    assert rows[0]["os_error"] == 0
    assert rows[0]["input_desktop_interactive"] is True
    assert rows[0]["cursor_at_requested_position"] is False
    assert "private-input" not in json.dumps(rows[0])


def test_observation_executes_without_write_lease_and_replay_is_rejected(tmp_path):
    with _running_server(tmp_path) as state:
        state.capability_kind = "computer_control"
        payload = _action_payload(
            state,
            request_id="request-observe",
            action="info",
        )
        status, result = _request(state, "/action", method="POST", payload=payload)
        replay_status, replay = _request(
            state,
            "/action",
            method="POST",
            payload=payload,
        )

    assert status == 200
    assert result["ok"] is True
    assert result["identity"] == state.identity
    assert state.test_calls == [("info", {"selector": "#safe"})]
    assert replay_status == 400
    assert "replayed" in replay["error"]


def test_mutation_requires_valid_core_lease(tmp_path):
    with _running_server(tmp_path) as state:
        no_lease_status, no_lease = _request(
            state,
            "/action",
            method="POST",
            payload=_action_payload(
                state,
                request_id="request-no-lease",
                action="click",
            ),
        )
        payload = _action_payload(
            state,
            request_id="request-with-lease",
            action="click",
            lease=_lease(state),
        )
        ok_status, ok = _request(state, "/action", method="POST", payload=payload)

    assert no_lease_status == 400
    assert "requires a Core lease" in no_lease["error"]
    assert ok_status == 200
    assert ok["ok"] is True


def test_browser_observation_requires_lease_until_provider_proves_read_only(tmp_path):
    with _running_server(tmp_path) as state:
        status, result = _request(
            state,
            "/action",
            method="POST",
            payload=_action_payload(
                state,
                request_id="request-browser-observe",
                action="get_text",
            ),
        )

    assert status == 400
    assert "requires a Core lease" in result["error"]


def test_wrong_instance_identity_is_rejected(tmp_path):
    with _running_server(tmp_path) as state:
        payload = _action_payload(
            state,
            request_id="request-wrong-instance",
            action="get_text",
        )
        payload["identity"] = {**state.identity, "instance_id": "HASHI1"}
        status, result = _request(state, "/action", method="POST", payload=payload)

    assert status == 400
    assert "identity mismatch" in result["error"]


def test_cleanup_endpoint_is_authenticated_and_identity_scoped(tmp_path):
    with _running_server(tmp_path) as state:
        payload = {
            "protocol_version": CAPABILITY_PROTOCOL_VERSION,
            "request_id": "cleanup-1",
            "identity": state.identity,
            "reason": "test-cleanup",
        }
        status, result = _request(
            state,
            "/cleanup",
            method="POST",
            payload=payload,
        )
        payload["identity"] = {**state.identity, "instance_id": "HASHI1"}
        wrong_status, wrong = _request(
            state,
            "/cleanup",
            method="POST",
            payload=payload,
        )

    assert status == 200
    assert result["identity"] == state.identity
    assert wrong_status == 400
    assert "identity mismatch" in wrong["error"]


def test_computer_worker_health_is_truthful_about_platform(tmp_path):
    state = _state(tmp_path)
    del state.health
    state.capability_kind = "computer_control"

    health = state.health()

    assert health["ok"] is (
        os.name == "nt"
        and bool(health["health"]["desktop_state"].get("interactive"))
    )
    assert health["capability_kind"] == "computer_control"


@pytest.mark.asyncio
async def test_computer_worker_forces_persistent_native_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    received = []

    async def fake_execute(action: str, arguments: dict):
        received.append((action, arguments))
        return "ok"

    monkeypatch.setattr(device_worker, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        windows_win32,
        "get_desktop_state",
        lambda: {"available": True, "interactive": True, "locked": False},
    )
    monkeypatch.setitem(
        sys.modules,
        "tools.windows_helper.backends",
        types.SimpleNamespace(execute_action=fake_execute),
    )
    state = DeviceWorkerState(
        bridge_home=tmp_path,
        capability_kind="computer_control",
        instance_id="HASHI3",
        device_id="device-test",
        user_session_id="user-test:interactive",
        worker_token="worker-token-" + "x" * 48,
        bind_host="127.0.0.1",
        advertise_host="127.0.0.1",
        wsl_distro=None,
        logger=logging.getLogger(f"test-computer-worker-{id(tmp_path)}"),
    )

    assert await state.execute("info", {"provider": "auto"}) == "ok"
    assert received == [
        (
            "info",
            {"provider": "usecomputer", "_persistent_worker": True},
        )
    ]

    with pytest.raises(
        device_worker.DeviceWorkerError,
        match="only permits its native provider",
    ):
        await state.execute("info", {"provider": "windows-mcp"})
