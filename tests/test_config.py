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
