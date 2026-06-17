import json
import logging

import pytest

from orchestrator.config import ConfigManager, LEGACY_FIXED_RUNTIME_ENV


def _write_base_files(tmp_path, agent):
    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {
                    "authorized_id": 0,
                    "base_logs_dir": "logs",
                    "base_media_dir": "media",
                },
                "agents": [agent],
            }
        ),
        encoding="utf-8",
    )
    secrets_path.write_text(json.dumps({"authorized_telegram_id": 0}), encoding="utf-8")
    return config_path, secrets_path


def test_missing_agent_type_is_rejected(tmp_path):
    config_path, secrets_path = _write_base_files(
        tmp_path,
        {
            "name": "legacy",
            "engine": "gemini-cli",
            "workspace_dir": "workspaces/legacy",
            "system_md": "workspaces/legacy/agent.md",
            "model": "gemini-3-flash",
        },
    )

    with pytest.raises(ValueError, match="no explicit type"):
        ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()


def test_explicit_fixed_agent_requires_emergency_flag(tmp_path):
    config_path, secrets_path = _write_base_files(
        tmp_path,
        {
            "name": "legacy",
            "type": "fixed",
            "engine": "gemini-cli",
            "workspace_dir": "workspaces/legacy",
            "system_md": "workspaces/legacy/agent.md",
            "model": "gemini-3-flash",
        },
    )

    with pytest.raises(ValueError, match=LEGACY_FIXED_RUNTIME_ENV):
        ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()


def test_explicit_fixed_agent_can_start_with_emergency_flag(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv(LEGACY_FIXED_RUNTIME_ENV, "1")
    config_path, secrets_path = _write_base_files(
        tmp_path,
        {
            "name": "legacy",
            "type": "fixed",
            "engine": "gemini-cli",
            "workspace_dir": "workspaces/legacy",
            "system_md": "workspaces/legacy/agent.md",
            "model": "gemini-3-flash",
        },
    )

    with caplog.at_level(logging.WARNING, logger="BridgeU.Config"):
        _, agents, _ = ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()

    assert agents[0].type == "fixed"
    assert LEGACY_FIXED_RUNTIME_ENV in caplog.text


def test_explicit_flex_agent_type_does_not_warn(tmp_path, caplog):
    config_path, secrets_path = _write_base_files(
        tmp_path,
        {
            "name": "flexy",
            "type": "flex",
            "workspace_dir": "workspaces/flexy",
            "system_md": "workspaces/flexy/agent.md",
            "allowed_backends": ["gemini-cli", "codex-cli"],
            "active_backend": "gemini-cli",
        },
    )

    with caplog.at_level(logging.WARNING, logger="BridgeU.Config"):
        _, agents, _ = ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()

    assert agents[0].type == "flex"
    assert "has no explicit type" not in caplog.text


def test_global_claw_providers_are_loaded(tmp_path):
    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {
                    "authorized_id": 0,
                    "base_logs_dir": "logs",
                    "base_media_dir": "media",
                    "claw_providers": {
                        "binary_path": "/opt/hashi/bin/claw",
                        "max_permission_mode": "workspace-write",
                        "providers": {
                            "openrouter": {
                                "base_url": "https://openrouter.ai/api/v1",
                                "secret": "openrouter_key",
                            }
                        },
                    },
                },
                "agents": [
                    {
                        "name": "flexy",
                        "type": "flex",
                        "workspace_dir": "workspaces/flexy",
                        "system_md": "workspaces/flexy/agent.md",
                        "allowed_backends": [{"engine": "claw-cli", "model": "deepseek/test"}],
                        "active_backend": "claw-cli",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    secrets_path.write_text(json.dumps({"authorized_telegram_id": 0}), encoding="utf-8")

    global_cfg, _, _ = ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()

    assert global_cfg.claw_providers["binary_path"] == "/opt/hashi/bin/claw"
    assert global_cfg.claw_providers["providers"]["openrouter"]["secret"] == "openrouter_key"


def test_enterprise_scheduler_lease_config_is_loaded(tmp_path):
    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {
                    "authorized_id": 0,
                    "base_logs_dir": "logs",
                    "base_media_dir": "media",
                    "enterprise_database_url": "sqlite:////data/state/enterprise.sqlite",
                    "enterprise_scheduler_lease_enabled": True,
                    "enterprise_scheduler_lease_name": "scheduler-main",
                    "enterprise_scheduler_lease_holder": "pod-a",
                    "enterprise_scheduler_lease_ttl_seconds": 90,
                },
                "agents": [
                    {
                        "name": "flexy",
                        "type": "flex",
                        "workspace_dir": "workspaces/flexy",
                        "system_md": "workspaces/flexy/agent.md",
                        "allowed_backends": ["gemini-cli"],
                        "active_backend": "gemini-cli",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    secrets_path.write_text(json.dumps({"authorized_telegram_id": 0}), encoding="utf-8")

    global_cfg, _, _ = ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()

    assert global_cfg.enterprise_database_url == "sqlite:////data/state/enterprise.sqlite"
    assert global_cfg.enterprise_scheduler_lease_enabled is True
    assert global_cfg.enterprise_scheduler_lease_name == "scheduler-main"
    assert global_cfg.enterprise_scheduler_lease_holder == "pod-a"
    assert global_cfg.enterprise_scheduler_lease_ttl_seconds == 90


def test_enterprise_scheduler_lease_env_overrides_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HASHI_ENTERPRISE_DATABASE_URL", "sqlite:////env/state/enterprise.sqlite")
    monkeypatch.setenv("HASHI_ENTERPRISE_SCHEDULER_LEASE_ENABLED", "1")
    monkeypatch.setenv("HASHI_ENTERPRISE_SCHEDULER_LEASE_NAME", "scheduler-env")
    monkeypatch.setenv("HASHI_ENTERPRISE_SCHEDULER_LEASE_HOLDER", "pod-env")
    monkeypatch.setenv("HASHI_ENTERPRISE_SCHEDULER_LEASE_TTL_SECONDS", "120")
    config_path, secrets_path = _write_base_files(
        tmp_path,
        {
            "name": "flexy",
            "type": "flex",
            "workspace_dir": "workspaces/flexy",
            "system_md": "workspaces/flexy/agent.md",
            "allowed_backends": ["gemini-cli"],
            "active_backend": "gemini-cli",
        },
    )

    global_cfg, _, _ = ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()

    assert global_cfg.enterprise_database_url == "sqlite:////env/state/enterprise.sqlite"
    assert global_cfg.enterprise_scheduler_lease_enabled is True
    assert global_cfg.enterprise_scheduler_lease_name == "scheduler-env"
    assert global_cfg.enterprise_scheduler_lease_holder == "pod-env"
    assert global_cfg.enterprise_scheduler_lease_ttl_seconds == 120
