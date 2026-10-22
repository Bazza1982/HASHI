"""Contract tests for the HASHI-owned public Agent creation service."""

from __future__ import annotations

from contextlib import contextmanager
import json
import multiprocessing
from pathlib import Path
import shutil

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
from orchestrator.config_admin import AgentWorkspaceConflictError, ConfigAdmin
from orchestrator.config_json import ConfigConflictError, ConfigDurabilityError
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


def _paths_for_root(tmp_path) -> BridgePaths:
    home = tmp_path / "home"
    return BridgePaths(
        code_root=home,
        bridge_home=home,
        instance_id="TEST",
        config_path=home / "agents.json",
        secrets_path=home / "secrets.json",
        tasks_path=home / "tasks.json",
        state_path=home / "scheduler_state.json",
        lock_path=home / "process.lock",
        pid_path=home / "process.pid",
        workspaces_root=home / "workspaces",
    )


def _paths(tmp_path) -> BridgePaths:
    paths = _paths_for_root(tmp_path)
    home = paths.bridge_home
    (home / "workspaces").mkdir(parents=True)
    paths.config_path.write_text(
        '{"global": {}, "agents": []}\n',
        encoding="utf-8",
    )
    return paths


def _service_create_contender(
    root: str,
    identity: str,
    held,
    release,
    attempted,
    proved_blocked,
    results,
) -> None:
    """Exercise the real Windows process lock through AgentCreationService."""

    import orchestrator.config_admin as config_admin_module

    shutil.which = lambda command: f"C:/test/{command}.exe"
    paths = _paths_for_root(Path(root))
    if identity == "a":
        real_pcm_write = config_admin_module.atomic_write_pcm

        def held_pcm_write(path, content):
            result = real_pcm_write(path, content)
            held.set()
            if not release.wait(timeout=15):
                raise RuntimeError("test did not release the first creator")
            return result

        config_admin_module.atomic_write_pcm = held_pcm_write
    else:
        real_file_lock = config_admin_module.file_write_lock

        @contextmanager
        def observed_file_lock(target, *args, **kwargs):
            attempted.set()
            try:
                with real_file_lock(target, timeout=0):
                    raise AssertionError("second creator acquired the held owner lock")
            except TimeoutError:
                proved_blocked.set()
            with real_file_lock(target, *args, **kwargs):
                yield

        config_admin_module.file_write_lock = observed_file_lock
    try:
        result = AgentCreationService(paths, global_config={}).create(
            AgentCreationSpec(
                name="same-agent",
                display_name=f"Owner {identity}",
                backend="codex-cli",
            )
        )
        results.put((identity, "created", result.config_published))
    except AgentExistsError:
        results.put((identity, "exists", False))
    except BaseException as exc:
        results.put((identity, "error", repr(exc)))


@pytest.fixture
def installed_cli_backends(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda command: f"/usr/bin/{command}")


def test_agent_name_validation_rejects_paths():
    assert validate_agent_name("researcher-1") == "researcher-1"
    for value in ("../escape", "agent/name", "agent name", "-agent", ""):
        with pytest.raises(InvalidAgentNameError):
            validate_agent_name(value)


def test_service_creates_inactive_agent_through_authoritative_config_writer(
    tmp_path, installed_cli_backends
):
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


def test_duplicate_and_orphan_workspace_are_preserved(tmp_path, installed_cli_backends):
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


def test_config_conflict_removes_only_new_scaffold(
    tmp_path, monkeypatch, installed_cli_backends
):
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
    with pytest.raises(AgentWorkspaceConflictError):
        ConfigAdmin(paths).add_agent_to_config("existing", {"engine": "codex-cli"})
    assert keep.read_text(encoding="utf-8") == "preserve"
    assert not (existing / "agent.md").exists()


def test_same_name_service_creators_share_one_cross_process_owner(tmp_path):
    paths = _paths(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    held = ctx.Event()
    release = ctx.Event()
    attempted = ctx.Event()
    proved_blocked = ctx.Event()
    results = ctx.Queue()
    first = ctx.Process(
        target=_service_create_contender,
        args=(str(tmp_path), "a", held, release, attempted, proved_blocked, results),
    )
    second = ctx.Process(
        target=_service_create_contender,
        args=(str(tmp_path), "b", held, release, attempted, proved_blocked, results),
    )
    first.start()
    try:
        assert held.wait(timeout=15), "first creator did not reach its held phase"
        second.start()
        assert attempted.wait(timeout=15), "second creator did not reach the owner lock"
        assert proved_blocked.wait(timeout=15), "the actual OS lock did not exclude it"
        release.set()
        outcomes = sorted(results.get(timeout=20) for _ in range(2))
        assert outcomes == [
            ("a", "created", True),
            ("b", "exists", False),
        ]
    finally:
        release.set()
        for child in (first, second):
            if child.pid is None:
                continue
            child.join(timeout=10)
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
    assert first.exitcode == second.exitcode == 0
    stored = json.loads(paths.config_path.read_text(encoding="utf-8"))
    assert [row["name"] for row in stored["agents"]] == ["same-agent"]
    agent_md = paths.workspaces_root / "same-agent" / "agent.md"
    assert "Owner a" in agent_md.read_text(encoding="utf-8")
    assert not list(paths.workspaces_root.glob(".agent-create-*"))


def test_unknown_post_commit_error_retains_published_scaffold_and_marker(
    tmp_path, monkeypatch, installed_cli_backends
):
    paths = _paths(tmp_path)
    real_write = ConfigAdmin.write_raw_config

    def commit_then_lose_ack(self, raw_cfg):
        real_write(self, raw_cfg)
        raise OSError("injected error after config commit")

    monkeypatch.setattr(ConfigAdmin, "write_raw_config", commit_then_lose_ack)
    with pytest.raises(OSError, match="after config commit"):
        AgentCreationService(paths, global_config={}).create(
            AgentCreationSpec(name="uncertain", backend="codex-cli")
        )
    stored = json.loads(paths.config_path.read_text(encoding="utf-8"))
    assert [row["name"] for row in stored["agents"]] == ["uncertain"]
    workspace = paths.workspaces_root / "uncertain"
    assert (workspace / "agent.md").is_file()
    assert (workspace / ".hashi-agent-create-owner").is_file()


def test_committed_durability_warning_returns_receipt_without_marker(
    tmp_path, monkeypatch, installed_cli_backends
):
    paths = _paths(tmp_path)
    real_write = ConfigAdmin.write_raw_config

    def commit_with_warning(self, raw_cfg):
        real_write(self, raw_cfg)
        raise ConfigDurabilityError("injected directory sync warning")

    monkeypatch.setattr(ConfigAdmin, "write_raw_config", commit_with_warning)
    result = AgentCreationService(paths, global_config={}).create(
        AgentCreationSpec(name="durability", backend="codex-cli")
    )
    assert result.config_published is True
    assert result.durability_warning is True
    workspace = paths.workspaces_root / "durability"
    assert (workspace / "agent.md").is_file()
    assert not (workspace / ".hashi-agent-create-owner").exists()


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
    assert view["backends"]["her-v3"]["providers"]["openrouter"]["available"] is False
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


@pytest.mark.parametrize("grant_mode,restricted", [
    ("selected", False), ("template", True), ("template", False),
])
def test_herv3_creation_preserves_provider_policy_without_granting_its_engine(
    tmp_path, grant_mode, restricted
):
    from orchestrator.config_json import read_config_json, write_config_json
    from orchestrator.agent_creation_policy import (
        build_effective_backend_catalogue, selection_row,
    )
    from orchestrator.runtime_effort_options import get_available_efforts

    paths = _paths(tmp_path)
    active, disabled, blocked = "client-thinking", "client-fixed", "client-blocked"
    policy = {
        "engine": "ollama-api", "model": active,
        "models": [active, disabled, blocked],
        "available_models": [active, disabled],
        "model_efforts": {active: ["enabled", "off"], disabled: []},
        "tools": ["must-not-inherit"], "permission_mode": "must-not-inherit",
    }
    raw = read_config_json(paths.config_path)
    raw["global"] = {
        "agent_creation": {"template_agent": "seed", "grant_mode": grant_mode},
        "her_providers": {"providers": {"ollama": {
            "engine": "ollama-api", "models": [active, disabled, blocked],
        }}},
    }
    raw["agents"] = [{"name": "seed", "allowed_backends": [
        {"engine": "her-v2", "effort": "enabled", "her_v2": {
            "main": {"provider": "ollama", "model": active},
            "v3_provider_allowlist": ["ollama-api"],
        }},
        policy,
    ]}]
    write_config_json(paths.config_path, raw)
    service = AgentCreationService(paths)
    before = service.creation_catalogue()["backends"]["her-v3"]["providers"]["ollama"]
    assert before["model_efforts"][active] == ["enabled", "off"]

    service.create(AgentCreationSpec(
        name="child", backend="her-v3", provider="ollama", model=active,
        effort="off", restricted=restricted, is_active=False,
    ))
    stored = read_config_json(paths.config_path)["agents"][-1]
    rows = stored["allowed_backends"]
    assert {row["engine"] for row in rows} == (
        {"her-v2", "ollama-api"} if grant_mode == "template" and not restricted
        else {"her-v2"}
    )
    assert not any("tools" in row or "permission_mode" in row for row in rows)
    # A created Agent can itself become a template without losing its policy.
    inherited = [selection_row(row) for row in rows]
    view = build_effective_backend_catalogue(
        inherited, raw["global"]["her_providers"]["providers"],
    )["her-v3"]["providers"]["ollama"]
    assert view["models"] == before["models"] == [active, disabled]
    assert view["model_efforts"] == before["model_efforts"]
    assert get_available_efforts(
        "ollama-api", active, allowed_backends=inherited, provider=True,
    ) == ["enabled", "off"]
    assert get_available_efforts(
        "ollama-api", disabled, allowed_backends=inherited, provider=True,
    ) == []
    assert blocked not in view["models"]
    assert "must-not-inherit" not in json.dumps(inherited)
    promoted = read_config_json(paths.config_path)
    promoted["global"]["agent_creation"] = {
        "template_agent": "child", "grant_mode": "selected",
    }
    write_config_json(paths.config_path, promoted)
    with pytest.raises(InvalidEffortError):
        service.create(AgentCreationSpec(
            name="denied", backend="her-v3", provider="ollama", model=disabled,
            effort="enabled", is_active=False,
        ))
    assert not (paths.workspaces_root / "denied").exists()
    service.create(AgentCreationSpec(
        name="grandchild", backend="her-v3", provider="ollama", model=active,
        effort="enabled", restricted=True, is_active=False,
    ))
    grandchild = read_config_json(paths.config_path)["agents"][-1]
    assert [row["engine"] for row in grandchild["allowed_backends"]] == ["her-v2"]
    descendants = build_effective_backend_catalogue(
        grandchild["allowed_backends"], raw["global"]["her_providers"]["providers"],
    )["her-v3"]["providers"]["ollama"]
    assert descendants["models"] == view["models"]
    assert descendants["model_efforts"] == view["model_efforts"]
