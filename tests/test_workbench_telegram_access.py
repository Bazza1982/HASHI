from __future__ import annotations

import json

import pytest

from orchestrator.config_json import read_config_json
from tests.test_workbench_telegram_credential import ADMIN_TOKEN, _Request, _server


@pytest.mark.asyncio
async def test_access_api_is_admin_only_and_disabled_until_runtime_is_ready(tmp_path):
    server = _server(tmp_path, verifier=None)
    unauthenticated = await server.handle_admin_agent_telegram_access(_Request())
    assert unauthenticated.status == 403
    authenticated = _Request(headers={"X-Workbench-Token": ADMIN_TOKEN})
    response = await server.handle_admin_agent_telegram_access(authenticated)
    assert response.status == 501
    assert json.loads(response.text)["error_code"] == "telegram_access_unavailable"
    before = (tmp_path / "agents.json").read_bytes()
    attempted = await server.handle_admin_agent_telegram_access_change(
        _Request(headers={"X-Workbench-Token": ADMIN_TOKEN}, payload={
            "expected_revision": "0" * 64, "scope": "global", "action": "add", "user_id": "200",
        })
    )
    assert attempted.status == 501
    assert (tmp_path / "agents.json").read_bytes() == before


@pytest.mark.asyncio
async def test_access_gate_requires_all_active_workers_and_no_legacy_grants(tmp_path):
    from types import SimpleNamespace
    from orchestrator.config_json import write_config_json

    server = _server(tmp_path, verifier=None, runtime_active=True, shared=True)
    headers = {"X-Workbench-Token": ADMIN_TOKEN}
    def worker(name, protocol=1, phase="ACTIVE", accepting=True):
        return SimpleNamespace(
            name=name, is_function_worker_proxy=True,
            get_runtime_metadata=lambda: {
                "telegram_scoped_access_protocol": protocol,
                "worker_phase": phase, "worker_accepting": accepting,
            },
        )
    server.orchestrator.runtimes = [worker("lily")]
    assert (await server.handle_admin_agent_telegram_access(_Request(headers=headers))).status == 501
    server.orchestrator.runtimes.append(worker("momo", protocol=0))
    assert (await server.handle_admin_agent_telegram_access(_Request(headers=headers))).status == 501

    server.orchestrator.runtimes[1] = worker("momo")
    response = await server.handle_admin_agent_telegram_access(_Request(headers=headers))
    assert response.status == 200
    state = json.loads(response.text)
    changed = await server.handle_admin_agent_telegram_access_change(_Request(
        headers=headers, payload={"expected_revision": state["revision"],
                                  "scope": "global", "action": "add", "user_id": "100"},
    ))
    assert changed.status == 200
    assert json.loads(changed.text)["global_user_ids"] == ["42", "100"]
    config_path = tmp_path / "agents.json"
    assert read_config_json(config_path)["global"]["telegram_scoped_access_version"] == 1

    server.orchestrator.runtimes[1] = worker("momo", protocol=0)
    before = config_path.read_bytes()
    state = json.loads(changed.text)
    blocked = await server.handle_admin_agent_telegram_access_change(_Request(
        headers=headers, payload={"expected_revision": state["revision"],
                                  "scope": "global", "action": "remove", "user_id": "100"},
    ))
    assert blocked.status == 501
    assert config_path.read_bytes() == before

    server.orchestrator.runtimes[1] = worker("momo")
    config = read_config_json(config_path)
    config["agents"][0]["extra"] = {"authorized_telegram_ids": [75]}
    write_config_json(config_path, config)
    assert (await server.handle_admin_agent_telegram_access(_Request(headers=headers))).status == 501


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_protocol,phase,accepting", [
    (True, "ACTIVE", True), (1, "DRAINING", True), (1, "ACTIVE", False)
])
async def test_access_gate_rejects_ambiguous_or_unavailable_workers(
    tmp_path, bad_protocol, phase, accepting
):
    from types import SimpleNamespace

    server = _server(tmp_path, verifier=None, runtime_active=True)
    server.orchestrator.runtimes = [SimpleNamespace(
        name="lily", is_function_worker_proxy=True,
        get_runtime_metadata=lambda: {
            "telegram_scoped_access_protocol": bad_protocol,
            "worker_phase": phase, "worker_accepting": accepting,
        },
    )]
    response = await server.handle_admin_agent_telegram_access(
        _Request(headers={"X-Workbench-Token": ADMIN_TOKEN})
    )
    assert response.status == 501


@pytest.mark.asyncio
async def test_workbench_grants_and_revocations_reach_the_selected_worker(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from telegram import Update
    from orchestrator.function_worker_host import FunctionWorkerHost
    from orchestrator import runtime_session
    from tests.test_telegram_actor_scope import instance, update

    server = _server(tmp_path, verifier=None, runtime_active=True, shared=True)
    metadata = {"telegram_scoped_access_protocol": 1,
                "worker_phase": "ACTIVE", "worker_accepting": True}
    server.orchestrator.runtimes = [
        SimpleNamespace(name=name, is_function_worker_proxy=True,
                        get_runtime_metadata=lambda: metadata)
        for name in ("lily", "momo")
    ]
    headers = {"X-Workbench-Token": ADMIN_TOKEN}
    response = await server.handle_admin_agent_telegram_access(_Request(headers=headers))
    state = json.loads(response.text)
    response = await server.handle_admin_agent_telegram_access_change(_Request(
        headers=headers, payload={"expected_revision": state["revision"], "scope": "global",
                                  "action": "add", "user_id": "100"},
    ))
    state = json.loads(response.text)
    response = await server.handle_admin_agent_telegram_access_change(_Request(
        headers=headers, payload={"expected_revision": state["revision"], "scope": "agent",
                                  "action": "add", "user_id": "200"},
    ))
    state = json.loads(response.text)
    assert state["global_user_ids"] == ["42", "100"]
    assert state["agent_user_ids"] == ["200"]

    selected = [update(100)]
    monkeypatch.setattr(Update, "de_json", lambda payload, bot: selected[0])
    observations = []
    hosts = {}
    for name in ("lily", "momo"):
        fixture_dir = tmp_path / ("fixture-" + name)
        fixture_dir.mkdir()
        runtime, _seen, _config_path = instance(fixture_dir)
        runtime.name = name
        runtime.global_config.config_path = tmp_path / "agents.json"
        runtime.global_config.secrets_path = tmp_path / "secrets.json"
        runtime.app.bot = object()
        async def process(telegram_update, *, worker_runtime=runtime):
            observations.append((worker_runtime.name, telegram_update.effective_user.id,
                                 runtime_session.owner_id(worker_runtime)))
        runtime.app.process_update = process
        host = FunctionWorkerHost.__new__(FunctionWorkerHost)
        host.runtime = runtime
        host.phase = "ACTIVE"
        host.accepting = True
        host.emit_metadata = AsyncMock()
        hosts[name] = host

    for name, actor in (("lily", 100), ("momo", 100), ("lily", 200), ("momo", 200)):
        selected[0] = update(actor)
        await hosts[name].handle_request("runtime.telegram_update", {"update": {}})
    assert [(name, actor) for name, actor, _owner in observations] == [
        ("lily", 100), ("momo", 100), ("lily", 200)
    ]
    assert all(owner.startswith("telegram-user:") for _, _, owner in observations)

    response = await server.handle_admin_agent_telegram_access_change(_Request(
        headers=headers, payload={"expected_revision": state["revision"], "scope": "global",
                                  "action": "remove", "user_id": "100"},
    ))
    state = json.loads(response.text)
    response = await server.handle_admin_agent_telegram_access_change(_Request(
        headers=headers, payload={"expected_revision": state["revision"], "scope": "agent",
                                  "action": "remove", "user_id": "200"},
    ))
    assert response.status == 200
    for name, actor in (("lily", 100), ("momo", 100), ("lily", 200)):
        selected[0] = update(actor)
        await hosts[name].handle_request("runtime.telegram_update", {"update": {}})
    assert len(observations) == 3


@pytest.mark.asyncio
async def test_access_api_uses_scoped_revision_and_rejects_stale_or_invalid_writes(tmp_path, monkeypatch):
    server = _server(tmp_path, verifier=None)
    monkeypatch.setattr(server, "_telegram_access_management_supported", lambda: True)
    headers = {"X-Workbench-Token": ADMIN_TOKEN}
    status = await server.handle_admin_agent_telegram_access(_Request(headers=headers))
    data = json.loads(status.text)
    assert status.status == 200
    assert status.headers["Cache-Control"] == "no-store"
    assert data["management_supported"] is True
    assert data["agent_id"] == "lily"
    assert data["global_user_ids"] == ["42"]
    assert "provider_api_key" not in status.text

    payload = {"expected_revision": data["revision"], "scope": "agent",
               "action": "add", "user_id": "200"}
    changed = await server.handle_admin_agent_telegram_access_change(_Request(headers=headers, payload=payload))
    assert changed.status == 200
    assert json.loads(changed.text)["agent_user_ids"] == ["200"]

    before = (tmp_path / "agents.json").read_bytes()
    stale = await server.handle_admin_agent_telegram_access_change(_Request(headers=headers, payload=payload))
    assert stale.status == 409
    assert json.loads(stale.text)["error_code"] == "telegram_access_changed"
    invalid = await server.handle_admin_agent_telegram_access_change(
        _Request(headers=headers, payload={**payload, "extra": "reject"})
    )
    assert invalid.status == 400
    assert (tmp_path / "agents.json").read_bytes() == before


@pytest.mark.asyncio
async def test_missing_admin_token_does_not_grant_access_management(tmp_path, monkeypatch):
    server = _server(tmp_path, verifier=None)
    server.admin_token = ""
    monkeypatch.setattr(server, "_telegram_access_management_supported", lambda: True)
    response = await server.handle_admin_agent_telegram_access(_Request())
    assert response.status == 403

@pytest.mark.asyncio
async def test_api_can_configure_first_and_additional_users_without_overwriting_secrets(tmp_path, monkeypatch):
    server = _server(tmp_path, verifier=None, authorized_id=0, shared=True)
    monkeypatch.setattr(server, "_telegram_access_management_supported", lambda: True)
    headers = {"X-Workbench-Token": ADMIN_TOKEN}
    before_secrets = (tmp_path / "secrets.json").read_bytes()

    status = json.loads((await server.handle_admin_agent_telegram_access(_Request(headers=headers))).text)
    assert status["primary_user_id"] is None
    first = json.loads((await server.handle_admin_agent_telegram_access_change(
        _Request(headers=headers, payload={"expected_revision": status["revision"], "scope": "global",
                                           "action": "add", "user_id": "100"})
    )).text)
    assert first["primary_user_id"] == "100"
    second = json.loads((await server.handle_admin_agent_telegram_access_change(
        _Request(headers=headers, payload={"expected_revision": first["revision"], "scope": "global",
                                           "action": "add", "user_id": "200"})
    )).text)
    assert second["global_user_ids"] == ["100", "200"]

    momo = json.loads((await server.handle_admin_agent_telegram_access(_Request(agent_id="momo", headers=headers))).text)
    assert momo["global_user_ids"] == ["100", "200"]
    assert momo["agent_user_ids"] == []
    assert read_config_json(tmp_path / "agents.json")["global"]["owner_scope_id"] == "user:0"
    assert (tmp_path / "secrets.json").read_bytes() == before_secrets
