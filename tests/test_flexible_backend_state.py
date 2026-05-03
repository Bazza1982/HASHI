from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from orchestrator.config import FlexibleAgentConfig, GlobalConfig
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.wrapper_mode import load_wrapper_config


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


def test_update_wrapper_blocks_preserves_managed_state_and_unknown_keys(tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps(
            {
                "active_backend": "codex-cli",
                "agent_mode": "wrapper",
                "active_model": "gpt-5.5",
                "unrelated": {"keep": True},
            }
        ),
        encoding="utf-8",
    )
    manager = _make_manager(workspace)
    manager.agent_mode = "wrapper"

    manager.update_wrapper_blocks(
        core={"backend": "codex-cli", "model": "gpt-5.5"},
        wrapper={"backend": "claude-cli", "model": "claude-haiku-4-5", "context_window": 3},
        wrapper_slots={"1": "Use a warm tone."},
    )

    state = _read_state(workspace)
    assert state["active_backend"] == "codex-cli"
    assert state["agent_mode"] == "wrapper"
    assert state["unrelated"] == {"keep": True}
    assert state["core"] == {"backend": "codex-cli", "model": "gpt-5.5"}
    assert state["wrapper"] == {"backend": "claude-cli", "model": "claude-haiku-4-5", "context_window": 3}
    assert state["wrapper_slots"] == {"1": "Use a warm tone."}


def test_update_wrapper_blocks_removes_stale_active_model_when_override_cleared(tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps({"active_backend": "codex-cli", "agent_mode": "wrapper", "active_model": "gpt-5.5"}),
        encoding="utf-8",
    )
    manager = _make_manager(workspace)
    manager.agent_mode = "wrapper"
    manager._active_model_override = None

    manager.update_wrapper_blocks(wrapper_slots={"1": "Keep facts exact."})

    state = _read_state(workspace)
    assert "active_model" not in state
    assert state["wrapper_slots"] == {"1": "Keep facts exact."}


def test_create_ephemeral_backend_does_not_replace_current_backend(tmp_path, monkeypatch):
    import adapters.registry

    created = []

    class FakeBackend:
        def __init__(self, config, global_config, api_key):
            self.config = config
            self.global_config = global_config
            self.api_key = api_key
            created.append(self)

        async def initialize(self):
            return True

        async def generate_response(self, prompt, request_id, is_retry=False, silent=False, on_stream_event=None):
            return SimpleNamespace(text=f"wrapped:{prompt}", is_success=True, error=None)

        async def shutdown(self):
            self.shutdown_called = True

    monkeypatch.setattr(adapters.registry, "get_backend_class", lambda engine: FakeBackend)
    workspace = tmp_path / "agent"
    manager = _make_manager(workspace)
    manager.current_backend = object()
    original_backend = manager.current_backend
    original_active = manager.config.active_backend

    backend = manager.create_ephemeral_backend("claude-cli", target_model="claude-haiku-4-5")

    assert backend is created[0]
    assert backend.config.engine == "claude-cli"
    assert backend.config.model == "claude-haiku-4-5"
    assert manager.current_backend is original_backend
    assert manager.config.active_backend == original_active


@pytest.mark.asyncio
async def test_generate_ephemeral_response_shuts_down_and_preserves_active_backend(tmp_path, monkeypatch):
    import adapters.registry

    created = []

    class FakeBackend:
        def __init__(self, config, global_config, api_key):
            self.config = config
            self.shutdown_called = False
            created.append(self)

        async def initialize(self):
            return True

        async def generate_response(self, prompt, request_id, is_retry=False, silent=False, on_stream_event=None):
            assert request_id == "req-wrapper"
            assert silent is True
            return SimpleNamespace(text=f"wrapped:{prompt}", is_success=True, error=None)

        async def shutdown(self):
            self.shutdown_called = True

    monkeypatch.setattr(adapters.registry, "get_backend_class", lambda engine: FakeBackend)
    workspace = tmp_path / "agent"
    manager = _make_manager(workspace)
    manager.current_backend = object()
    original_backend = manager.current_backend
    original_active = manager.config.active_backend

    response = await manager.generate_ephemeral_response(
        engine="claude-cli",
        model="claude-haiku-4-5",
        prompt="rewrite me",
        request_id="req-wrapper",
        silent=True,
    )

    assert response.text == "wrapped:rewrite me"
    assert created[0].config.engine == "claude-cli"
    assert created[0].config.model == "claude-haiku-4-5"
    assert created[0].shutdown_called is True
    assert manager.current_backend is original_backend
    assert manager.config.active_backend == original_active


def test_wrapper_config_survives_manager_reload_and_unrelated_state_saves(tmp_path):
    workspace = tmp_path / "agent"
    manager = _make_manager(workspace)
    manager.agent_mode = "wrapper"
    manager.update_wrapper_blocks(
        core={"backend": "codex-cli", "model": "gpt-5.5"},
        wrapper={"backend": "claude-cli", "model": "claude-haiku-4-5", "context_window": 5},
        wrapper_slots={"1": "Be gentle."},
    )

    manager.agent_mode = "fixed"
    manager._save_state()
    manager.agent_mode = "wrapper"
    manager.persist_state(active_model="gpt-5.5")

    reloaded = _make_manager(workspace)
    cfg = load_wrapper_config(reloaded.get_state_snapshot())
    state = reloaded.get_state_snapshot()

    assert cfg.core_backend == "codex-cli"
    assert cfg.core_model == "gpt-5.5"
    assert cfg.wrapper_backend == "claude-cli"
    assert cfg.wrapper_model == "claude-haiku-4-5"
    assert cfg.context_window == 5
    assert state["wrapper_slots"] == {"1": "Be gentle."}
