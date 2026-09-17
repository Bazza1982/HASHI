"""Contract tests for the public HASHI agent creation service.

Covers identifier validation, duplicate/workspace collisions, ordinary
backend row construction, HER v2 creation presets, lifecycle identity and
the narrow filesystem rollback semantics around config publication.
"""

from __future__ import annotations

import json
import os

import pytest

from orchestrator import agent_creation
from orchestrator.agent_incarnation import AGENT_LIFECYCLE_FIELD
from orchestrator.agent_creation import (
    AgentCreationService,
    AgentCreationSpec,
    AgentExistsError,
    ConfigConflictCreationError,
    CreationFailedError,
    InvalidAgentNameError,
    InvalidBackendError,
    InvalidEffortError,
    InvalidHerPresetError,
    InvalidModelError,
    WorkspaceExistsError,
    build_agent_config,
    build_her_backend_row,
    build_ordinary_backend_row,
    validate_agent_name,
)
from orchestrator.config import default_agent_mode_for_backend
from orchestrator.config_admin import ConfigAdmin
from orchestrator.config_json import ConfigConflictError, ConfigDurabilityError
from orchestrator.flexible_backend_registry import (
    HER_V2_ENGINE,
    get_available_models,
    get_provider_reasoning_efforts,
)
from orchestrator.pathing import BridgePaths

HER_PROVIDER_PROFILES = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "secret": "deepseek-api_key",
        "status": "stable",
    },
    "hashi": {
        "base_url": "http://127.0.0.1:18801/v1",
        "fast_model": "gpt-5.6-luna",
        "pro_model": "gpt-5.6-sol",
        "secret": None,
        "status": "provisional",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "secret": "openrouter-api_key",
        "status": "stable",
    },
}


def make_paths(tmp_path) -> BridgePaths:
    home = tmp_path / "home"
    (home / "workspaces").mkdir(parents=True)
    config_path = home / "agents.json"
    config_path.write_text(
        json.dumps({"agents": []}), encoding="utf-8"
    )
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


def load_agents(config_path):
    return json.loads(config_path.read_text(encoding="utf-8"))["agents"]


# --- identifier validation -------------------------------------------------


def test_validate_agent_name_accepts_simple_identifiers():
    for name in ("researcher", "agent-1", "my_agent", "A1", "a" * 64):
        assert validate_agent_name(name) == name


def test_validate_agent_name_rejects_unsafe_identifiers():
    for name in (
        "",
        " ",
        "agent name",
        "foo/bar",
        "foo\\bar",
        "../evil",
        ".",
        "..",
        "/tmp/x",
        "~",
        " agent",
        "agent ",
        "C:\\temp",
        "-lead",
        "a" * 65,
        None,
    ):
        with pytest.raises(InvalidAgentNameError):
            validate_agent_name(name)


# --- ordinary backend rows -------------------------------------------------


def test_ordinary_backend_row_respects_registry_model_and_effort():
    row = build_ordinary_backend_row("codex-cli", "gpt-5.6-sol", "medium")
    assert row == {
        "engine": "codex-cli",
        "model": "gpt-5.6-sol",
        "effort": "medium",
    }


def test_ordinary_backend_row_fills_default_model():
    row = build_ordinary_backend_row("codex-cli", None, None)
    assert row["engine"] == "codex-cli"
    assert row["model"] in get_available_models("codex-cli")


def test_ordinary_backend_row_rejects_unknown_model():
    with pytest.raises(InvalidModelError):
        build_ordinary_backend_row("codex-cli", "not-a-model", None)


def test_ordinary_backend_row_rejects_unknown_effort():
    with pytest.raises(InvalidEffortError):
        build_ordinary_backend_row("codex-cli", "gpt-5.6-sol", "turbo")


def test_provider_only_backend_is_not_selectable():
    with pytest.raises(InvalidBackendError):
        build_agent_config(
            AgentCreationSpec(name="agent", backend="openrouter-api"),
            HER_PROVIDER_PROFILES,
        )


def test_removed_backend_is_not_selectable():
    with pytest.raises(InvalidBackendError):
        build_agent_config(
            AgentCreationSpec(name="agent", backend="claw-cli"),
            HER_PROVIDER_PROFILES,
        )


# --- HER presets -----------------------------------------------------------


@pytest.mark.parametrize("preset", ["fast", "balanced", "maximum"])
def test_her_preset_builds_five_valid_role_profiles(preset):
    row = build_her_backend_row(preset, HER_PROVIDER_PROFILES)
    assert row["engine"] == HER_V2_ENGINE
    assert row["model"] == "role-configured"
    profiles = row["her_v2"]["profiles"]
    assert sorted(profiles.keys()) == [
        "lightweight",
        "orchestrator",
        "premium",
        "reviewer",
        "triage",
    ]
    for role, profile in profiles.items():
        engine = profile["engine"]
        model = profile["model"]
        assert engine in {"deepseek-api", "hashi-api", "openrouter-api"}
        assert model
        reasoning = profile.get("reasoning")
        if reasoning is not None:
            assert reasoning in get_provider_reasoning_efforts(engine, model)


def test_her_preset_rows_never_embed_secrets():
    blob = json.dumps(build_her_backend_row("balanced", HER_PROVIDER_PROFILES))
    assert "secret" not in blob
    assert "api_key" not in blob
    assert "base_url" not in blob


def test_her_preset_rejects_unknown_preset():
    with pytest.raises(InvalidHerPresetError):
        build_her_backend_row("ultra", HER_PROVIDER_PROFILES)


def test_her_preset_without_configured_providers_fails_closed():
    with pytest.raises(CreationFailedError):
        build_her_backend_row("balanced", {})


def test_her_alias_backend_resolves_to_her_v2():
    row = build_agent_config(
        AgentCreationSpec(name="agent", backend="her", preset="fast"),
        HER_PROVIDER_PROFILES,
    )
    assert row["active_backend"] == HER_V2_ENGINE
    assert row["allowed_backends"][0]["engine"] == HER_V2_ENGINE


# --- service level ---------------------------------------------------------


def test_service_creates_ordinary_agent_with_safe_scaffold(tmp_path):
    paths = make_paths(tmp_path)
    service = AgentCreationService(paths, global_config={})
    result = service.create(
        AgentCreationSpec(
            name="researcher",
            backend="codex-cli",
            display_name="Researcher",
            model="gpt-5.6-sol",
            effort="medium",
            is_active=False,
        )
    )
    assert result.workspace_created is True
    assert result.config_published is True
    assert result.active_backend == "codex-cli"
    assert result.is_active is False

    rows = load_agents(paths.config_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "researcher"
    assert row["display_name"] == "Researcher"
    assert row["type"] == "flex"
    assert row["workspace_dir"] == "workspaces/researcher"
    assert row["is_active"] is False
    assert row["active_backend"] == "codex-cli"
    assert row["default_mode"] == default_agent_mode_for_backend("codex-cli")
    assert len(row["allowed_backends"]) == 1
    backend_row = row["allowed_backends"][0]
    assert backend_row["engine"] == "codex-cli"
    assert "tools" not in backend_row
    assert "grants" not in backend_row
    assert row.get(AGENT_LIFECYCLE_FIELD)

    agent_md = paths.workspaces_root / "researcher" / "agent.md"
    assert agent_md.exists()
    assert "researcher" in agent_md.read_text(encoding="utf-8")


def test_service_generates_fresh_lifecycle_id_per_creation(tmp_path):
    paths = make_paths(tmp_path)
    service = AgentCreationService(paths, global_config={})
    service.create(AgentCreationSpec(name="one", backend="codex-cli"))
    service.create(AgentCreationSpec(name="two", backend="codex-cli"))
    rows = load_agents(paths.config_path)
    ids = {row.get(AGENT_LIFECYCLE_FIELD) for row in rows}
    assert len(ids) == 2
    assert all(ids)


def test_service_rejects_duplicate_agent(tmp_path):
    paths = make_paths(tmp_path)
    service = AgentCreationService(paths, global_config={})
    service.create(AgentCreationSpec(name="dup", backend="codex-cli"))
    with pytest.raises(AgentExistsError):
        service.create(AgentCreationSpec(name="dup", backend="codex-cli"))
    assert len(load_agents(paths.config_path)) == 1


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive filesystem check")
def test_service_rejects_case_colliding_agent_on_windows(tmp_path):
    paths = make_paths(tmp_path)
    service = AgentCreationService(paths, global_config={})
    service.create(AgentCreationSpec(name="Researcher", backend="codex-cli"))
    with pytest.raises(AgentExistsError):
        service.create(AgentCreationSpec(name="researcher", backend="codex-cli"))


def test_service_rejects_orphan_workspace_and_preserves_contents(tmp_path):
    paths = make_paths(tmp_path)
    orphan = paths.workspaces_root / "orphan"
    orphan.mkdir()
    keep = orphan / "keep.txt"
    keep.write_text("precious", encoding="utf-8")
    service = AgentCreationService(paths, global_config={})
    with pytest.raises(WorkspaceExistsError):
        service.create(AgentCreationSpec(name="orphan", backend="codex-cli"))
    assert keep.read_text(encoding="utf-8") == "precious"
    assert load_agents(paths.config_path) == []


def test_service_creates_her_agent_with_preset_profiles(tmp_path):
    paths = make_paths(tmp_path)
    service = AgentCreationService(
        paths, global_config={"her_providers": {"providers": HER_PROVIDER_PROFILES}}
    )
    result = service.create(
        AgentCreationSpec(name="analyst", backend="her-v2", preset="balanced")
    )
    assert result.active_backend == HER_V2_ENGINE
    row = load_agents(paths.config_path)[0]
    backend_row = row["allowed_backends"][0]
    assert backend_row["engine"] == HER_V2_ENGINE
    assert backend_row["model"] == "role-configured"
    profiles = backend_row["her_v2"]["profiles"]
    assert sorted(profiles.keys()) == [
        "lightweight",
        "orchestrator",
        "premium",
        "reviewer",
        "triage",
    ]
    blob = json.dumps(row)
    assert "secret" not in blob
    assert "base_url" not in blob


def test_service_display_name_never_mutates_identifier(tmp_path):
    paths = make_paths(tmp_path)
    service = AgentCreationService(paths, global_config={})
    service.create(
        AgentCreationSpec(
            name="safe-name",
            backend="codex-cli",
            display_name="../evil display",
        )
    )
    rows = load_agents(paths.config_path)
    assert rows[0]["name"] == "safe-name"
    assert rows[0]["workspace_dir"] == "workspaces/safe-name"
    assert not (paths.workspaces_root / "evil").exists()


# --- rollback semantics ----------------------------------------------------


def test_config_admin_conflict_rolls_back_created_scaffold(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    admin = ConfigAdmin(paths)

    def fail_write(self, raw_cfg):
        raise ConfigConflictError("simulated stale revision")

    monkeypatch.setattr(ConfigAdmin, "write_raw_config", fail_write)
    with pytest.raises(ConfigConflictError):
        admin.add_agent_to_config("ghost", {"engine": "codex-cli"})
    assert not (paths.workspaces_root / "ghost").exists()


def test_config_admin_conflict_preserves_preexisting_workspace(
    tmp_path, monkeypatch
):
    paths = make_paths(tmp_path)
    existing = paths.workspaces_root / "existing"
    existing.mkdir()
    keep = existing / "keep.txt"
    keep.write_text("precious", encoding="utf-8")
    admin = ConfigAdmin(paths)

    def fail_write(self, raw_cfg):
        raise ConfigConflictError("simulated stale revision")

    monkeypatch.setattr(ConfigAdmin, "write_raw_config", fail_write)
    with pytest.raises(ConfigConflictError):
        admin.add_agent_to_config("existing", {"engine": "codex-cli"})
    assert keep.read_text(encoding="utf-8") == "precious"
    assert not (existing / "agent.md").exists()


def test_config_admin_durability_error_never_rolls_back(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    admin = ConfigAdmin(paths)

    def fail_durability(self, raw_cfg):
        raise ConfigDurabilityError("simulated committed publication")

    monkeypatch.setattr(ConfigAdmin, "write_raw_config", fail_durability)
    with pytest.raises(ConfigDurabilityError):
        admin.add_agent_to_config("durable", {"engine": "codex-cli"})
    assert (paths.workspaces_root / "durable" / "agent.md").exists()


def test_service_conflict_cleans_scaffold_and_maps_error(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    service = AgentCreationService(paths, global_config={})

    def fail_write(self, raw_cfg):
        raise ConfigConflictError("simulated stale revision")

    monkeypatch.setattr(ConfigAdmin, "write_raw_config", fail_write)
    with pytest.raises(ConfigConflictCreationError):
        service.create(AgentCreationSpec(name="cleanup", backend="codex-cli"))
    assert not (paths.workspaces_root / "cleanup").exists()


def test_service_durability_with_committed_row_reports_success(
    tmp_path, monkeypatch
):
    paths = make_paths(tmp_path)
    service = AgentCreationService(paths, global_config={})

    real_write = ConfigAdmin.write_raw_config

    def write_then_warn(self, raw_cfg):
        # Publish for real, then raise the durability signal.
        real_write(self, raw_cfg)
        raise ConfigDurabilityError("simulated committed publication")

    monkeypatch.setattr(ConfigAdmin, "write_raw_config", write_then_warn)
    result = service.create(AgentCreationSpec(name="committed", backend="codex-cli"))
    assert result.durability_warning is True
    assert result.config_published is True
    rows = load_agents(paths.config_path)
    assert [row["name"] for row in rows] == ["committed"]


def test_service_lost_race_reports_agent_exists(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    service = AgentCreationService(paths, global_config={})

    def pretend_lost(self, agent_name, agent_cfg=None, token=None):
        return False

    monkeypatch.setattr(ConfigAdmin, "add_agent_to_config", pretend_lost)
    with pytest.raises(AgentExistsError):
        service.create(AgentCreationSpec(name="raced", backend="codex-cli"))