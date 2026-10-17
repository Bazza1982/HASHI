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
                 target_agent="momo", query=None, job_id=None):
        self.match_info = {"name": "momo", "agent_id": "momo"}
        if job_id is not None:
            self.match_info["job_id"] = job_id
        self.query = query or {"instance_id": target_instance}
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
    )
    server._strict_authenticated_owner = lambda _request: "owner"
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
async def test_frontend_settings_activity_uses_agent_scheduler_and_requires_delete_confirmation(tmp_path) -> None:
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
    assert denied.status == 403

    deleted = await server.handle_v1_agent_settings_activity_delete(
        Request(operation="activity.scheduler.delete", job_id="workbench-daily",
                payload={"kind": "cron", "explicit_user_confirmation": True})
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
            'access_scope': 'project', 'confirm_scope_expansion': True,
        })
    )
    assert widened.status == 200
    assert runtime.backend_manager.current_backend.config.access_scope == 'project'


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

    def conflict(*_args, **_kwargs):
        raise ConfigConflictError("changed concurrently")

    monkeypatch.setattr("orchestrator.config_json.write_config_json", conflict)
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
