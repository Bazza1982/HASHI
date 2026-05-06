import json
import logging

from orchestrator.config import ConfigManager


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


def test_missing_agent_type_warns_and_defaults_to_fixed(tmp_path, caplog):
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

    with caplog.at_level(logging.WARNING, logger="BridgeU.Config"):
        _, agents, _ = ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()

    assert agents[0].type == "fixed"
    assert "has no explicit type" in caplog.text
    assert "legacy fixed runtime" in caplog.text


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
