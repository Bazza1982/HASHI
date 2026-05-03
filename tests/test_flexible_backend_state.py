from __future__ import annotations

import json
from pathlib import Path

from orchestrator.config import FlexibleAgentConfig, GlobalConfig
from orchestrator.flexible_backend_manager import FlexibleBackendManager


def _make_manager(workspace: Path) -> FlexibleBackendManager:
    workspace.mkdir(parents=True, exist_ok=True)
    cfg = FlexibleAgentConfig(
        name="test-flex",
        workspace_dir=workspace,
        system_md=workspace / "AGENT.md",
        telegram_token_key="test-flex",
        allowed_backends=[
            {"engine": "codex-cli", "model": "gpt-5.4"},
            {"engine": "claude-cli", "model": "claude-haiku-4-5"},
        ],
        active_backend="codex-cli",
        project_root=workspace,
    )
    global_cfg = GlobalConfig(
        authorized_id=1,
        base_logs_dir=workspace / "logs",
        base_media_dir=workspace / "media",
        project_root=workspace,
    )
    return FlexibleBackendManager(cfg, global_cfg, secrets={})


def _read_state(workspace: Path) -> dict:
    return json.loads((workspace / "state.json").read_text(encoding="utf-8"))


def test_save_state_preserves_unknown_keys(tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps(
            {
                "active_backend": "claude-cli",
                "agent_mode": "flex",
                "core": {"backend": "codex-cli", "model": "gpt-5.5"},
                "wrapper": {"backend": "claude-cli", "model": "claude-haiku-4-5"},
                "wrapper_slots": {"1": "Preserve facts."},
            }
        ),
        encoding="utf-8",
    )
    manager = _make_manager(workspace)

    manager.config.active_backend = "codex-cli"
    manager.agent_mode = "fixed"
    manager._save_state()

    state = _read_state(workspace)
    assert state["active_backend"] == "codex-cli"
    assert state["agent_mode"] == "fixed"
    assert state["core"] == {"backend": "codex-cli", "model": "gpt-5.5"}
    assert state["wrapper"] == {"backend": "claude-cli", "model": "claude-haiku-4-5"}
    assert state["wrapper_slots"] == {"1": "Preserve facts."}


def test_save_state_removes_stale_active_model_when_override_cleared(tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps(
            {
                "active_backend": "codex-cli",
                "agent_mode": "flex",
                "active_model": "gpt-5.5",
                "wrapper": {"backend": "claude-cli"},
            }
        ),
        encoding="utf-8",
    )
    manager = _make_manager(workspace)
    manager._active_model_override = None

    manager._save_state()

    state = _read_state(workspace)
    assert "active_model" not in state
    assert state["wrapper"] == {"backend": "claude-cli"}


def test_save_state_writes_active_model_when_override_exists(tmp_path):
    workspace = tmp_path / "agent"
    manager = _make_manager(workspace)

    manager.persist_state(active_model="gpt-5.5")

    state = _read_state(workspace)
    assert state["active_backend"] == "codex-cli"
    assert state["agent_mode"] == "flex"
    assert state["active_model"] == "gpt-5.5"


def test_save_state_recovers_from_invalid_existing_json(tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    (workspace / "state.json").write_text("{invalid json", encoding="utf-8")
    manager = _make_manager(workspace)

    manager._save_state()

    state = _read_state(workspace)
    assert state["active_backend"] == "codex-cli"
    assert state["agent_mode"] == "flex"
