"""Exercise credential save -> idle adoption through the public API owner."""
from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio

import orchestrator.workbench_api as api
from tests.test_workbench_telegram_credential import (
    ADMIN_TOKEN, NEW_TOKEN, _Request, _server,
)
from orchestrator.config_json import read_config_json, write_config_json


@pytest_asyncio.fixture
async def activation(tmp_path, monkeypatch):
    async def verify(_token):
        return {"id": 123, "username": "safe_bot"}
    server = _server(tmp_path, verifier=verify, runtime_active=True)
    runtime = server.orchestrator.runtimes[0]
    runtime.is_function_worker_proxy = True
    state = {"agent_lifecycle_id": "creation-lily-1", "agent_stop_epoch": 0,
             "online": True, "worker_phase": "ACTIVE", "worker_accepting": True,
             "is_generating": True, "queue_depth": 0, "detached_request_count": 0}
    runtime.get_runtime_metadata = lambda: dict(state)
    calls, responses = [], []
    accepted = asyncio.Event()

    async def execute(target, command, **kwargs):
        calls.append((target, command, kwargs))
        response = responses.pop(0) if responses else {
            "ok": True, "result": {"accepted": True, "record": {
                "id": "operation-lily-1", "status": "accepted", "targets": ["lily"],
            }},
        }
        if isinstance(response, Exception):
            raise response
        if isinstance(response, dict) and response.get("result", {}).get("accepted"):
            accepted.set()
        return response

    monkeypatch.setattr(api, "try_execute_slash_command_text", execute)
    coordinator = getattr(server, "telegram_activation", None)
    if coordinator is not None:
        coordinator.poll_seconds = 0.01
    yield server, runtime, state, calls, responses, accepted
    if coordinator is not None:
        await coordinator.close()


async def save(server):
    response = await server.handle_admin_agent_telegram_credential_replace(_Request(
        headers={"X-Workbench-Token": ADMIN_TOKEN},
        payload={"token": NEW_TOKEN, "replace_existing": True,
                 "expected_revision": server.telegram_credential_service.status("lily")["credential_revision"]},
    ))
    assert response.status == 200
    return json.loads(response.text)


async def status(server):
    response = await server.handle_admin_agent_telegram_credential(_Request(
        headers={"X-Workbench-Token": ADMIN_TOKEN},
    ))
    assert response.status == 200
    return json.loads(response.text)


@pytest.mark.asyncio
async def test_busy_save_finishes_when_idle_without_resaving_or_another_agent(activation, tmp_path):
    server, runtime, state, calls, responses, accepted = activation
    saved = await save(server)
    assert saved["activation_waiting"] is True
    assert saved["activation_required"] == "agent_idle"
    assert calls == []  # A busy Worker is never disrupted to install a token.
    saved_bytes = (tmp_path / "secrets.json").read_bytes()
    assert (await status(server))["activation_waiting"] is True
    state["is_generating"] = False
    await asyncio.wait_for(accepted.wait(), 1)
    assert len(calls) == 1 and calls[0][0] is runtime
    assert calls[0][1] == "/reboot min"
    adopted = await status(server)
    assert adopted["activation_status"] == "activating"
    assert adopted["activation_operation_id"] == "operation-lily-1"
    assert adopted["bidirectional_ready"] is False
    for _ in range(3):
        await status(server)
    assert len(calls) == 1
    assert (tmp_path / "secrets.json").read_bytes() == saved_bytes
    assert NEW_TOKEN not in repr(calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["credential", "creation", "stop", "stopped"])
async def test_wait_cannot_activate_a_replaced_binding(activation, tmp_path, changed):
    server, runtime, state, calls, responses, accepted = activation
    assert (await save(server))["activation_waiting"] is True
    if changed == "credential":
        document = read_config_json(tmp_path / "secrets.json")
        document["lily.telegram"] = "987654:abcdefghijklmnopqrstuvwxyz_ABCDEFGHI"
        write_config_json(tmp_path / "secrets.json", document)
    elif changed == "creation":
        state["agent_lifecycle_id"] = "new-creation-with-same-name"
    elif changed == "stop":
        state["agent_stop_epoch"] = 1
    else:
        server.orchestrator.runtimes.clear()
    state["is_generating"] = False
    await asyncio.sleep(0.08)
    assert calls == []
    assert (await status(server)).get("activation_waiting", False) is False


@pytest.mark.asyncio
async def test_admission_busy_is_retried_but_an_accepted_receipt_is_never_replayed(activation):
    server, runtime, state, calls, responses, accepted = activation
    state["is_generating"] = False
    responses.append({"ok": False, "error": "AGENT_BUSY"})
    saved = await save(server)
    assert saved["activation_waiting"] is True
    await asyncio.wait_for(accepted.wait(), 1)
    assert len(calls) == 2
    assert calls[0][2]["session_metadata"]["frontend_invocation_id"] == calls[1][2]["session_metadata"]["frontend_invocation_id"]
    await asyncio.sleep(0.05)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_durable_target_busy_receipt_is_not_silently_resubmitted(activation):
    server, runtime, state, calls, responses, accepted = activation
    state["is_generating"] = False
    responses.append({"ok": True, "result": {"accepted": True, "record": {
        "id": "durable-busy", "status": "failed", "reason": "target_busy", "targets": ["lily"],
    }}})
    saved = await save(server)
    assert saved["activation_status"] == "pending"
    assert saved["activation_operation_id"] == "durable-busy"
    await asyncio.sleep(0.05)
    assert len(calls) == 1
    assert (await status(server)).get("activation_waiting", False) is False


@pytest.mark.asyncio
async def test_wait_expires_with_explicit_retry_and_shutdown_cancels_wait(activation):
    server, runtime, state, calls, responses, accepted = activation
    server.telegram_activation.wait_seconds = 0.04
    assert (await save(server))["activation_waiting"] is True
    # Observe the durable expiry within a bounded scheduler window.
    async with asyncio.timeout(2):
        while True:
            expired = await status(server)
            if expired.get("activation_error_code") == "telegram_activation_wait_expired":
                break
            await asyncio.sleep(0.005)
    assert expired["activation_error_code"] == "telegram_activation_wait_expired"
    assert expired["activation_retry_allowed"] is True
    assert calls == []
    response = await server.handle_admin_agent_telegram_activation(_Request(
        headers={"X-Workbench-Token": ADMIN_TOKEN},
        payload={"expected_revision": expired["credential_revision"],
                 "expected_lifecycle_id": state["agent_lifecycle_id"]},
    ))
    assert response.status == 200
    assert json.loads(response.text)["activation_waiting"] is True
    await server.telegram_activation.close()
    state["is_generating"] = False
    await asyncio.sleep(0.04)
    assert calls == []


@pytest.mark.asyncio
async def test_explicit_activation_is_authenticated_revision_bound_and_token_free(activation):
    server, runtime, state, calls, responses, accepted = activation
    current = await status(server)
    base = {"expected_revision": current["credential_revision"],
            "expected_lifecycle_id": state["agent_lifecycle_id"]}
    for headers, body, expected in [
        ({}, base, 403),
        ({"X-Workbench-Token": ADMIN_TOKEN}, {**base, "token": NEW_TOKEN}, 400),
        ({"X-Workbench-Token": ADMIN_TOKEN}, {**base, "expected_revision": "0" * 64}, 409),
        ({"X-Workbench-Token": ADMIN_TOKEN}, {**base, "expected_lifecycle_id": "old-creation"}, 409),
    ]:
        response = await server.handle_admin_agent_telegram_activation(_Request(headers=headers, payload=body))
        assert response.status == expected
    assert calls == []
    state["is_generating"] = False
    response = await server.handle_admin_agent_telegram_activation(_Request(
        headers={"X-Workbench-Token": ADMIN_TOKEN}, payload=base,
    ))
    assert response.status == 200
    assert json.loads(response.text)["activation_operation_id"] == "operation-lily-1"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_http_activation_route_keeps_auth_scope_and_deduplicates_accepted_intent(activation):
    from aiohttp.test_utils import TestClient, TestServer
    server, runtime, state, calls, responses, accepted = activation
    current = await status(server)
    body = {"expected_revision": current["credential_revision"],
            "expected_lifecycle_id": state["agent_lifecycle_id"]}
    async with TestClient(TestServer(server.app)) as client:
        denied = await client.post("/api/admin/agents/lily/telegram-activation", json=body)
        assert denied.status == 403
        headers = {"X-Workbench-Token": ADMIN_TOKEN}
        queued = await client.post("/api/admin/agents/lily/telegram-activation",
                                   headers=headers, json=body)
        assert queued.status == 200
        assert (await queued.json())["activation_waiting"] is True
        state["is_generating"] = False
        await asyncio.wait_for(accepted.wait(), 1)
        repeated = await client.post("/api/admin/agents/lily/telegram-activation",
                                     headers=headers, json=body)
        assert (await repeated.json())["activation_operation_id"] == "operation-lily-1"
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_concurrent_explicit_activation_submits_only_one_owner_operation(activation, monkeypatch):
    server, runtime, state, calls, responses, accepted = activation
    state["is_generating"] = False
    started, release = asyncio.Event(), asyncio.Event()

    async def execute(target, command, **kwargs):
        calls.append((target, command, kwargs))
        started.set()
        await release.wait()
        return {"ok": True, "result": {"accepted": True, "record": {
            "id": "one-operation", "status": "accepted", "targets": ["lily"]}}}

    monkeypatch.setattr(api, "try_execute_slash_command_text", execute)
    current = await status(server)
    body = {"expected_revision": current["credential_revision"],
            "expected_lifecycle_id": state["agent_lifecycle_id"]}
    def request():
        return _Request(headers={"X-Workbench-Token": ADMIN_TOKEN}, payload=body)
    first = asyncio.create_task(server.handle_admin_agent_telegram_activation(request()))
    await asyncio.wait_for(started.wait(), 1)
    try:
        second = await server.handle_admin_agent_telegram_activation(request())
        assert second.status == 200
        assert len(calls) == 1
    finally:
        release.set()
        assert (await first).status == 200
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, {}, {"ok": True}, {
    "ok": True, "result": {"accepted": False, "reason": "ack_unknown"},
}])
async def test_malformed_acknowledgements_never_enable_replay(activation, response):
    server, runtime, state, calls, responses, accepted = activation
    state["is_generating"] = False
    responses.append(response)
    saved = await save(server)
    assert saved["activation_retry_allowed"] is False
    assert saved["activation_status"] == "pending"
    await asyncio.sleep(0.05)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unknown_activity_does_not_interrupt_a_worker(activation):
    server, runtime, state, calls, responses, accepted = activation
    state.pop("queue_depth")
    saved = await save(server)
    assert saved["activation_status"] == "pending"
    assert saved["activation_waiting"] is False
    assert calls == []


@pytest.mark.asyncio
async def test_unknown_response_and_status_reads_never_replay_an_activation(activation):
    server, runtime, state, calls, responses, accepted = activation
    assert (await status(server))["activation_status"] == "pending"
    assert calls == []
    state["is_generating"] = False
    responses.append(RuntimeError(NEW_TOKEN))
    saved = await save(server)
    assert saved["activation_status"] == "pending"
    assert saved["activation_retry_allowed"] is False
    for _ in range(3):
        assert (await status(server))["activation_retry_allowed"] is False
    await asyncio.sleep(0.05)
    assert len(calls) == 1
    assert NEW_TOKEN not in repr(saved)
# HASHI-Fingerprint: hfp_796e570be426497cb631bb1c98bd1689
