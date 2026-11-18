from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.privacy_levels import PrivacyLevel
from orchestrator.skill_manager import SkillManager
from orchestrator.workbench_api import WorkbenchApiServer


class Request:
    def __init__(self, *, operation=None, payload=None, target_instance="HASHI3",
                 target_agent="momo", query=None, job_id=None, headers=None):
        self.match_info = {"name": "momo", "agent_id": "momo"}
        if job_id is not None:
            self.match_info["job_id"] = job_id
        self.query = query or {"instance_id": target_instance}
        self.headers = (
            {"X-Workbench-Token": "test-admin"}
            if headers is None else dict(headers)
        )
        self.scheme = "http"
        self.host = "127.0.0.1"
        self._body = {
            "type": "hashi.frontend-settings-operation",
            "version": 1,
            "request_id": "settings-test-1",
            "target": {"instance_id": target_instance, "agent_id": target_agent},
            "operation": operation,
            "payload": payload or {},
        }

    async def json(self):
        return self._body


class Runtime:
    name = "momo"

    def __init__(self, workspace: Path, manager: SkillManager):
        self.workspace_dir = workspace
        self.skill_manager = manager
        self.config = SimpleNamespace(active_backend="openrouter-api")
        self.backend_manager = SimpleNamespace(privacy_level=PrivacyLevel.PROVIDER_TRUST)
        self.privacy_writes = []

        def set_privacy_level(level, *, expected_current_level=None):
            self.privacy_writes.append((int(level), expected_current_level))
            self.backend_manager.privacy_level = PrivacyLevel(int(level))
            return self.backend_manager.privacy_level

        self.backend_manager.set_privacy_level = set_privacy_level


def make_server(tmp_path: Path):
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({"crons": [], "heartbeats": [], "nudges": []}), encoding="utf-8")
    workspace = tmp_path / "workspaces" / "momo"
    workspace.mkdir(parents=True)
    runtime = Runtime(workspace, SkillManager(tmp_path, tasks))
    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps({"global": {}, "agents": [{"name": "momo"}]}), encoding="utf-8")
    global_config = SimpleNamespace(
        bridge_home=tmp_path,
        project_root=tmp_path,
        deployment_profile="personal",
        instance_id="HASHI3",
    )
    orchestrator = SimpleNamespace(runtimes=[runtime], scheduler=SimpleNamespace(state={}))
    server = WorkbenchApiServer(
        config_path=config_path,
        global_config=global_config,
        orchestrator=orchestrator,
        secrets={"workbench_admin_token": "test-admin"},
    )
    return server, runtime


def decode(response):
    return json.loads(response.body.decode("utf-8"))


def test_frontend_settings_capability_uses_existing_hashi_authority() -> None:
    capability = WorkbenchApiServer._frontend_settings_capability()

    assert capability["authority"] == "HASHI Frontend Connector"
    assert capability["target_fields"] == ["instance_id", "agent_id"]
    assert "privacy.set_level" in capability["operations"]
    assert "permissions.read" in capability["operations"]
    assert "permissions.set_access_scope" in capability["operations"]
    assert "activity.scheduler.create" in capability["operations"]
    assert capability["privacy_levels"] == [0, 1, 2]
    assert capability["autonomy_risk_ladder"] is False
    assert capability["permissions_scope_marker_optional"] is True


@pytest.mark.asyncio
async def test_frontend_settings_envelope_fails_closed_for_auth_and_wrong_targets(tmp_path, monkeypatch) -> None:
    server, runtime = make_server(tmp_path)
    monkeypatch.setattr(server, "_strict_authenticated_owner", lambda _request: None)

    unauthenticated = await server.handle_v1_agent_settings_privacy_set(
        Request(operation="privacy.set_level", payload={"level": 0, "expected_current_level": 1})
    )
    assert unauthenticated.status == 401

    monkeypatch.setattr(server, "_strict_authenticated_owner", lambda _request: "owner")
    wrong_instance = await server.handle_v1_agent_settings_privacy_set(
        Request(operation="privacy.set_level", target_instance="HASHI4",
                payload={"level": 0, "expected_current_level": 1, "confirm_downgrade": True})
    )
    assert wrong_instance.status == 409
    assert decode(wrong_instance)["error_code"] == "settings_target_instance_mismatch"

    wrong_agent = await server.handle_v1_agent_settings_privacy_set(
        Request(operation="privacy.set_level", target_agent="someone-else",
                payload={"level": 0, "expected_current_level": 1, "confirm_downgrade": True})
    )
    assert wrong_agent.status == 409
    assert decode(wrong_agent)["error_code"] == "settings_target_agent_mismatch"
    assert runtime.privacy_writes == []


@pytest.mark.asyncio
async def test_frontend_settings_activity_uses_agent_scheduler_and_current_delete_revision(tmp_path) -> None:
    server, _runtime = make_server(tmp_path)
    create = await server.handle_v1_agent_settings_activity_create(
        Request(
            operation="activity.scheduler.create",
            payload={
                "kind": "cron",
                "task_id": "workbench-daily",
                "schedule": "0 9 * * *",
                "prompt": "Review the open work and report.",
                "action": "enqueue_prompt",
                "enabled": True,
            },
        )
    )
    assert create.status == 201
    assert decode(create)["result"]["authority"] == "HASHI Scheduler"

    listing = await server.handle_v1_agent_settings_activity_get(
        Request(operation=None, query={"instance_id": "HASHI3"})
    )
    listed = decode(listing)["result"]["jobs"]
    assert [item["job_id"] for item in listed] == ["workbench-daily"]

    update = await server.handle_v1_agent_settings_activity_update(
        Request(operation="activity.scheduler.update", job_id="workbench-daily",
                payload={"kind": "cron", "enabled": False})
    )
    assert update.status == 200
    assert decode(update)["result"]["job"]["enabled"] is False

    denied = await server.handle_v1_agent_settings_activity_delete(
        Request(operation="activity.scheduler.delete", job_id="workbench-daily",
                payload={"kind": "cron"})
    )
    assert denied.status == 400

    deleted = await server.handle_v1_agent_settings_activity_delete(
        Request(operation="activity.scheduler.delete", job_id="workbench-daily",
                payload={"kind": "cron",
                         "expected_revision": decode(update)["result"]["job"]["deletion_revision"]})
    )
    assert deleted.status == 200
    assert decode(deleted)["result"]["job_id"] == "workbench-daily"

    generated = await server.handle_v1_agent_settings_activity_create(
        Request(
            operation="activity.scheduler.create",
            payload={
                "kind": "heartbeat",
                "task_id": "",
                "interval_seconds": 30,
                "prompt": "Check for work.",
                "action": "enqueue_prompt",
                "enabled": True,
            },
        )
    )
    assert generated.status == 201
    assert decode(generated)["result"]["job"]["job_id"].startswith("momo-heartbeat-")


@pytest.mark.asyncio
async def test_frontend_permissions_update_hashi_access_scope_and_confirm_expansion(tmp_path: Path) -> None:
    server, runtime = make_server(tmp_path)
    config_path = tmp_path / 'agents.json'
    document = json.loads(config_path.read_text(encoding='utf-8'))
    document['agents'][0].update({
        'active_backend': 'codex-cli',
        'access_scope': 'project',
        'allowed_backends': [{'engine': 'codex-cli', 'access_scope': 'project'}],
    })
    config_path.write_text(json.dumps(document), encoding='utf-8')
    manager = SimpleNamespace(
        config=SimpleNamespace(
            active_backend='codex-cli',
            allowed_backends=[{'engine': 'codex-cli', 'access_scope': 'project'}],
        ),
        current_backend=SimpleNamespace(config=SimpleNamespace(access_scope='project')),
        _active_model_override=None,
    )
    switches = []

    async def switch_backend(engine, *, target_model=None):
        switches.append((engine, target_model))
        scope = manager.config.allowed_backends[0].get('access_scope', 'project')
        manager.current_backend.config.access_scope = scope
        return True

    manager.switch_backend = switch_backend
    runtime.backend_manager = manager
    runtime.config = manager.config

    read = await server.handle_v1_agent_settings_permissions_get(
        Request(operation=None, query={'instance_id': 'HASHI3'})
    )
    assert read.status == 200
    assert decode(read)['permissions']['access_scope'] == 'project'

    denied_expansion = await server.handle_v1_agent_settings_permissions_set(
        Request(operation='permissions.set_access_scope', payload={
            'backend': 'codex-cli', 'expected_current_scope': 'project',
            'access_scope': 'drive', 'confirm_scope_expansion': False,
        })
    )
    assert denied_expansion.status == 409
    assert decode(denied_expansion)['error_code'] == 'permission_scope_expansion_confirmation_required'
    assert switches == []

    narrowed = await server.handle_v1_agent_settings_permissions_set(
        Request(operation='permissions.set_access_scope', payload={
            'backend': 'codex-cli', 'expected_current_scope': 'project',
            'access_scope': 'workspace', 'confirm_scope_expansion': False,
        })
    )
    assert narrowed.status == 200
    assert runtime.backend_manager.current_backend.config.access_scope == 'workspace'
    saved = json.loads(config_path.read_text(encoding='utf-8'))
    assert saved['agents'][0]['allowed_backends'][0]['access_scope'] == 'workspace'

    widened_without_confirmation = await server.handle_v1_agent_settings_permissions_set(
        Request(operation='permissions.set_access_scope', payload={
            'backend': 'codex-cli', 'expected_current_scope': 'workspace',
            'access_scope': 'project', 'confirm_scope_expansion': False,
        })
    )
    assert widened_without_confirmation.status == 409
    widened = await server.handle_v1_agent_settings_permissions_set(
        Request(operation='permissions.set_access_scope', payload={
            'backend': 'codex-cli', 'expected_current_scope': 'workspace',
            'access_scope': 'project',
        })
    )
    assert widened.status == 200
    assert runtime.backend_manager.current_backend.config.access_scope == 'project'


@pytest.mark.asyncio
async def test_scope_http_direct_save_has_auth_stale_and_explicit_opt_out_controls(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    server, runtime = make_server(tmp_path)
    document = json.loads(server.config_path.read_text(encoding="utf-8"))
    document["agents"][0].update({
        "active_backend": "codex-cli", "access_scope": "workspace",
        "allowed_backends": [{"engine": "codex-cli", "access_scope": "workspace"}],
    })
    server.config_path.write_text(json.dumps(document), encoding="utf-8")
    manager = SimpleNamespace(
        config=SimpleNamespace(active_backend="codex-cli", allowed_backends=[
            {"engine": "codex-cli", "access_scope": "workspace"},
        ]),
        current_backend=SimpleNamespace(config=SimpleNamespace(access_scope="workspace")),
        _active_model_override=None,
    )
    calls = []

    async def switch_backend(engine, *, target_model=None):
        calls.append(engine)
        manager.current_backend.config.access_scope = manager.config.allowed_backends[0]["access_scope"]
        return True

    manager.switch_backend = switch_backend
    runtime.backend_manager = manager
    runtime.config = manager.config
    url = "/api/v1/agents/momo/settings/permissions"
    envelope = {
        "type": "hashi.frontend-settings-operation", "version": 1, "request_id": "scope-http",
        "target": {"instance_id": "HASHI3", "agent_id": "momo"},
        "operation": "permissions.set_access_scope",
        "payload": {"backend": "codex-cli", "expected_current_scope": "workspace", "access_scope": "project"},
    }
    headers = {"X-Workbench-Token": "test-admin"}
    async with TestClient(TestServer(server.app)) as client:
        missing_auth = await client.patch(url, json=envelope)
        assert missing_auth.status == 401 and calls == []

        denied = {**envelope, "payload": {**envelope["payload"], "confirm_scope_expansion": False}}
        explicitly_denied = await client.patch(url, json=denied, headers=headers)
        assert explicitly_denied.status == 409 and calls == []

        wrong = {**envelope, "target": {"instance_id": "HASHI4", "agent_id": "momo"}}
        wrong_target = await client.patch(url, json=wrong, headers=headers)
        assert wrong_target.status == 409 and calls == []

        stale = {**envelope, "payload": {**envelope["payload"], "expected_current_scope": "drive"}}
        conflict = await client.patch(url, json=stale, headers=headers)
        assert conflict.status == 409 and calls == []

        saved = await client.patch(url, json=envelope, headers=headers)
        assert saved.status == 200 and calls == ["codex-cli"]
        assert (await saved.json())["permissions"]["access_scope"] == "project"
        assert json.loads(server.config_path.read_text(encoding="utf-8"))[
            "agents"][0]["allowed_backends"][0]["access_scope"] == "project"

        invalid = {**envelope, "payload": {
            **envelope["payload"], "expected_current_scope": "project",
            "access_scope": "drive", "confirm_scope_expansion": "true",
        }}
        malformed = await client.patch(url, json=invalid, headers=headers)
        assert malformed.status == 400 and calls == ["codex-cli"]

        legacy = {**envelope, "payload": {**invalid["payload"], "confirm_scope_expansion": True}}
        accepted_legacy = await client.patch(url, json=legacy, headers=headers)
        assert accepted_legacy.status == 200 and calls == ["codex-cli", "codex-cli"]
        assert (await accepted_legacy.json())["permissions"]["access_scope"] == "drive"


@pytest.mark.asyncio
async def test_governed_settings_use_existing_agent_visibility_and_management_policy(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    server, runtime = make_server(tmp_path)
    server.global_config.deployment_profile = "enterprise"

    class Identity:
        def get_session_user(self, token):
            return SimpleNamespace(id=token) if token in {"outside", "member", "manager"} else None

        def list_project_memberships(self, *, user_id):
            return {
                "outside": [{"role": "individual_user", "project_id": "other"}],
                "member": [{"role": "individual_user", "project_id": "project-a"}],
                "manager": [{"role": "team_admin", "project_id": "project-a"}],
            }[user_id]

    server.identity_service = Identity()
    document = json.loads(server.config_path.read_text(encoding="utf-8"))
    document["agents"][0].update({
        "project_id": "project-a", "active_backend": "codex-cli", "access_scope": "workspace",
        "allowed_backends": [{"engine": "codex-cli", "access_scope": "workspace"}],
    })
    server.config_path.write_text(json.dumps(document), encoding="utf-8")
    manager = SimpleNamespace(
        config=SimpleNamespace(active_backend="codex-cli", allowed_backends=[
            {"engine": "codex-cli", "access_scope": "workspace"},
        ]),
        current_backend=SimpleNamespace(config=SimpleNamespace(access_scope="workspace")),
        _active_model_override=None, privacy_level=PrivacyLevel.PROVIDER_TRUST,
    )
    calls = []

    def set_privacy_level(level, *, expected_current_level=None):
        assert int(manager.privacy_level) == expected_current_level
        manager.privacy_level = level
        return level

    manager.set_privacy_level = set_privacy_level

    async def switch_backend(engine, *, target_model=None):
        calls.append(engine)
        manager.current_backend.config.access_scope = manager.config.allowed_backends[0]["access_scope"]
        return True

    manager.switch_backend = switch_backend
    runtime.backend_manager = manager
    runtime.config = manager.config
    url = "/api/v1/agents/momo/settings/permissions"
    envelope = {
        "type": "hashi.frontend-settings-operation", "version": 1, "request_id": "governed-scope",
        "target": {"instance_id": "HASHI3", "agent_id": "momo"},
        "operation": "permissions.set_access_scope",
        "payload": {"backend": "codex-cli", "expected_current_scope": "workspace", "access_scope": "project"},
    }
    async with TestClient(TestServer(server.app)) as client:
        headers = lambda token: {"Authorization": "Bearer " + token}  # noqa: E731
        outsider_read = await client.get(url + "?instance_id=HASHI3", headers=headers("outside"))
        outsider_write = await client.patch(url, json=envelope, headers=headers("outside"))
        assert (outsider_read.status, outsider_write.status, calls) == (404, 404, [])
        privacy_url = "/api/v1/agents/momo/settings/privacy"
        activity_url = "/api/v1/agents/momo/settings/activity"
        for other_url in (privacy_url, activity_url):
            hidden = await client.get(other_url + "?instance_id=HASHI3", headers=headers("outside"))
            assert hidden.status == 404
        privacy_request = {
            **envelope, "operation": "privacy.set_level",
            "payload": {"level": 0, "expected_current_level": 1, "confirm_downgrade": True},
        }
        hidden_write = await client.patch(privacy_url, json=privacy_request, headers=headers("outside"))
        assert hidden_write.status == 404

        member_read = await client.get(url + "?instance_id=HASHI3", headers=headers("member"))
        assert member_read.status == 200
        member_write = await client.patch(url, json=envelope, headers=headers("member"))
        assert member_write.status == 403 and calls == []
        member_privacy = await client.get(privacy_url + "?instance_id=HASHI3", headers=headers("member"))
        assert member_privacy.status == 200
        member_privacy_write = await client.patch(privacy_url, json=privacy_request, headers=headers("member"))
        assert member_privacy_write.status == 403
        assert manager.privacy_level == PrivacyLevel.PROVIDER_TRUST

        manager_write = await client.patch(url, json=envelope, headers=headers("manager"))
        assert manager_write.status == 200 and calls == ["codex-cli"]
        assert (await manager_write.json())["permissions"]["access_scope"] == "project"
        manager_privacy = await client.patch(privacy_url, json=privacy_request, headers=headers("manager"))
        assert manager_privacy.status == 200
        assert manager.privacy_level == PrivacyLevel.OFF


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["while_waiting", "during_switch", "rollback_fails"])
async def test_governed_scope_change_rechecks_authority_and_compensates(tmp_path, mode):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer

    server, runtime = make_server(tmp_path)
    server.global_config.deployment_profile = "enterprise"

    class Identity:
        active = True
        role = "team_admin"

        def get_session_user(self, token):
            return SimpleNamespace(id="manager") if self.active and token == "manager" else None

        def list_project_memberships(self, *, user_id):
            assert user_id == "manager"
            return [{"role": self.role, "project_id": "project-a"}]

    identity = Identity()
    server.identity_service = identity
    document = json.loads(server.config_path.read_text(encoding="utf-8"))
    document["agents"][0].update({
        "project_id": "project-a", "active_backend": "codex-cli", "access_scope": "workspace",
        "allowed_backends": [{"engine": "codex-cli", "access_scope": "workspace"}],
    })
    server.config_path.write_text(json.dumps(document), encoding="utf-8")
    manager = SimpleNamespace(
        config=SimpleNamespace(active_backend="codex-cli", allowed_backends=[
            {"engine": "codex-cli", "access_scope": "workspace"},
        ]),
        current_backend=SimpleNamespace(config=SimpleNamespace(access_scope="workspace")),
        _active_model_override=None,
    )
    calls = []

    async def switch_backend(_engine, *, target_model=None):
        scope = manager.config.allowed_backends[0]["access_scope"]
        calls.append(scope)
        if len(calls) == 1 and mode != "while_waiting":
            manager.current_backend.config.access_scope = scope
            identity.role = "individual_user"
            return True
        if len(calls) == 2 and mode == "rollback_fails":
            return False
        manager.current_backend.config.access_scope = scope
        return True

    manager.switch_backend = switch_backend
    runtime.backend_manager = manager
    runtime.config = manager.config
    url = "/api/v1/agents/momo/settings/permissions"
    envelope = {
        "type": "hashi.frontend-settings-operation", "version": 1, "request_id": "governed-race",
        "target": {"instance_id": "HASHI3", "agent_id": "momo"},
        "operation": "permissions.set_access_scope",
        "payload": {"backend": "codex-cli", "expected_current_scope": "workspace", "access_scope": "project"},
    }
    headers = {"Authorization": "Bearer manager"}
    async with TestClient(TestServer(server.app)) as client:
        if mode == "while_waiting":
            started = asyncio.Event()
            original_guard = server._guard_agent_management_access

            def notify_guard(request, agent):
                result = original_guard(request, agent)
                started.set()
                return result

            server._guard_agent_management_access = notify_guard
            await server._agent_config_lock.acquire()
            try:
                task = asyncio.create_task(client.patch(url, json=envelope, headers=headers))
                await asyncio.wait_for(started.wait(), timeout=5)
                identity.active = False
            finally:
                server._agent_config_lock.release()
            response = await asyncio.wait_for(task, timeout=5)
            assert response.status == 401 and calls == []
        else:
            response = await client.patch(url, json=envelope, headers=headers)
            if mode == "during_switch":
                assert response.status == 403
                assert calls == ["project", "workspace"]
                assert manager.current_backend.config.access_scope == "workspace"
            else:
                assert response.status == 503
                assert (await response.json())["error_code"] == "settings_persistence_uncertain"
                assert calls == ["project", "workspace"]
        saved = json.loads(server.config_path.read_text(encoding="utf-8"))
        assert saved["agents"][0]["allowed_backends"][0]["access_scope"] == "workspace"


@pytest.mark.asyncio
async def test_governed_privacy_rechecks_role_after_async_readiness(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    server, runtime = make_server(tmp_path)
    server.global_config.deployment_profile = "enterprise"

    class Identity:
        role = "team_admin"

        def get_session_user(self, token):
            return SimpleNamespace(id="manager") if token == "manager" else None

        def list_project_memberships(self, *, user_id):
            assert user_id == "manager"
            return [{"role": self.role, "project_id": "project-a"}]

    identity = Identity()
    server.identity_service = identity
    document = json.loads(server.config_path.read_text(encoding="utf-8"))
    document["agents"][0]["project_id"] = "project-a"
    server.config_path.write_text(json.dumps(document), encoding="utf-8")

    async def readiness():
        identity.role = "individual_user"

    monkeypatch.setattr("orchestrator.runtime_privacy._verify_level_two_detector", readiness)
    monkeypatch.setattr("orchestrator.runtime_privacy.get_supported_privacy_levels", lambda _backend: (0, 1, 2))
    envelope = {
        "type": "hashi.frontend-settings-operation", "version": 1, "request_id": "privacy-race",
        "target": {"instance_id": "HASHI3", "agent_id": "momo"}, "operation": "privacy.set_level",
        "payload": {"level": 2, "expected_current_level": 1, "accept_level_two_risk": True},
    }
    async with TestClient(TestServer(server.app)) as client:
        response = await client.patch("/api/v1/agents/momo/settings/privacy", json=envelope,
                                      headers={"Authorization": "Bearer manager"})
        assert response.status == 403
        assert runtime.privacy_writes == []
        assert runtime.backend_manager.privacy_level == PrivacyLevel.PROVIDER_TRUST


@pytest.mark.asyncio
async def test_frontend_permissions_reports_uncertain_state_when_persistence_rollback_fails(
    tmp_path: Path, monkeypatch,
) -> None:
    from orchestrator.config_json import ConfigConflictError

    server, runtime = make_server(tmp_path)
    config_path = tmp_path / "agents.json"
    document = json.loads(config_path.read_text(encoding="utf-8"))
    document["agents"][0].update({
        "active_backend": "codex-cli",
        "access_scope": "project",
        "allowed_backends": [{"engine": "codex-cli", "access_scope": "project"}],
    })
    config_path.write_text(json.dumps(document), encoding="utf-8")
    manager = SimpleNamespace(
        config=SimpleNamespace(
            active_backend="codex-cli",
            allowed_backends=[{"engine": "codex-cli", "access_scope": "project"}],
        ),
        current_backend=SimpleNamespace(config=SimpleNamespace(access_scope="project")),
        _active_model_override=None,
    )
    switch_results = iter((True, False))

    async def switch_backend(_engine, *, target_model=None):
        applied = next(switch_results)
        if applied:
            manager.current_backend.config.access_scope = (
                manager.config.allowed_backends[0]["access_scope"]
            )
        return applied

    manager.switch_backend = switch_backend
    runtime.backend_manager = manager
    runtime.config = manager.config

    publications = []

    def conflict(path, proposed):
        publications.append((path, proposed))
        raise ConfigConflictError("changed concurrently")

    monkeypatch.setattr("orchestrator.agent_config_json.write_agent_config_json", conflict)
    response = await server.handle_v1_agent_settings_permissions_set(
        Request(operation="permissions.set_access_scope", payload={
            "backend": "codex-cli",
            "expected_current_scope": "project",
            "access_scope": "workspace",
            "confirm_scope_expansion": False,
        })
    )

    assert response.status == 503
    assert decode(response)["error_code"] == "settings_persistence_uncertain"
    assert len(publications) == 1
    assert publications[0][0] == config_path
    assert publications[0][1]["agents"][0]["allowed_backends"][0]["access_scope"] == "workspace"
    assert manager.current_backend.config.access_scope == "workspace"
    assert json.loads(config_path.read_text(encoding="utf-8")) == document


async def test_frontend_privacy_requires_fresh_value_and_downgrade_confirmation(tmp_path) -> None:
    server, runtime = make_server(tmp_path)

    stale = await server.handle_v1_agent_settings_privacy_set(
        Request(operation="privacy.set_level",
                payload={"level": 0, "expected_current_level": 0, "confirm_downgrade": True})
    )
    assert stale.status == 409
    assert decode(stale)["error_code"] == "privacy_state_changed"
    assert runtime.privacy_writes == []

    unconfirmed = await server.handle_v1_agent_settings_privacy_set(
        Request(operation="privacy.set_level",
                payload={"level": 0, "expected_current_level": 1})
    )
    assert unconfirmed.status == 409
    assert decode(unconfirmed)["error_code"] == "privacy_downgrade_confirmation_required"
    assert runtime.privacy_writes == []

    applied = await server.handle_v1_agent_settings_privacy_set(
        Request(operation="privacy.set_level",
                payload={
                    "level": 0,
                    "expected_current_level": 1,
                    "confirm_downgrade": True,
                })
    )
    assert applied.status == 200
    assert runtime.privacy_writes == [(0, 1)]
    assert decode(applied)["privacy"]["current_level"] == 0
# HASHI-Fingerprint: hfp_c066c6d2f33145a88df8cbc4ce6b2381
