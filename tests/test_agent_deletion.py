from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from orchestrator.agent_deletion import (
    AgentBusyError,
    AgentDeletionError,
    AgentDeletionService,
    AgentNotFoundError,
    CleanupPendingError,
    ConfirmationMismatchError,
    ConfigConflictDeletionError,
    LastActiveAgentDeletionError,
    PreviewExpiredError,
    PreviewInvalidError,
    SharedWorkspaceError,
    UnmanagedWorkspaceError,
)
from orchestrator.pathing import BridgePaths


def _setup_paths(tmp_path: Path, agents: list[dict], tasks: dict | None = None, secrets: dict | None = None) -> BridgePaths:
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps({"global": {"instance_id": "HASHI1"}, "agents": agents}),
        encoding="utf-8",
    )
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps(tasks or {"version": 1, "heartbeats": [], "crons": [], "nudges": []}),
        encoding="utf-8",
    )
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(
        json.dumps(secrets or {}),
        encoding="utf-8",
    )
    workspaces_root = tmp_path / "workspaces"
    workspaces_root.mkdir(parents=True, exist_ok=True)
    return BridgePaths(
        code_root=tmp_path,
        bridge_home=tmp_path,
        instance_id="HASHI1",
        config_path=config_path,
        secrets_path=secrets_path,
        tasks_path=tasks_path,
        state_path=tmp_path / "state",
        lock_path=tmp_path / "process.lock",
        pid_path=tmp_path / "process.pid",
        workspaces_root=workspaces_root,
    )


def test_preview_inactive_agent_succeeds(tmp_path):
    paths = _setup_paths(
        tmp_path,
        [
            {"name": "momo", "is_active": False, "agent_lifecycle_id": "momo-lc-1"},
            {"name": "koko", "is_active": True, "agent_lifecycle_id": "koko-lc-1"},
        ],
    )
    ws = paths.workspaces_root / "momo"
    ws.mkdir(parents=True)
    (ws / "notes.txt").write_text("hello world")

    service = AgentDeletionService(paths)
    preview = service.preview("momo")

    assert preview.ok is True
    assert preview.agent_id == "momo"
    assert preview.blocked_reasons == []
    assert preview.workspace["managed"] is True
    assert preview.workspace["file_count"] == 1
    assert preview.workspace["bytes"] > 0
    assert bool(preview.preview_token)
    assert preview.recovery_available is True
    assert preview.recovery_retention_days == 7


def test_preview_active_agent_is_blocked(tmp_path):
    paths = _setup_paths(
        tmp_path,
        [
            {"name": "momo", "is_active": True, "agent_lifecycle_id": "momo-lc-1"},
            {"name": "koko", "is_active": True, "agent_lifecycle_id": "koko-lc-1"},
        ],
    )
    service = AgentDeletionService(paths)
    preview = service.preview("momo")
    assert "AGENT_ACTIVE" in preview.blocked_reasons


def test_preview_last_active_agent_is_blocked(tmp_path):
    paths = _setup_paths(
        tmp_path,
        [
            {"name": "momo", "is_active": True, "agent_lifecycle_id": "momo-lc-1"},
            {"name": "koko", "is_active": False, "agent_lifecycle_id": "koko-lc-1"},
        ],
    )
    service = AgentDeletionService(paths)
    preview = service.preview("momo")
    assert "LAST_ACTIVE_AGENT" in preview.blocked_reasons


def test_preview_running_runtime_is_blocked(tmp_path):
    paths = _setup_paths(
        tmp_path,
        [
            {"name": "momo", "is_active": False, "agent_lifecycle_id": "momo-lc-1"},
            {"name": "koko", "is_active": True, "agent_lifecycle_id": "koko-lc-1"},
        ],
    )
    mock_orch = MagicMock()
    mock_orch._runtime_map.return_value = {"momo": MagicMock()}
    service = AgentDeletionService(paths, orchestrator=mock_orch)
    preview = service.preview("momo")
    assert "AGENT_RUNNING" in preview.blocked_reasons


def test_preview_unknown_agent_raises_not_found(tmp_path):
    paths = _setup_paths(tmp_path, [{"name": "koko", "is_active": True}])
    service = AgentDeletionService(paths)
    with pytest.raises(AgentNotFoundError):
        service.preview("nonexistent")


def test_preview_unmanaged_workspace_is_rejected(tmp_path):
    paths = _setup_paths(
        tmp_path,
        [
            {
                "name": "momo",
                "is_active": False,
                "workspace_dir": str(tmp_path.parent / "escape"),
            },
            {"name": "koko", "is_active": True},
        ],
    )
    service = AgentDeletionService(paths)
    with pytest.raises(UnmanagedWorkspaceError):
        service.preview("momo")


def test_preview_shared_workspace_is_rejected(tmp_path):
    paths = _setup_paths(
        tmp_path,
        [
            {"name": "momo", "is_active": False, "workspace_dir": "workspaces/shared"},
            {"name": "koko", "is_active": True, "workspace_dir": "workspaces/shared"},
        ],
    )
    service = AgentDeletionService(paths)
    with pytest.raises(SharedWorkspaceError):
        service.preview("momo")


def test_delete_requires_exact_confirmation(tmp_path):
    paths = _setup_paths(
        tmp_path,
        [
            {"name": "momo", "is_active": False, "agent_lifecycle_id": "momo-lc-1"},
            {"name": "koko", "is_active": True, "agent_lifecycle_id": "koko-lc-1"},
        ],
    )
    service = AgentDeletionService(paths)
    preview = service.preview("momo")

    with pytest.raises(ConfirmationMismatchError):
        service.delete(
            "momo",
            preview_token=preview.preview_token,
            confirmed_agent_id="wrong_id",
        )


def test_delete_rejects_invalid_or_expired_token(tmp_path):
    paths = _setup_paths(
        tmp_path,
        [
            {"name": "momo", "is_active": False, "agent_lifecycle_id": "momo-lc-1"},
            {"name": "koko", "is_active": True, "agent_lifecycle_id": "koko-lc-1"},
        ],
    )
    service = AgentDeletionService(paths)
    with pytest.raises(PreviewInvalidError):
        service.delete("momo", preview_token="bad_token", confirmed_agent_id="momo")


def test_delete_rejects_config_conflict(tmp_path):
    paths = _setup_paths(
        tmp_path,
        [
            {"name": "momo", "is_active": False, "agent_lifecycle_id": "momo-lc-1"},
            {"name": "koko", "is_active": True, "agent_lifecycle_id": "koko-lc-1"},
        ],
    )
    service = AgentDeletionService(paths)
    preview = service.preview("momo")

    # Modify agents.json out-of-band to change revision
    raw = json.loads(paths.config_path.read_text(encoding="utf-8"))
    raw["agents"].append({"name": "extra", "is_active": False})
    paths.config_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigConflictDeletionError):
        service.delete(
            "momo",
            preview_token=preview.preview_token,
            confirmed_agent_id="momo",
        )


def test_delete_lifecycle_conflict_fails(tmp_path):
    paths = _setup_paths(
        tmp_path,
        [
            {"name": "momo", "is_active": False, "agent_lifecycle_id": "momo-lc-1"},
            {"name": "koko", "is_active": True, "agent_lifecycle_id": "koko-lc-1"},
        ],
    )
    service = AgentDeletionService(paths)
    preview = service.preview("momo")

    # Recreate agent with same name but new lifecycle
    raw = json.loads(paths.config_path.read_text(encoding="utf-8"))
    raw["agents"][0]["agent_lifecycle_id"] = "momo-lc-2"
    paths.config_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigConflictDeletionError):
        service.delete(
            "momo",
            preview_token=preview.preview_token,
            confirmed_agent_id="momo",
        )


def test_delete_success_cleans_tasks_secrets_and_quarantines(tmp_path):
    paths = _setup_paths(
        tmp_path,
        agents=[
            {
                "name": "momo",
                "is_active": False,
                "agent_lifecycle_id": "momo-lc-1",
                "telegram_token_key": "momo_token",
            },
            {
                "name": "koko",
                "is_active": True,
                "agent_lifecycle_id": "koko-lc-1",
                "telegram_token_key": "shared_token",
            },
        ],
        tasks={
            "version": 1,
            "heartbeats": [{"agent": "momo", "interval": 60}, {"agent": "koko", "interval": 120}],
            "crons": [{"agent": "momo", "time": "12:00"}],
            "nudges": [],
        },
        secrets={
            "momo_token": "secret_momo",
            "shared_token": "secret_koko",
        },
    )
    ws = paths.workspaces_root / "momo"
    ws.mkdir(parents=True)
    (ws / "file.txt").write_text("data")

    service = AgentDeletionService(paths)
    preview = service.preview("momo")

    receipt = service.delete(
        "momo",
        preview_token=preview.preview_token,
        confirmed_agent_id="momo",
        idempotency_key="idemp-123",
    )

    assert receipt["ok"] is True
    assert receipt["status"] == "succeeded"
    assert receipt["workspace_quarantined"] is True

    # 1. Check agents.json
    cfg = json.loads(paths.config_path.read_text(encoding="utf-8"))
    remaining_names = [a["name"] for a in cfg["agents"]]
    assert "momo" not in remaining_names
    assert "koko" in remaining_names

    # 2. Check tasks.json
    tasks_data = json.loads(paths.tasks_path.read_text(encoding="utf-8"))
    assert len(tasks_data["heartbeats"]) == 1
    assert tasks_data["heartbeats"][0]["agent"] == "koko"
    assert len(tasks_data["crons"]) == 0

    # 3. Check secrets.json: unshared momo_token removed, shared_token kept
    secrets_data = json.loads(paths.secrets_path.read_text(encoding="utf-8"))
    assert "momo_token" not in secrets_data
    assert "shared_token" in secrets_data

    # 4. Check workspace quarantined
    assert not ws.exists()
    quarantine_items = list(service.quarantine_root.glob("momo_*"))
    assert len(quarantine_items) == 1
    assert (quarantine_items[0] / "file.txt").read_text() == "data"

    # 5. Idempotent repeat returns existing receipt
    repeat = service.delete(
        "momo",
        preview_token=preview.preview_token,
        confirmed_agent_id="momo",
        idempotency_key="idemp-123",
    )
    assert repeat["ok"] is True
    assert repeat["operation_id"] == receipt["operation_id"]


def test_delete_cleanup_pending_on_quarantine_error(tmp_path, monkeypatch):
    paths = _setup_paths(
        tmp_path,
        agents=[
            {"name": "momo", "is_active": False, "agent_lifecycle_id": "momo-lc-1"},
            {"name": "koko", "is_active": True, "agent_lifecycle_id": "koko-lc-1"},
        ],
    )
    ws = paths.workspaces_root / "momo"
    ws.mkdir(parents=True)
    service = AgentDeletionService(paths)
    preview = service.preview("momo")

    def failing_quarantine(agent_id, ws_path):
        raise OSError("quarantine disk full")

    monkeypatch.setattr(service, "_quarantine_workspace", failing_quarantine)

    with pytest.raises(CleanupPendingError) as exc_info:
        service.delete(
            "momo",
            preview_token=preview.preview_token,
            confirmed_agent_id="momo",
        )

    op_id = exc_info.value.operation_id
    receipt = service.get_receipt(op_id)
    assert receipt is not None
    assert receipt["status"] == "cleanup_pending"
    assert "quarantine disk full" in receipt["error"]


class _DummyRequest:
    def __init__(self, *, match_info=None, payload=None, headers=None):
        self.match_info = match_info or {}
        self._payload = payload
        self.headers = headers or {}

    async def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_workbench_api_deletion_endpoints(tmp_path):
    from types import SimpleNamespace
    from orchestrator.workbench_api import WorkbenchApiServer

    paths = _setup_paths(
        tmp_path,
        agents=[
            {"name": "momo", "is_active": False, "agent_lifecycle_id": "momo-lc-1"},
            {"name": "koko", "is_active": True, "agent_lifecycle_id": "koko-lc-1"},
        ],
    )
    global_config = SimpleNamespace(
        deployment_profile="personal",
        bridge_home=tmp_path,
        workbench_port=18800,
        project_root=tmp_path,
    )
    server = WorkbenchApiServer(config_path=paths.config_path, global_config=global_config)
    server.admin_token = "secret"

    # 1. Unauthenticated request gets 403
    unauth = await server.handle_admin_agent_deletion_preview(
        _DummyRequest(match_info={"agent_id": "momo"})
    )
    assert unauth.status == 403

    # Authenticate requests
    auth_headers = {"X-Workbench-Token": "secret"}

    # 2. Preview
    res_preview = await server.handle_admin_agent_deletion_preview(
        _DummyRequest(match_info={"agent_id": "momo"}, headers=auth_headers)
    )
    assert res_preview.status == 200
    preview_data = json.loads(res_preview.text)
    assert preview_data["ok"] is True
    assert preview_data["agent_id"] == "momo"
    token = preview_data["preview_token"]

    # 3. Commit
    res_delete = await server.handle_admin_agent_deletion(
        _DummyRequest(
            match_info={"agent_id": "momo"},
            payload={"preview_token": token, "confirmed_agent_id": "momo"},
            headers=auth_headers,
        )
    )
    assert res_delete.status == 200
    del_data = json.loads(res_delete.text)
    assert del_data["ok"] is True
    assert del_data["status"] == "succeeded"
    op_id = del_data["operation_id"]

    # 4. Status
    res_status = await server.handle_admin_agent_deletion_status(
        _DummyRequest(match_info={"operation_id": op_id}, headers=auth_headers)
    )
    assert res_status.status == 200
    status_data = json.loads(res_status.text)
    assert status_data["ok"] is True
    assert status_data["receipt"]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_workbench_api_advertises_agent_deletion(tmp_path):
    from types import SimpleNamespace
    from orchestrator.workbench_api import WorkbenchApiServer

    paths = _setup_paths(tmp_path, agents=[{"name": "koko", "is_active": True}])
    global_config = SimpleNamespace(
        deployment_profile="personal",
        bridge_home=tmp_path,
        workbench_port=18800,
        project_root=tmp_path,
    )
    server = WorkbenchApiServer(config_path=paths.config_path, global_config=global_config)
    server._v1_owner_id = lambda req: "user:1"

    res = await server.handle_v1_capabilities(_DummyRequest())
    assert res.status == 200
    data = json.loads(res.text)
    assert "agent_deletion" in data
    assert data["agent_deletion"]["supported"] is True
