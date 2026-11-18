from __future__ import annotations

import json

import pytest

from orchestrator.config_json import read_config_json
from tests.test_workbench_telegram_credential import ADMIN_TOKEN, _Request, _server


@pytest.mark.asyncio
async def test_access_api_can_save_offline_configuration_and_reports_pending_adoption(tmp_path):
    server = _server(tmp_path, verifier=None)
    unauthenticated = await server.handle_admin_agent_telegram_access(_Request())
    assert unauthenticated.status == 403
    authenticated = _Request(headers={"X-Workbench-Token": ADMIN_TOKEN})
    response = await server.handle_admin_agent_telegram_access(authenticated)
    assert response.status == 200
    snapshot = json.loads(response.text)
    assert snapshot["management_supported"] is True
    assert snapshot["global_management_supported"] is True
    assert snapshot["policy_adoption"]["agent"]["state"] == "offline"
    assert snapshot["primary_user_id"] == "42"
    attempted = await server.handle_admin_agent_telegram_access_change(
        _Request(headers={"X-Workbench-Token": ADMIN_TOKEN}, payload={
            "expected_revision": snapshot["revision"], "scope": "global", "action": "add", "user_id": "200",
        })
    )
    assert attempted.status == 200
    assert json.loads(attempted.text)["global_user_ids"] == ["42", "200"]
    assert json.loads(attempted.text)["policy_adoption"]["global"]["state"] == "pending"


@pytest.mark.asyncio
async def test_policy_adoption_is_per_worker_and_requires_the_current_revision(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    server = _server(tmp_path, verifier=None, shared=True)
    headers = {"X-Workbench-Token": ADMIN_TOKEN}
    server.orchestrator = SimpleNamespace(runtimes=[])

    async def observed():
        return server.telegram_access_service.status("lily")["policy_revision"]

    lily = SimpleNamespace(name="lily", is_function_worker_proxy=True,
                           observe_telegram_access_policy=AsyncMock(side_effect=observed))
    momo = SimpleNamespace(name="momo", is_function_worker_proxy=True,
                           observe_telegram_access_policy=AsyncMock(return_value="old-revision"))
    server.orchestrator.runtimes = [lily, momo]
    status = json.loads((await server.handle_admin_agent_telegram_access(_Request(headers=headers))).text)
    assert status["policy_adoption"]["agent"]["state"] == "applied"
    assert status["policy_adoption"]["global"]["state"] == "pending"
    changed = await server.handle_admin_agent_telegram_access_change(_Request(
        headers=headers, payload={"expected_revision": status["revision"], "scope": "global",
                                  "action": "add", "user_id": "100"},
    ))
    updated = json.loads(changed.text)
    assert changed.status == 200 and updated["global_user_ids"] == ["42", "100"]
    assert updated["policy_adoption"]["agent"]["state"] == "applied"
    assert updated["policy_adoption"]["global"]["state"] == "pending"


@pytest.mark.asyncio
async def test_policy_adoption_does_not_claim_a_concurrently_replaced_revision(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    server = _server(tmp_path, verifier=None)
    changed = False

    async def observe():
        nonlocal changed
        status = server.telegram_access_service.status("lily")
        if not changed:
            changed = True
            server.telegram_access_service.change(
                "lily", expected_revision=status["revision"], scope="agent", action="add", user_id="200",
            )
        return server.telegram_access_service.status("lily")["policy_revision"]

    server.orchestrator = SimpleNamespace(runtimes=[SimpleNamespace(
        name="lily", is_function_worker_proxy=True,
        observe_telegram_access_policy=AsyncMock(side_effect=observe),
    )])
    headers = {"X-Workbench-Token": ADMIN_TOKEN}
    first = json.loads((await server.handle_admin_agent_telegram_access(_Request(headers=headers))).text)
    assert first["agent_user_ids"] == []
    assert first["policy_adoption"]["agent"]["state"] == "unknown"
    second = json.loads((await server.handle_admin_agent_telegram_access(_Request(headers=headers))).text)
    assert second["agent_user_ids"] == ["200"]
    assert second["policy_adoption"]["agent"]["state"] == "applied"


@pytest.mark.asyncio
async def test_policy_observation_uses_the_actual_function_worker_snapshot(tmp_path):
    from types import SimpleNamespace
    from orchestrator.function_worker_host import FunctionWorkerHost
    from orchestrator.function_worker_supervisor import AgentRuntimeHandle

    server = _server(tmp_path, verifier=None, shared=True)
    config = SimpleNamespace(config_path=tmp_path / "agents.json", secrets_path=tmp_path / "secrets.json")
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.runtime = SimpleNamespace(name="lily", global_config=config)
    host.phase = "ACTIVE"
    host.accepting = True
    host.agent_name = "lily"
    proxy = AgentRuntimeHandle.__new__(AgentRuntimeHandle)
    proxy.name = "lily"
    proxy.kernel = SimpleNamespace(global_cfg=config)

    async def route(method, params=None, **kwargs):
        return await host.handle_request(method, params or {})

    proxy._route = route
    server.orchestrator = SimpleNamespace(runtimes=[proxy])
    headers = {"X-Workbench-Token": ADMIN_TOKEN}
    status = json.loads((await server.handle_admin_agent_telegram_access(_Request(headers=headers))).text)
    assert status["policy_adoption"]["agent"]["state"] == "applied"
    assert status["policy_adoption"]["global"]["state"] == "pending"
    changed = await server.handle_admin_agent_telegram_access_change(_Request(
        headers=headers, payload={"expected_revision": status["revision"], "scope": "global",
                                  "action": "add", "user_id": "100"},
    ))
    result = json.loads(changed.text)
    assert changed.status == 200 and result["policy_revision"] != status["policy_revision"]
    assert result["policy_adoption"]["agent"]["state"] == "applied"
    assert result["policy_adoption"]["global"]["state"] == "pending"


@pytest.mark.asyncio
async def test_shared_telegram_ingress_rechecks_revocation_before_old_worker(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from orchestrator.function_worker_supervisor import AgentRuntimeHandle

    server = _server(tmp_path, verifier=None)
    headers = {"X-Workbench-Token": ADMIN_TOKEN}
    status = json.loads((await server.handle_admin_agent_telegram_access(_Request(headers=headers))).text)
    changed = await server.handle_admin_agent_telegram_access_change(_Request(
        headers=headers, payload={"expected_revision": status["revision"], "scope": "agent",
                                  "action": "add", "user_id": "200"},
    ))
    state = json.loads(changed.text)
    handle = AgentRuntimeHandle.__new__(AgentRuntimeHandle)
    handle.name = "lily"
    handle.kernel = SimpleNamespace(global_cfg=SimpleNamespace(
        config_path=tmp_path / "agents.json", secrets_path=tmp_path / "secrets.json",
    ))
    handle._route = AsyncMock(return_value=True)
    update = {"update_id": 10, "message": {"message_id": 1, "date": 1,
              "chat": {"id": 200, "type": "private"},
              "from": {"id": 200, "is_bot": False, "first_name": "test"}, "text": "hello"}}
    assert await handle.deliver_telegram_update(update)
    assert handle._route.await_count == 1
    revoked = await server.handle_admin_agent_telegram_access_change(_Request(
        headers=headers, payload={"expected_revision": state["revision"], "scope": "agent",
                                  "action": "remove", "user_id": "200"},
    ))
    assert revoked.status == 200
    assert await handle.deliver_telegram_update(update)
    assert handle._route.await_count == 1


@pytest.mark.asyncio
async def test_shared_ingress_does_not_use_owner_scope_or_list_budget_as_authority(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from orchestrator.config_json import write_config_json
    from orchestrator.function_worker_supervisor import AgentRuntimeHandle

    _server(tmp_path, verifier=None)
    config_path = tmp_path / "agents.json"
    config = read_config_json(config_path)
    config["global"]["authorized_telegram_user_ids"] = [200]
    config["global"]["owner_scope_id"] = "invalid local scope"
    config["global"]["telegram_users_per_scope_max"] = "invalid budget"
    write_config_json(config_path, config)
    handle = AgentRuntimeHandle.__new__(AgentRuntimeHandle)
    handle.name = "lily"
    handle.kernel = SimpleNamespace(global_cfg=SimpleNamespace(
        config_path=config_path, secrets_path=tmp_path / "secrets.json",
    ))
    handle._route = AsyncMock(return_value=True)
    update = {"update_id": 11, "message": {"message_id": 1, "date": 1,
              "chat": {"id": 200, "type": "private"},
              "from": {"id": 200, "is_bot": False, "first_name": "test"}, "text": "hello"}}
    assert await handle.deliver_telegram_update(update)
    assert handle._route.await_count == 1


@pytest.mark.asyncio
async def test_large_access_policy_is_readable_and_uses_configured_capacity(tmp_path):
    from orchestrator.config_json import write_config_json

    server = _server(tmp_path, verifier=None)
    config_path = tmp_path / "agents.json"
    config = read_config_json(config_path)
    config["global"]["authorized_telegram_user_ids"] = list(range(1000, 1600))
    config["global"]["telegram_users_per_scope_max"] = 602
    write_config_json(config_path, config)
    headers = {"X-Workbench-Token": ADMIN_TOKEN}
    status = json.loads((await server.handle_admin_agent_telegram_access(_Request(headers=headers))).text)
    assert len(status["global_user_ids"]) == 601 and status["users_per_scope_max"] == 602
    added = await server.handle_admin_agent_telegram_access_change(_Request(
        headers=headers, payload={"expected_revision": status["revision"], "scope": "global",
                                  "action": "add", "user_id": "9999"},
    ))
    updated = json.loads(added.text)
    assert added.status == 200 and len(updated["global_user_ids"]) == 602
    limited = await server.handle_admin_agent_telegram_access_change(_Request(
        headers=headers, payload={"expected_revision": updated["revision"], "scope": "global",
                                  "action": "add", "user_id": "9998"},
    ))
    assert limited.status == 400 and json.loads(limited.text)["error_code"] == "telegram_access_limit"
    config = read_config_json(config_path)
    config["global"]["telegram_users_per_scope_max"] = 10
    write_config_json(config_path, config)
    oversized = json.loads((await server.handle_admin_agent_telegram_access(_Request(headers=headers))).text)
    assert len(oversized["global_user_ids"]) == 602 and oversized["users_per_scope_max"] == 10
    removed = await server.handle_admin_agent_telegram_access_change(_Request(
        headers=headers, payload={"expected_revision": oversized["revision"], "scope": "global",
                                  "action": "remove", "user_id": "1000"},
    ))
    assert removed.status == 200 and len(json.loads(removed.text)["global_user_ids"]) == 601


@pytest.mark.asyncio
async def test_global_access_update_does_not_require_all_workers(tmp_path):
    from types import SimpleNamespace
    from orchestrator.config_json import write_config_json

    server = _server(tmp_path, verifier=None, runtime_active=True, shared=True)
    headers = {"X-Workbench-Token": ADMIN_TOKEN}
    def worker(name, protocol=3, phase="ACTIVE", accepting=True):
        return SimpleNamespace(
            name=name, is_function_worker_proxy=True,
            get_runtime_metadata=lambda: {
                "telegram_scoped_access_protocol": protocol,
                "worker_phase": phase, "worker_accepting": accepting,
            },
        )
    server.orchestrator.runtimes = [worker("lily")]
    response = await server.handle_admin_agent_telegram_access(_Request(headers=headers))
    assert response.status == 200
    assert json.loads(response.text)["global_management_supported"] is True
    server.orchestrator.runtimes.append(worker("momo", protocol=0))
    response = await server.handle_admin_agent_telegram_access(_Request(headers=headers))
    assert response.status == 200

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
    state = json.loads(changed.text)
    blocked = await server.handle_admin_agent_telegram_access_change(_Request(
        headers=headers, payload={"expected_revision": state["revision"],
                                  "scope": "global", "action": "remove", "user_id": "100"},
    ))
    assert blocked.status == 200
    assert json.loads(blocked.text)["global_user_ids"] == ["42"]
    assert json.loads(blocked.text)["policy_adoption"]["global"]["state"] == "pending"

    server.orchestrator.runtimes[1] = worker("momo")
    config = read_config_json(config_path)
    config["agents"][0]["extra"] = {"authorized_telegram_ids": [75]}
    write_config_json(config_path, config)
    assert (await server.handle_admin_agent_telegram_access(_Request(headers=headers))).status == 200
    config = read_config_json(config_path)
    config["agents"][0].pop("extra")
    config["agents"][0]["authorized_telegram_ids"] = [75]
    write_config_json(config_path, config)
    assert (await server.handle_admin_agent_telegram_access(_Request(headers=headers))).status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_protocol,phase,accepting", [
    (True, "ACTIVE", True), (1, "ACTIVE", True), (2, "ACTIVE", True),
    (3, "DRAINING", True), (3, "ACTIVE", False)
])
async def test_access_status_reads_when_worker_compatibility_is_unknown(
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
    assert response.status == 200
    assert json.loads(response.text)["management_supported"] is True
    assert json.loads(response.text)["policy_adoption"]["agent"]["state"] == "pending"


@pytest.mark.asyncio
async def test_workbench_grants_and_revocations_reach_the_selected_worker(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from telegram import Update
    from orchestrator.function_worker_host import FunctionWorkerHost
    from orchestrator import runtime_session
    from tests.test_telegram_actor_scope import instance, update

    server = _server(tmp_path, verifier=None, runtime_active=True, shared=True)
    metadata = {"telegram_scoped_access_protocol": 3,
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
async def test_access_api_uses_scoped_revision_and_rejects_stale_or_invalid_writes(tmp_path):
    server = _server(tmp_path, verifier=None)
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
async def test_missing_admin_token_does_not_grant_access_management(tmp_path):
    server = _server(tmp_path, verifier=None)
    server.admin_token = ""
    response = await server.handle_admin_agent_telegram_access(_Request())
    assert response.status == 403

@pytest.mark.asyncio
async def test_api_can_configure_first_and_additional_users_without_overwriting_secrets(tmp_path):
    server = _server(tmp_path, verifier=None, authorized_id=0, shared=True)
    headers = {"X-Workbench-Token": ADMIN_TOKEN}
    before_secrets = (tmp_path / "secrets.json").read_bytes()

    status = json.loads((await server.handle_admin_agent_telegram_access(_Request(headers=headers))).text)
    assert status["primary_user_id"] is None
    first = json.loads((await server.handle_admin_agent_telegram_access_change(
        _Request(headers=headers, payload={"expected_revision": status["revision"], "scope": "global",
                                           "action": "add", "user_id": "100"})
    )).text)
    assert first["primary_user_id"] is None
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


@pytest.mark.asyncio
async def test_agent_access_is_independent_of_stopped_agent_and_preserves_legacy_grants(tmp_path):
    from types import SimpleNamespace
    from orchestrator.config_json import write_config_json

    server = _server(tmp_path, verifier=None, runtime_active=True, shared=True)
    config_path = tmp_path / 'agents.json'
    config = read_config_json(config_path)
    config['agents'][0]['extra'] = {'authorized_telegram_ids': [75]}
    config['agents'][1]['extra'] = {'authorized_telegram_ids': [300]}
    # Even an inactive legacy holder must not return with a stale owner grant.
    config['agents'][1]['is_active'] = False
    write_config_json(config_path, config)
    def worker(name, protocol=3):
        return SimpleNamespace(name=name, is_function_worker_proxy=True,
            get_runtime_metadata=lambda: {'telegram_scoped_access_protocol': protocol,
                                          'worker_phase': 'ACTIVE', 'worker_accepting': True})
    server.orchestrator.runtimes = [worker('lily')]
    headers = {'X-Workbench-Token': ADMIN_TOKEN}
    response = await server.handle_admin_agent_telegram_access(_Request(headers=headers))
    assert response.status == 200
    state = json.loads(response.text)
    assert state['agent_user_ids'] == ['75']
    assert state['global_management_supported'] is True
    assert state['policy_adoption']['global']['state'] == 'pending'
    changed = await server.handle_admin_agent_telegram_access_change(_Request(
        headers=headers, payload={'expected_revision': state['revision'], 'scope': 'agent',
                                  'action': 'add', 'user_id': '200'}))
    assert changed.status == 200
    state = json.loads(changed.text)
    assert state['agent_user_ids'] == ['75', '200']
    config = read_config_json(config_path)
    assert config['agents'][0]['extra']['authorized_telegram_ids'] == [75]
    assert config['agents'][1]['extra']['authorized_telegram_ids'] == [300]
    assert config['agents'][0]['extra']['telegram_scoped_access_version'] == 1
    assert 'telegram_scoped_access_version' not in config['global']
    blocked = await server.handle_admin_agent_telegram_access_change(_Request(
        headers=headers, payload={'expected_revision': state['revision'], 'scope': 'global',
                                  'action': 'add', 'user_id': '900'}))
    assert blocked.status == 200
    assert json.loads(blocked.text)['global_user_ids'] == ['42', '900']
    server.orchestrator.runtimes.append(worker('momo', protocol=1))
    response = await server.handle_admin_agent_telegram_access(_Request(headers=headers))
    assert json.loads(response.text)['global_management_supported'] is True
    assert json.loads(response.text)['policy_adoption']['global']['state'] == 'pending'
    server.orchestrator.runtimes[1] = worker('momo')
    response = await server.handle_admin_agent_telegram_access(_Request(headers=headers))
    state = json.loads(response.text)
    assert state['global_management_supported'] is True
    changed = await server.handle_admin_agent_telegram_access_change(_Request(
        headers=headers, payload={'expected_revision': state['revision'], 'scope': 'global',
                                  'action': 'add', 'user_id': '900'}))
    assert changed.status == 200
    assert json.loads(changed.text)['global_user_ids'] == ['42', '900']
    config = read_config_json(config_path)
    assert config['agents'][0]['extra']['authorized_telegram_ids'] == [75]
    assert config['agents'][1]['extra']['authorized_telegram_ids'] == [300]
