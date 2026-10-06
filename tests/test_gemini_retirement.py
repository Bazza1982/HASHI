"""Retirement must block executable routes without erasing historical facts."""

import json
from types import SimpleNamespace

import pytest

from adapters.registry import get_backend_class, packaged_backend_engines
from orchestrator.api_gateway_preflight import check_gateway_engine
from orchestrator.backend_preflight import BackendPreflight
from orchestrator.config import ConfigManager
from orchestrator.flexible_backend_registry import (
    get_available_models,
    get_gateway_engine_for_model,
    is_cli_backend,
    is_selectable_backend,
    normalize_allowed_backends,
    public_backend_engine,
)
from orchestrator.runtime_delivery import _backend_runtime_name
from tests.test_workbench_backend_catalogue import _server


@pytest.mark.asyncio
async def test_public_catalogue_retires_client_and_keeps_antigravity_models(tmp_path):
    response = await _server(tmp_path).handle_backend_catalogue(object())
    backends = json.loads(response.text)["backends"]
    assert "gemini-cli" not in backends
    assert "gemini-3.8-flash-high" in backends["antigravity-cli"]["models"]
    assert get_gateway_engine_for_model("gemini-3.8-flash-high") == "antigravity-cli"
    assert get_available_models("gemini-cli") == []
    assert is_selectable_backend("gemini-cli") is False
    assert is_cli_backend("gemini-cli") is False


def test_retired_adapter_cannot_be_packaged_or_instantiated():
    assert "gemini-cli" not in packaged_backend_engines()
    with pytest.raises(ValueError, match="gemini-cli.*removed.*antigravity-cli"):
        get_backend_class("gemini-cli")


@pytest.mark.parametrize("row", ["gemini-cli", {"engine": "gemini-cli", "model": "gemini-2.5-flash"}])
def test_retired_permission_is_rejected_instead_of_silently_changing_model(row):
    with pytest.raises(ValueError, match="gemini-cli.*removed.*antigravity-cli"):
        normalize_allowed_backends([row])


@pytest.mark.parametrize("fragment", [
    {"type": "flex", "allowed_backends": ["gemini-cli"], "active_backend": "gemini-cli"},
    {"type": "flex", "allowed_backends": ["antigravity-cli"], "active_backend": "gemini-cli"},
    {"type": "fixed", "engine": "gemini-cli", "model": "gemini-2.5-flash"},
])
def test_legacy_configuration_rejects_client_before_any_migration_write(tmp_path, fragment):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"global": {"authorized_id": 0}, "agents": [
        {"name": "legacy", "workspace_dir": "workspaces/legacy", **fragment}
    ]}), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="gemini-cli.*removed.*antigravity-cli"):
        ConfigManager(config_path=path, secrets_path=tmp_path / "secrets.json", code_root=tmp_path).load()
    assert path.read_bytes() == before
    assert not (tmp_path / "workspaces/legacy").exists()


def test_preflights_refuse_retired_client_even_when_its_executable_is_installed(monkeypatch):
    probes = []

    def installed(command):
        probes.append(command)
        return "installed-client"

    monkeypatch.setattr("shutil.which", installed)
    cfg = SimpleNamespace(gemini_cmd="old-gemini", claude_cmd="claude", codex_cmd="codex", agy_cmd="agy")
    agent = SimpleNamespace(active_backend="gemini-cli", allowed_backends=[{"engine": "gemini-cli"}])
    available, reason = BackendPreflight().check_backend_availability(cfg, [agent], {})["gemini-cli"]
    assert available is False
    assert "antigravity-cli" in reason
    status = check_gateway_engine(cfg, {}, "gemini-cli")
    assert status["available"] is False
    assert "antigravity-cli" in status["reason"]
    assert "old-gemini" not in probes


def test_historical_engine_identity_is_preserved_for_old_messages_and_receipts():
    assert public_backend_engine("gemini-cli") == "gemini-cli"
    assert _backend_runtime_name("gemini-cli") == "Gemini CLI"


def test_first_run_detects_antigravity_instead_of_the_installed_retired_client(monkeypatch):
    from tui import onboarding

    probes = []

    def installed(command):
        probes.append(command)
        return command if command in {"agy", "gemini"} else None

    monkeypatch.setattr(onboarding.shutil, "which", installed)
    monkeypatch.setattr(onboarding.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr("orchestrator.pathing.resolve_agy_executable", lambda *args: "agy")
    assert onboarding.audit_environment() == ("Antigravity CLI", "antigravity-cli")
    assert "gemini" not in probes


def test_first_run_refuses_retired_engine_before_creating_files(tmp_path):
    from tui.onboarding import write_config

    with pytest.raises(ValueError, match="gemini-cli.*removed.*antigravity-cli"):
        write_config(tmp_path, "gemini-cli", {}, "en")
    assert list(tmp_path.iterdir()) == []


def test_first_run_antigravity_configuration_uses_its_own_model(tmp_path):
    from tui.onboarding import write_config

    write_config(tmp_path, "antigravity-cli", {}, "en")
    config = json.loads((tmp_path / "agents.json").read_text(encoding="utf-8"))["agents"][0]
    assert config["active_backend"] == "antigravity-cli"
    assert config["model"] == "gemini-3.8-flash-high"
    assert "gemini-cli" not in {row["engine"] for row in config["allowed_backends"]}
