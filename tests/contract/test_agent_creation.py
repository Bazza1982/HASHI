"""Contract tests for the HASHI-owned public Agent creation service."""

from __future__ import annotations

import json

import pytest

from orchestrator.agent_creation import (
    AgentCreationService,
    AgentCreationSpec,
    AgentExistsError,
    ConfigConflictCreationError,
    InvalidBackendError,
    InvalidAgentNameError,
    InvalidEffortError,
    WorkspaceExistsError,
    build_agent_config,
    build_her_backend_row,
    validate_agent_name,
)
from orchestrator.agent_incarnation import AGENT_LIFECYCLE_FIELD
from orchestrator.config_admin import ConfigAdmin
from orchestrator.config_json import ConfigConflictError
from orchestrator.pathing import BridgePaths


HER_PROVIDER_PROFILES = {
    "hashi": {
        "base_url": "http://127.0.0.1:18801/v1",
        "fast_model": "gpt-5.6-luna",
        "pro_model": "gpt-5.6-sol",
        "secret": None,
        "status": "provisional",
    }
}


def _paths(tmp_path) -> BridgePaths:
    home = tmp_path / "home"
    (home / "workspaces").mkdir(parents=True)
    config_path = home / "agents.json"
    config_path.write_text('{"global": {}, "agents": []}\n', encoding="utf-8")
    return BridgePaths(
        code_root=home,
        bridge_home=home,
        instance_id="TEST",
        config_path=config_path,
        secrets_path=home / "secrets.json",
        tasks_path=home / "tasks.json",
        state_path=home / "scheduler_state.json",
        lock_path=home / "process.lock",
        pid_path=home / "process.pid",
        workspaces_root=home / "workspaces",
    )


def test_agent_name_validation_rejects_paths():
    assert validate_agent_name("researcher-1") == "researcher-1"
    for value in ("../escape", "agent/name", "agent name", "-agent", ""):
        with pytest.raises(InvalidAgentNameError):
            validate_agent_name(value)


def test_service_creates_inactive_agent_through_authoritative_config_writer(tmp_path):
    paths = _paths(tmp_path)
    result = AgentCreationService(paths, global_config={}).create(
        AgentCreationSpec(
            name="researcher",
            display_name="Researcher",
            backend="codex-cli",
            model="gpt-5.6-sol",
            effort="medium",
            is_active=False,
        )
    )

    stored = json.loads(paths.config_path.read_text(encoding="utf-8"))
    assert result.config_published is True
    assert stored["agents"][0]["name"] == "researcher"
    assert stored["agents"][0]["is_active"] is False
    assert stored["agents"][0]["active_backend"] == "codex-cli"
    assert stored["agents"][0].get(AGENT_LIFECYCLE_FIELD)
    assert "tools" not in stored["agents"][0]["allowed_backends"][0]
    assert (paths.workspaces_root / "researcher" / "agent.md").is_file()


def test_public_spec_has_no_raw_agent_config_field():
    assert "agent_cfg" not in AgentCreationSpec.__dataclass_fields__
    assert "preset" not in AgentCreationSpec.__dataclass_fields__


def test_her_creation_efforts_persist_mode_without_changing_provider_profiles():
    provider_rows = []
    for effort in ("zero", "low", "medium"):
        row = build_her_backend_row(effort, HER_PROVIDER_PROFILES)
        assert row["effort"] == effort
        profiles = row["her_v2"]["profiles"]
        assert set(profiles) == {
            "lightweight", "triage", "premium", "reviewer", "orchestrator"
        }
        encoded = json.dumps(row)
        assert "secret" not in encoded
        assert "base_url" not in encoded
        provider_rows.append(row["her_v2"])
    assert provider_rows[0] == provider_rows[1] == provider_rows[2]
    with pytest.raises(InvalidEffortError):
        build_her_backend_row("unknown", HER_PROVIDER_PROFILES)


def test_non_selectable_backend_is_rejected():
    with pytest.raises(InvalidBackendError):
        build_agent_config(
            AgentCreationSpec(name="bad", backend="openrouter-api"),
            HER_PROVIDER_PROFILES,
        )


def test_duplicate_and_orphan_workspace_are_preserved(tmp_path):
    paths = _paths(tmp_path)
    service = AgentCreationService(paths, global_config={})
    service.create(AgentCreationSpec(name="one", backend="codex-cli"))
    with pytest.raises(AgentExistsError):
        service.create(AgentCreationSpec(name="one", backend="codex-cli"))

    orphan = paths.workspaces_root / "orphan"
    orphan.mkdir()
    keep = orphan / "keep.txt"
    keep.write_text("preserve", encoding="utf-8")
    with pytest.raises(WorkspaceExistsError):
        service.create(AgentCreationSpec(name="orphan", backend="codex-cli"))
    assert keep.read_text(encoding="utf-8") == "preserve"


def test_config_conflict_removes_only_new_scaffold(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    service = AgentCreationService(paths, global_config={})

    def fail_write(self, raw_cfg):
        raise ConfigConflictError("stale revision")

    monkeypatch.setattr(ConfigAdmin, "write_raw_config", fail_write)
    with pytest.raises(ConfigConflictCreationError):
        service.create(AgentCreationSpec(name="conflict", backend="codex-cli"))
    assert not (paths.workspaces_root / "conflict").exists()

    existing = paths.workspaces_root / "existing"
    existing.mkdir()
    keep = existing / "keep.txt"
    keep.write_text("preserve", encoding="utf-8")
    with pytest.raises(ConfigConflictError):
        ConfigAdmin(paths).add_agent_to_config("existing", {"engine": "codex-cli"})
    assert keep.read_text(encoding="utf-8") == "preserve"
    assert not (existing / "agent.md").exists()
