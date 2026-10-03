from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import workbench_api as workbench_module
from orchestrator import config_admin as config_admin_module
from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.agent_management import AgentManagementAction
from orchestrator.workbench_api import WorkbenchApiServer


class _Request:
    def __init__(self, *, query=None, match_info=None, payload=None, headers=None):
        self.query = query or {}
        self.match_info = match_info or {}
        self._payload = payload
        self.headers = headers or {}

    async def json(self):
        return self._payload


def _config(agent_active: bool = False) -> dict:
    return {
        "global": {},
        "agents": [
            {
                "name": "lily",
                "display_name": "Lily",
                "emoji": "🌸",
                "workspace_dir": "workspaces/lily",
                "type": "flex",
                "active_backend": "codex-cli",
                "allowed_backends": [{"engine": "codex-cli", "model": "gpt-5.6"}],
                "is_active": agent_active,
            }
        ],
    }


def _server(tmp_path: Path, *, active: bool = False, orchestrator=None) -> WorkbenchApiServer:
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(_config(active), indent=2) + "\n",
        encoding="utf-8-sig",
        newline="\r\n",
    )
    global_config = SimpleNamespace(
        deployment_profile="personal",
        bridge_home=tmp_path,
        workbench_port=18800,
        project_root=tmp_path,
        her_providers={
            "providers": {
                "hashi": {
                    "fast_model": "gpt-5.6-luna",
                    "pro_model": "gpt-5.6-sol",
                    "status": "provisional",
                }
            }
        },
    )
    return WorkbenchApiServer(
        config_path=config_path,
        global_config=global_config,
        orchestrator=orchestrator,
    )


@pytest.mark.asyncio
async def test_reboot_operation_uses_authenticated_owner_and_fixed_agent_scope(tmp_path):
    calls = []

    def operation(operation_id, **scope):
        calls.append((operation_id, scope))
        return {"operation_id": operation_id, "status": "running", "terminal": False, "latest_sequence": 4}

    server = _server(tmp_path, orchestrator=SimpleNamespace(reboot_manager=SimpleNamespace(operation=operation)))
    response = await server.handle_reboot_operation(_Request(
        query={"agent_id": "lily", "after_sequence": "3", "owner_id": "untrusted"},
        match_info={"operation_id": "a" * 32},
    ))
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert calls == [("a" * 32, {"owner_id": "user:0", "agent_id": "lily", "after_sequence": 3})]
    assert json.loads(response.text)["operation"]["latest_sequence"] == 4
    missing = await server.handle_reboot_operation(_Request(match_info={"operation_id": "a" * 32}))
    assert missing.status == 400
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_admin_min_reboot_preserves_trusted_owner_and_operation(tmp_path, monkeypatch):
    calls = []
    operation = {"operation_id": "b" * 32, "mode": "min", "status": "accepted"}

    async def metadata(*args, **kwargs):
        return {"is_generating": False, "queue_depth": 0}

    async def submit(**kwargs):
        calls.append(kwargs)
        return {"accepted": True, "record": {"id": "b" * 32, "status": "accepted"},
                "operation": operation}

    server = _server(tmp_path, orchestrator=SimpleNamespace(
        request_reboot=submit, runtimes=[SimpleNamespace(
            name="lily", is_function_worker_proxy=True, client=SimpleNamespace(call=metadata))]))
    monkeypatch.setattr(server, "_check_admin_auth", lambda request: True)
    monkeypatch.setattr(server, "_v1_owner_id", lambda request: "enterprise:trusted")
    response = await server.handle_admin_reboot_agent(_Request(payload={
        "agent": "lily", "request_key": "config-save", "owner_id": "attacker"}))
    assert response.status == 200
    assert calls[0]["origin"] == {"surface": "workbench", "owner_id": "enterprise:trusted"}
    assert json.loads(response.text)["operation"] == operation


@pytest.mark.asyncio
async def test_reboot_presentation_ack_binds_authenticated_owner(tmp_path, monkeypatch):
    calls = []

    def acknowledge(operation_id, **scope):
        calls.append((operation_id, scope))
        return {"acknowledged": True, "operation": {"operation_id": operation_id}}

    server = _server(tmp_path, orchestrator=SimpleNamespace(
        reboot_manager=SimpleNamespace(acknowledge_start_presentation=acknowledge)))
    monkeypatch.setattr(server, "_v1_owner_id", lambda request: "enterprise:trusted")
    response = await server.handle_reboot_presentation_ack(_Request(
        match_info={"operation_id": "a" * 32},
        payload={"agent_id": "lily", "sequence": 1, "owner_id": "enterprise:attacker"},
    ))
    assert response.status == 200
    assert calls == [("a" * 32, {
        "owner_id": "enterprise:trusted", "agent_id": "lily", "sequence": 1,
        "message_id": None,
    })]
    monkeypatch.setattr(server, "_v1_owner_id", lambda request: None)
    denied = await server.handle_reboot_presentation_ack(_Request(
        match_info={"operation_id": "a" * 32}, payload={"agent_id": "lily", "sequence": 1}))
    assert denied.status == 401
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,status", [
    ("not_found", 404), ("invalid_sequence", 400), ("sequence_mismatch", 409),
    ("not_pending", 409), ("not_required", 409),
])
async def test_reboot_presentation_ack_preserves_owner_rejection(tmp_path, reason, status):
    server = _server(tmp_path, orchestrator=SimpleNamespace(reboot_manager=SimpleNamespace(
        acknowledge_start_presentation=lambda *args, **kwargs: {
            "acknowledged": False, "reason": reason},
    )))
    response = await server.handle_reboot_presentation_ack(_Request(
        match_info={"operation_id": "a" * 32}, payload={"agent_id": "lily", "sequence": 1}))
    assert response.status == status
    assert json.loads(response.text)["error_code"] == reason


@pytest.mark.asyncio
async def test_agents_can_include_inactive_for_authenticated_workbench_gateway(tmp_path):
    server = _server(tmp_path, active=False)

    normal = await server.handle_agents(_Request())
    complete = await server.handle_agents(_Request(query={"include_inactive": "1"}))

    assert json.loads(normal.text)["agents"] == []
    agents = json.loads(complete.text)["agents"]
    assert len(agents) == 1
    assert agents[0]["id"] == "lily"
    assert agents[0]["is_active"] is False
    assert agents[0]["status"] == "inactive"


def test_manual_stop_is_projected_separately_from_unexpected_offline(tmp_path):
    orchestrator = SimpleNamespace(
        agent_lifecycle=SimpleNamespace(manually_stopped_agents={"lily"})
    )
    server = _server(tmp_path, active=True, orchestrator=orchestrator)
    row = _config(True)["agents"][0]

    stopped = server._metadata_for_agent(row, None)
    assert stopped["status"] == "stopped"
    assert stopped["is_active"] is True
    assert stopped["online"] is False

    orchestrator.agent_lifecycle.manually_stopped_agents.clear()
    assert server._metadata_for_agent(row, None)["status"] == "offline"


@pytest.mark.asyncio
async def test_agent_metadata_update_normalizes_config_encoding_and_updates_values(tmp_path):
    server = _server(tmp_path, active=False)

    response = await server.handle_agent_metadata(
        _Request(
            match_info={"name": "lily"},
            payload={"display_name": "Lily Moon", "emoji": "🌙"},
        )
    )

    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["agent"]["display_name"] == "Lily Moon"
    assert payload["agent"]["emoji"] == "🌙"
    raw = server.config_path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    stored = json.loads(raw.decode("utf-8-sig"))
    assert stored["agents"][0]["display_name"] == "Lily Moon"


@pytest.mark.asyncio
async def test_agent_metadata_rejects_intervening_config_publication(tmp_path, monkeypatch):
    server = _server(tmp_path, active=False)
    actual_write = write_config_json

    def interleaved(path, stale):
        winner = read_config_json(path)
        winner["unrelated"] = {"kept": True}
        actual_write(path, winner)
        actual_write(path, stale)

    monkeypatch.setattr(config_admin_module, "write_config_json", interleaved)

    before_agent = read_config_json(server.config_path)["agents"][0]["display_name"]
    response = await server.handle_agent_metadata(
        _Request(
            match_info={"name": "lily"},
            payload={"display_name": "stale value"},
        )
    )

    persisted = read_config_json(server.config_path)
    assert response.status == 409
    assert json.loads(response.text)["error_code"] == "config_conflict"
    assert persisted["unrelated"] == {"kept": True}
    assert persisted["agents"][0]["display_name"] == before_agent


@pytest.mark.asyncio
async def test_agent_activation_persists_before_start_and_can_be_disabled(tmp_path):
    seen_active_values: list[bool] = []
    orchestrator = SimpleNamespace(runtimes=[])
    server = _server(tmp_path, active=False, orchestrator=orchestrator)
    snapshot = read_config_json(server.config_path)
    snapshot["agents"].append(
        {
            "name": "guardian",
            "type": "flex",
            "workspace_dir": "workspaces/guardian",
            "active_backend": "codex-cli",
            "allowed_backends": [{"engine": "codex-cli"}],
            "is_active": True,
        }
    )
    write_config_json(server.config_path, snapshot)

    async def start_agent(name: str):
        raw = json.loads(server.config_path.read_text(encoding="utf-8-sig"))
        seen_active_values.append(raw["agents"][0]["is_active"])
        return True, f"started {name}"

    async def stop_agent(name: str, *, reason: str):
        assert reason == "frontend lifecycle control"
        return True, f"stopped {name}"

    orchestrator.start_agent = start_agent
    orchestrator.stop_agent = stop_agent

    activated = await server.handle_agent_active(
        _Request(match_info={"name": "lily"}, payload={"is_active": True})
    )
    disabled = await server.handle_agent_active(
        _Request(match_info={"name": "lily"}, payload={"is_active": False})
    )

    # A successful start call without an actual Worker is not online evidence.
    assert activated.status == 202
    assert json.loads(activated.text)["lifecycle"]["status"] == "starting"
    assert json.loads(activated.text)["agent"]["is_active"] is True
    assert seen_active_values == [True]
    assert disabled.status == 200
    assert json.loads(disabled.text)["agent"]["is_active"] is False
    stored = json.loads(server.config_path.read_text(encoding="utf-8-sig"))
    assert stored["agents"][0]["is_active"] is False


@pytest.mark.asyncio
async def test_already_starting_is_pending_not_successfully_online(tmp_path):
    async def start_agent(_name):
        return False, "Agent 'lily' is already starting."

    orchestrator = SimpleNamespace(runtimes=[], start_agent=start_agent, _startup_tasks={"lily": object()})
    server = _server(tmp_path, active=True, orchestrator=orchestrator)
    response = await server.handle_agent_active(
        _Request(match_info={"name": "lily"}, payload={"is_active": True})
    )
    assert response.status == 202
    assert json.loads(response.text)["agent"]["online"] is False
    agents = json.loads((await server.handle_agents(_Request())).text)["agents"]
    assert agents[0]["status"] == "starting"


@pytest.mark.asyncio
async def test_agent_lifecycle_control_is_durable_and_replayed_once(tmp_path):
    calls = []

    async def request_start(name, **_kwargs):
        calls.append(name)
        return {"ok": True, "status": "starting", "message": "accepted"}

    orchestrator = SimpleNamespace(
        runtimes=[],
        _startup_tasks={"lily": object()},
        agent_lifecycle=SimpleNamespace(request_start_agent=request_start),
    )
    server = _server(tmp_path, active=False, orchestrator=orchestrator)
    snapshot = read_config_json(server.config_path)
    snapshot["agents"].append(
        {
            "name": "guardian",
            "type": "flex",
            "workspace_dir": "workspaces/guardian",
            "active_backend": "codex-cli",
            "allowed_backends": [{"engine": "codex-cli"}],
            "is_active": True,
        }
    )
    write_config_json(server.config_path, snapshot)
    request = _Request(
        match_info={"name": "lily"},
        payload={"is_active": True},
        headers={"Idempotency-Key": "agent-control-1"},
    )

    first = await server.handle_agent_active(request)
    second = await server.handle_agent_active(request)

    assert first.status == second.status == 202
    assert calls == ["lily"]
    primary = server.session_store.resolve_primary_session(
        owner_id="user:0", agent_id="lily"
    )
    events = server.session_store.events(
        primary["session_id"], owner_id="user:0", limit=20
    )
    results = [event for event in events if event["kind"] == "frontend.command_result"]
    assert len(results) == 1
    assert results[0]["detail"]["result"]["state"] == "starting"


@pytest.mark.asyncio
async def test_agent_lifecycle_control_fences_target_recreation(tmp_path, monkeypatch):
    start_calls = []

    async def request_start(name, **_kwargs):
        start_calls.append(name)
        return {"ok": True, "status": "starting", "message": "accepted"}

    orchestrator = SimpleNamespace(
        runtimes=[],
        _startup_tasks={},
        agent_lifecycle=SimpleNamespace(request_start_agent=request_start),
    )
    server = _server(tmp_path, active=False, orchestrator=orchestrator)
    manager = server._agent_management_manager()
    actual_agent_row = manager._agent_row
    reads = 0

    def recreated_target(name):
        nonlocal reads
        reads += 1
        row = actual_agent_row(name)
        if row is not None and reads > 1:
            row["agent_lifecycle_id"] = "recreated-lifecycle"
        return row

    monkeypatch.setattr(manager, "_agent_row", recreated_target)

    response = await server.handle_agent_active(
        _Request(
            match_info={"name": "lily"},
            payload={"is_active": True},
            headers={"Idempotency-Key": "agent-control-recreated"},
        )
    )

    assert response.status == 409
    assert json.loads(response.text)["error_code"] == "lifecycle_target_changed"
    assert start_calls == []


@pytest.mark.asyncio
async def test_add_agent_api_rejects_raw_config_and_publishes_public_intent(tmp_path):
    server = _server(tmp_path, active=False)

    rejected = await server.handle_admin_add_agent(
        _Request(payload={"name": "unsafe", "backend": "codex-cli", "agent_cfg": {}})
    )
    assert rejected.status == 400
    assert json.loads(rejected.text)["error_code"] == "invalid_request"

    created = await server.handle_admin_add_agent(
        _Request(
            payload={
                "name": "new-agent",
                "display_name": "New Agent",
                "backend": "codex-cli",
                "model": "gpt-5.6-sol",
                "effort": "medium",
                "is_active": False,
            }
        )
    )

    assert created.status == 201
    response = json.loads(created.text)
    assert response["ok"] is True
    assert response["agent"] == {
        "name": "new-agent",
        "display_name": "New Agent",
        "is_active": False,
        "active_backend": "codex-cli",
    }
    stored = read_config_json(server.config_path)
    row = next(item for item in stored["agents"] if item["name"] == "new-agent")
    assert row["is_active"] is False
    assert row["allowed_backends"] == [
        {"engine": "codex-cli", "model": "gpt-5.6-sol", "effort": "medium"}
    ]
    assert (tmp_path / "workspaces" / "new-agent" / "agent.md").is_file()


@pytest.mark.asyncio
async def test_add_agent_api_starts_agent_when_created_active(tmp_path):
    start_calls: list[str] = []
    orchestrator = SimpleNamespace(runtimes=[])

    async def start_agent(name: str):
        start_calls.append(name)
        return True, f"started {name}"

    orchestrator.start_agent = start_agent
    server = _server(tmp_path, active=False, orchestrator=orchestrator)

    created = await server.handle_admin_add_agent(
        _Request(
            payload={
                "name": "ready-agent",
                "display_name": "Ready Agent",
                "backend": "codex-cli",
                "model": "gpt-5.6-sol",
                "is_active": True,
            }
        )
    )

    assert created.status == 201
    response = json.loads(created.text)
    assert response["ok"] is True
    assert response["agent"]["is_active"] is True
    assert response["lifecycle"] == {
        "ok": True,
        "message": "started ready-agent",
    }
    assert start_calls == ["ready-agent"]


@pytest.mark.asyncio
async def test_add_active_agent_is_accepted_once_by_shared_lifecycle_owner(tmp_path):
    calls = []

    async def request_start(name, **kwargs):
        calls.append((name, kwargs))
        return {"ok": True, "status": "starting", "message": "accepted"}

    orchestrator = SimpleNamespace(
        runtimes=[],
        _startup_tasks={},
        agent_lifecycle=SimpleNamespace(request_start_agent=request_start),
    )
    server = _server(tmp_path, active=False, orchestrator=orchestrator)

    created = await server.handle_admin_add_agent(
        _Request(
            payload={
                "name": "pending-agent",
                "backend": "codex-cli",
                "model": "gpt-5.6-sol",
                "is_active": True,
            }
        )
    )

    assert created.status == 202
    body = json.loads(created.text)
    assert body["ok"] is True
    assert body["lifecycle"]["status"] == "starting"
    assert len(calls) == 1
    assert calls[0][0] == "pending-agent"
    assert len(calls[0][1]["deactivate_on_failure_revision"]) == 64


@pytest.mark.asyncio
async def test_add_active_agent_immediate_admission_rejection_is_inactive(tmp_path):
    async def request_start(_name, **_kwargs):
        return {
            "ok": False,
            "status": "unavailable",
            "message": "Shared Functions are draining.",
        }

    server = _server(
        tmp_path,
        active=False,
        orchestrator=SimpleNamespace(
            runtimes=[],
            _startup_tasks={},
            agent_lifecycle=SimpleNamespace(request_start_agent=request_start),
        ),
    )

    created = await server.handle_admin_add_agent(
        _Request(
            payload={
                "name": "rejected-agent",
                "backend": "codex-cli",
                "model": "gpt-5.6-sol",
                "is_active": True,
            }
        )
    )

    assert created.status == 503
    body = json.loads(created.text)
    assert body["ok"] is False
    assert body["agent"]["is_active"] is False
    stored = read_config_json(server.config_path)
    row = next(item for item in stored["agents"] if item["name"] == "rejected-agent")
    assert row["is_active"] is False


@pytest.mark.asyncio
async def test_add_agent_api_start_failure_leaves_created_agent_inactive(tmp_path):
    orchestrator = SimpleNamespace(runtimes=[])

    async def start_agent(name: str):
        return False, f"worker failed for {name}"

    orchestrator.start_agent = start_agent
    server = _server(tmp_path, active=False, orchestrator=orchestrator)

    created = await server.handle_admin_add_agent(
        _Request(
            payload={
                "name": "offline-agent",
                "display_name": "Offline Agent",
                "backend": "codex-cli",
                "model": "gpt-5.6-sol",
                "is_active": True,
            }
        )
    )

    assert created.status == 503
    response = json.loads(created.text)
    assert response["ok"] is False
    assert response["error_code"] == "agent_start_failed"
    assert response["created"] == {"workspace": True, "config": True}
    assert response["agent"]["is_active"] is False
    assert response["lifecycle"] == {
        "ok": False,
        "message": "worker failed for offline-agent",
    }
    stored = read_config_json(server.config_path)
    row = next(item for item in stored["agents"] if item["name"] == "offline-agent")
    assert row["is_active"] is False


@pytest.mark.asyncio
async def test_add_agent_api_does_not_overwrite_newer_config_after_start_failure(
    tmp_path, monkeypatch
):
    orchestrator = SimpleNamespace(runtimes=[])

    async def start_agent(name: str):
        return False, f"worker failed for {name}"

    orchestrator.start_agent = start_agent
    server = _server(tmp_path, active=False, orchestrator=orchestrator)
    manager = server._agent_management_manager()
    actual_set_active = manager._admin.set_agent_active

    def interleaved(name, active, **kwargs):
        winner = read_config_json(server.config_path)
        winner["newer_configuration"] = {"kept": True}
        created_row = next(
            item for item in winner["agents"] if item["name"] == "racing-agent"
        )
        created_row["is_active"] = False
        write_config_json(server.config_path, winner)
        return actual_set_active(name, active, **kwargs)

    monkeypatch.setattr(manager._admin, "set_agent_active", interleaved)

    created = await server.handle_admin_add_agent(
        _Request(
            payload={
                "name": "racing-agent",
                "display_name": "Racing Agent",
                "backend": "codex-cli",
                "model": "gpt-5.6-sol",
                "is_active": True,
            }
        )
    )

    assert created.status == 503
    response = json.loads(created.text)
    assert response["ok"] is False
    assert response["error_code"] == "agent_start_failed"
    assert response["agent"]["is_active"] is False
    assert "configuration changed" in response["configuration_warning"].lower()
    stored = read_config_json(server.config_path)
    assert stored["newer_configuration"] == {"kept": True}
    row = next(item for item in stored["agents"] if item["name"] == "racing-agent")
    assert row["is_active"] is False


@pytest.mark.asyncio
async def test_add_agent_api_persists_her_orchestration_effort(tmp_path):
    server = _server(tmp_path, active=False)

    retired = await server.handle_admin_add_agent(
        _Request(
            payload={
                "name": "old-preset",
                "backend": "her-v2",
                "preset": "balanced",
            }
        )
    )
    assert retired.status == 400
    assert json.loads(retired.text)["error_code"] == "invalid_request"

    created = await server.handle_admin_add_agent(
        _Request(
            payload={
                "name": "strategist",
                "display_name": "Strategist",
                "backend": "her-v2",
                "effort": "low",
                "is_active": False,
            }
        )
    )

    assert created.status == 201
    stored = read_config_json(server.config_path)
    row = next(item for item in stored["agents"] if item["name"] == "strategist")
    assert row["allowed_backends"][0]["engine"] == "her-v2"
    assert row["allowed_backends"][0]["effort"] == "low"
    assert row["allowed_backends"][0]["her_v2"]["main"] == {
        "provider": "hashi-api",
        "model": "gpt-5.6-sol",
    }


@pytest.mark.asyncio
async def test_herv3_creation_uses_explicit_provider_and_rejects_ambiguous_model(tmp_path):
    server = _server(tmp_path)
    server.global_config.her_providers["providers"]["openrouter"] = {
        "engine": "openrouter-api", "models": ["gpt-5.6-sol"],
        "default_model": "gpt-5.6-sol", "status": "stable",
    }
    spec = {"name": "explicit-provider", "backend": "her-v3",
            "provider": "openrouter-api", "model": "gpt-5.6-sol", "effort": "high"}
    created = await server.handle_admin_add_agent(_Request(payload=spec))
    assert created.status == 201, created.text
    stored = read_config_json(server.config_path)
    row = next(item for item in stored["agents"] if item["name"] == spec["name"])
    assert row["allowed_backends"][0]["her_v2"]["main"] == {
        "provider": "openrouter-api", "model": "gpt-5.6-sol"}
    ambiguous = await server.handle_admin_add_agent(_Request(payload={
        **spec, "name": "ambiguous-provider", "provider": None}))
    assert ambiguous.status == 400
    assert json.loads(ambiguous.text)["error_code"] == "invalid_provider"
    unavailable = await server.handle_admin_add_agent(_Request(payload={
        **spec, "name": "wrong-provider", "provider": "missing-api"}))
    assert unavailable.status == 400
    assert json.loads(unavailable.text)["error_code"] == "invalid_provider"


def test_typed_agent_create_rejects_non_string_optional_fields():
    base = {
        "kind": "action",
        "operation": "agent.create",
        "owner_id": "user:7",
        "connector_id": "backend_api",
        "agent_id": "typed-provider",
    }
    for field in ("display_name", "model", "provider", "effort"):
        with pytest.raises(
            ValueError,
            match=rf"Agent creation {field} must be a string or null",
        ):
            AgentManagementAction(
                **base,
                payload={"backend": "her-v3", "is_active": False, field: 7},
            )

    action = AgentManagementAction(
        **base,
        payload={
            "backend": "her-v3",
            "provider": "openrouter-api",
            "model": None,
            "effort": "high",
            "is_active": False,
        },
    )
    assert action.payload["provider"] == "openrouter-api"


@pytest.mark.asyncio
async def test_add_agent_api_maps_duplicate_to_conflict(tmp_path):
    server = _server(tmp_path, active=False)
    request = _Request(payload={"name": "duplicate", "backend": "codex-cli"})

    first = await server.handle_admin_add_agent(request)
    second = await server.handle_admin_add_agent(request)

    assert first.status == 201
    assert second.status == 409
    assert json.loads(second.text)["error_code"] == "agent_exists"


@pytest.mark.asyncio
async def test_agent_management_routes_mutations_to_typed_pao_owner(tmp_path):
    actions = []

    class _AgentManagement:
        async def dispatch(self, action, *, session_store=None):
            actions.append(action)
            if action.operation == "metadata.update":
                return {
                    "agent_row": {
                        **_config(False)["agents"][0],
                        "display_name": "Lily Moon",
                        "emoji": "🌙",
                    }
                }
            if action.operation == "lifecycle.set_active":
                return {
                    "agent_row": {**_config(True)["agents"][0]},
                    "lifecycle": {
                        "ok": True,
                        "status": "starting",
                        "message": "accepted",
                    },
                    "status": 202,
                }
            raise AssertionError(action.operation)

    orchestrator = SimpleNamespace(
        runtimes=[],
        agent_management=_AgentManagement(),
        _startup_tasks={"lily": object()},
    )
    server = _server(tmp_path, active=False, orchestrator=orchestrator)

    metadata = await server.handle_agent_metadata(
        _Request(
            match_info={"name": "lily"},
            payload={"display_name": "Lily Moon", "emoji": "🌙"},
        )
    )
    active = await server.handle_agent_active(
        _Request(match_info={"name": "lily"}, payload={"is_active": True})
    )

    assert metadata.status == 200
    assert active.status == 202
    assert [action.operation for action in actions] == [
        "metadata.update",
        "lifecycle.set_active",
    ]
    assert all(action.kind in {"action", "control"} for action in actions)
    assert all(action.connector_id == "backend_api" for action in actions)
    assert all(action.owner_id == "user:0" for action in actions)


@pytest.mark.asyncio
async def test_agent_command_binds_trusted_owner_not_payload_owner(tmp_path, monkeypatch):
    captured = []

    async def execute(_runtime, _command, **kwargs):
        captured.append(kwargs["session_metadata"])
        return {"ok": True}

    monkeypatch.setattr(workbench_module, "execute_local_command", execute)
    runtime = SimpleNamespace(name="lily")
    server = _server(
        tmp_path,
        active=True,
        orchestrator=SimpleNamespace(runtimes=[runtime]),
    )
    monkeypatch.setattr(
        server, "_v1_owner_id", lambda _request: "enterprise:trusted-user"
    )

    response = await server.handle_agent_command(
        _Request(
            match_info={"name": "lily"},
            payload={"command": "/reboot min", "owner_id": "enterprise:attacker"},
        )
    )

    assert response.status == 200
    assert captured == [
        {
            "connector_id": "backend_api",
            "fc_compatibility_adapter_id": "backend_api.agent_command",
            "_hashi_owner_id": "enterprise:trusted-user",
        }
    ]


@pytest.mark.asyncio
async def test_agent_deletion_routes_preview_commit_and_status_to_pao_owner(tmp_path):
    actions = []

    class _AgentManagement:
        async def dispatch(self, action, *, session_store=None):
            actions.append(action)
            if action.operation == "deletion.preview":
                return {
                    "preview": {
                        "ok": True,
                        "agent_id": "lily",
                        "preview_token": "preview-1",
                    },
                    "status": 200,
                }
            if action.operation == "deletion.commit":
                return {
                    "result": {
                        "ok": True,
                        "operation_id": "delete-1",
                        "status": "succeeded",
                    },
                    "status": 200,
                }
            return {
                "receipt": {"operation_id": "delete-1", "status": "succeeded"},
                "status": 200,
            }

    server = _server(
        tmp_path,
        orchestrator=SimpleNamespace(
            runtimes=[], agent_management=_AgentManagement()
        ),
    )

    preview = await server.handle_admin_agent_deletion_preview(
        _Request(match_info={"agent_id": "lily"})
    )
    commit = await server.handle_admin_agent_deletion(
        _Request(
            match_info={"agent_id": "lily"},
            payload={
                "preview_token": "preview-1",
                "confirmed_agent_id": "lily",
            },
        )
    )
    status = await server.handle_admin_agent_deletion_status(
        _Request(match_info={"operation_id": "delete-1"})
    )

    assert [preview.status, commit.status, status.status] == [200, 200, 200]
    assert [action.operation for action in actions] == [
        "deletion.preview",
        "deletion.commit",
        "deletion.status",
    ]
    assert all(action.owner_id == "user:0" for action in actions)
