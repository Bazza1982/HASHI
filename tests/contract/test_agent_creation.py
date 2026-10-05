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


def test_her_v3_creation_persists_one_model_and_provider_reasoning_effort():
    provider_rows = []
    for effort in ("none", "high", "max"):
        row = build_her_backend_row(effort, HER_PROVIDER_PROFILES)
        assert row["effort"] == effort
        assert row["model"] == "gpt-5.6-sol"
        assert row["her_v2"]["main"] == {
            "provider": "hashi-api",
            "model": "gpt-5.6-sol",
        }
        encoded = json.dumps(row)
        assert "secret" not in encoded
        assert "base_url" not in encoded
        provider_rows.append(row["her_v2"])
    assert provider_rows[0] == provider_rows[1] == provider_rows[2]
    with pytest.raises(InvalidEffortError):
        build_her_backend_row("unknown", HER_PROVIDER_PROFILES)


def test_her_v3_creation_is_publicly_named_but_keeps_compatible_storage(tmp_path):
    paths = _paths(tmp_path)
    result = AgentCreationService(
        paths,
        global_config={"her_providers": {"providers": HER_PROVIDER_PROFILES}},
    ).create(
        AgentCreationSpec(
            name="her-agent",
            backend="her-v3",
            model="gpt-5.6-sol",
            effort="high",
        )
    )

    stored = json.loads(paths.config_path.read_text(encoding="utf-8"))
    assert result.active_backend == "her-v3"
    assert stored["agents"][0]["active_backend"] == "her-v2"
    assert stored["agents"][0]["allowed_backends"][0]["her_v2"]["main"] == {
        "provider": "hashi-api",
        "model": "gpt-5.6-sol",
    }


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


def test_explicit_template_optin_and_full_backend_policy_persist(tmp_path, monkeypatch):
    import shutil
    from orchestrator.config_json import read_config_json, write_config_json
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/" + cmd)
    paths = _paths(tmp_path)
    raw = read_config_json(paths.config_path)
    raw["global"]["agent_creation"] = {"template_agent": "seed", "grant_mode": "template"}
    raw["agents"] = [{"name": "seed", "active_backend": "codex-cli", "allowed_backends": [
        {"engine": "codex-cli", "model": "gpt-6.1-sol", "default_model": "gpt-6-sol", "models": ["gpt-6.1-sol"],
         "model_efforts": {"gpt-6.1-sol": ["low", "medium", "high", "xhigh", "max", "ultra"]},
         "tools": ["dangerous_tool"], "permission_mode": "unrestricted"},
        {"engine": "claude-cli", "model": "claude-sonnet-4-6"},
    ]}]
    write_config_json(paths.config_path, raw)
    service = AgentCreationService(paths, global_config={})
    service.create(AgentCreationSpec(name="optin", backend="codex-cli", model="gpt-6.1-sol", effort="ultra"))
    stored = read_config_json(paths.config_path)["agents"][-1]
    assert stored["active_backend"] == "codex-cli"
    assert {row["engine"] for row in stored["allowed_backends"]} == {"codex-cli", "claude-cli"}
    selected = stored["allowed_backends"][0]
    assert selected["model"] == selected["default_model"] == "gpt-6.1-sol" and selected["effort"] == "ultra"
    assert selected["models"] == ["gpt-6.1-sol"]
    assert selected["model_efforts"]["gpt-6.1-sol"][-1] == "ultra"
    assert not any("tools" in row or "permission_mode" in row for row in stored["allowed_backends"])
    catalogue = service.creation_catalogue()
    assert "gpt-6.1-sol" in catalogue["backends"]["codex-cli"]["models"]
    assert catalogue["backends"]["codex-cli"]["model_efforts"]["gpt-6.1-sol"][-1] == "ultra"


def test_missing_explicit_template_fails_before_workspace_creation(tmp_path):
    from orchestrator.config_json import read_config_json, write_config_json
    paths = _paths(tmp_path)
    raw = read_config_json(paths.config_path)
    raw["global"]["agent_creation"] = {"template_agent": "missing", "grant_mode": "template"}
    write_config_json(paths.config_path, raw)
    with pytest.raises(Exception, match="creation template"):
        AgentCreationService(paths, global_config={}).create(AgentCreationSpec(name="blocked", backend="codex-cli"))
    assert not (paths.workspaces_root / "blocked").exists()


def test_creation_grants_only_installed_backends_and_authorized_credentialed_providers(tmp_path, monkeypatch):
    import shutil
    from orchestrator.config_json import read_config_json, write_config_json
    from orchestrator.agent_creation import InvalidBackendError, InvalidEffortError
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/codex" if "codex" in cmd else None)
    paths = _paths(tmp_path)
    raw = read_config_json(paths.config_path)
    raw["global"] = {"agent_creation": {"template_agent": "seed", "grant_mode": "template"},
        "her_providers": {"providers": {
            "deepseek": {"models": ["deepseek-v4-pro"], "secret": "qa-provider-key"},
            "openrouter": {"models": ["deepseek/deepseek-v4-pro"], "secret": "missing-key"},
        }}}
    raw["agents"] = [{"name": "seed", "allowed_backends": [
        {"engine": "codex-cli", "model": "gpt-6-astra", "model_efforts": {"gpt-6-astra": []}},
        {"engine": "claude-cli", "model": "claude-sonnet-4-6"},
        {"engine": "her-v2", "model": "deepseek-v4-pro", "effort": "high", "her_v2": {
            "main": {"provider": "deepseek-api", "model": "deepseek-v4-pro"},
            "v3_provider_allowlist": ["deepseek-api", "openrouter-api"], "tools": ["never-inherit"]}},
    ]}]
    write_config_json(paths.config_path, raw)
    write_config_json(paths.secrets_path, {"qa-provider-key": "synthetic-test-credential"})
    service = AgentCreationService(paths)
    view = service.creation_catalogue()
    assert view["backends"]["claude-cli"]["available"] is False
    assert view["backends"]["claude-cli"]["reason"] == "backend_not_installed"
    assert view["backends"]["her-v3"]["providers"]["openrouter-api"]["available"] is False
    assert view["backends"]["codex-cli"]["model_efforts"]["gpt-6-astra"] == []
    with pytest.raises(InvalidEffortError):
        service.create(AgentCreationSpec(name="bad-effort", backend="codex-cli", effort="high"))
    with pytest.raises(InvalidBackendError):
        service.create(AgentCreationSpec(name="uninstalled", backend="claude-cli"))
    with pytest.raises(InvalidBackendError):
        service.create(AgentCreationSpec(name="no-key", backend="her-v3", provider="openrouter-api"))
    service.create(AgentCreationSpec(name="ordinary", backend="codex-cli"))
    service.create(AgentCreationSpec(name="limited", backend="codex-cli", restricted=True))
    ordinary, limited = read_config_json(paths.config_path)["agents"][-2:]
    assert [row["engine"] for row in ordinary["allowed_backends"]] == ["codex-cli", "her-v2"]
    her = ordinary["allowed_backends"][1]
    assert her["her_v2"]["v3_provider_allowlist"] == ["deepseek-api"]
    assert "tools" not in her["her_v2"]
    assert [row["engine"] for row in limited["allowed_backends"]] == ["codex-cli"]
    assert not any((paths.workspaces_root / name).exists() for name in ("bad-effort", "uninstalled", "no-key"))


def test_creation_keeps_template_revision_until_publication(tmp_path, monkeypatch):
    import shutil
    from orchestrator.config_json import read_config_json, write_config_json
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/" + cmd)
    paths = _paths(tmp_path)
    raw = read_config_json(paths.config_path)
    raw["global"]["agent_creation"] = {"template_agent": "seed", "grant_mode": "template"}
    raw["agents"] = [{"name": "seed", "allowed_backends": [{"engine": "codex-cli", "model": "gpt-6-astra"}]}]
    write_config_json(paths.config_path, raw)
    original = ConfigAdmin.add_agent_to_config
    def racing_write(admin, name, cfg, **kwargs):
        latest = read_config_json(paths.config_path)
        latest["global"]["agent_creation"]["template_agent"] = "changed-template"
        write_config_json(paths.config_path, latest)
        return original(admin, name, cfg, **kwargs)
    monkeypatch.setattr(ConfigAdmin, "add_agent_to_config", racing_write)
    with pytest.raises(ConfigConflictCreationError):
        AgentCreationService(paths).create(AgentCreationSpec(name="stale-policy", backend="codex-cli"))
    latest = read_config_json(paths.config_path)
    assert latest["global"]["agent_creation"]["template_agent"] == "changed-template"
    assert [row["name"] for row in latest["agents"]] == ["seed"]
    assert not (paths.workspaces_root / "stale-policy").exists()
