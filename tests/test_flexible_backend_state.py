from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.base import BackendCapabilities, BackendResponse, BaseBackend
from adapters.timeout_policy import (
    BACKEND_CONFIG_SOURCE,
    HARD_TIMEOUT_KEY,
    IDLE_TIMEOUT_KEY,
    TIMEOUT_POLICY_META_KEY,
    USER_OVERRIDE_SOURCE,
)
from orchestrator.audit_mode import load_audit_config
from orchestrator.config import FlexibleAgentConfig, GlobalConfig
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.privacy_levels import PrivacyLevel
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


def test_stale_persisted_backend_falls_back_to_configured_allowed_backend(
    tmp_path,
    caplog,
):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps(
            {
                "active_backend": "her",
                "active_model": "deepseek-v4-flash",
                "active_provider": "deepseek",
                "agent_mode": "fixed",
                "unrelated": {"keep": True},
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="BackendMgr.test-flex"):
        manager = _make_manager(workspace)

    assert manager.config.active_backend == "codex-cli"
    assert manager._active_model_override is None
    assert manager._active_provider_override is None
    assert manager.agent_mode == "fixed"
    assert "not present in allowed_backends" in caplog.text
    state = _read_state(workspace)
    assert state["active_backend"] == "codex-cli"
    assert "active_model" not in state
    assert "active_provider" not in state
    assert state["unrelated"] == {"keep": True}


def test_allowed_persisted_backend_still_overrides_configured_backend(tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps(
            {
                "active_backend": "claude-cli",
                "active_model": "claude-haiku-4-5",
            }
        ),
        encoding="utf-8",
    )

    manager = _make_manager(workspace)

    assert manager.config.active_backend == "claude-cli"
    assert manager._active_model_override == "claude-haiku-4-5"


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


def test_privacy_defaults_to_level_one_and_persists_level_zero(tmp_path):
    workspace = tmp_path / "agent"
    manager = _make_manager(workspace)

    assert manager.privacy_level is PrivacyLevel.PROVIDER_TRUST

    manager.set_privacy_level(PrivacyLevel.OFF)

    state = _read_state(workspace)
    assert state["privacy_level"] == 0
    reloaded = _make_manager(workspace)
    assert reloaded.privacy_level is PrivacyLevel.OFF


def test_backend_effort_survives_manager_reload(tmp_path):
    workspace = tmp_path / "agent"
    manager = _make_manager(workspace)
    manager.config.allowed_backends[0]["effort"] = "high"

    manager.persist_state()

    state = _read_state(workspace)
    assert state["backend_efforts"] == {"codex-cli": "high"}
    reloaded = _make_manager(workspace)
    assert reloaded.config.allowed_backends[0]["effort"] == "high"


def test_timeout_priority_is_user_then_backend_then_agent(tmp_path):
    workspace = tmp_path / "agent"
    manager = _make_manager(workspace)
    manager.config.extra = {
        "idle_timeout_sec": 600,
        "hard_timeout_sec": 7200,
    }
    backend_cfg = manager.config.allowed_backends[0]
    backend_cfg["process_timeout"] = 1200
    backend_cfg["hard_timeout_sec"] = 8000
    manager.state_store.replace(
        {
            "backend_timeouts": {
                "codex-cli": {
                    "idle_timeout_sec": 1500,
                    "hard_timeout_sec": 9000,
                }
            }
        }
    )

    adapter_cfg = manager._build_adapter_config("codex-cli", backend_cfg)

    assert adapter_cfg.extra[IDLE_TIMEOUT_KEY] == 1500
    assert adapter_cfg.extra[HARD_TIMEOUT_KEY] == 9000
    assert adapter_cfg.extra[TIMEOUT_POLICY_META_KEY]["sources"] == {
        IDLE_TIMEOUT_KEY: USER_OVERRIDE_SOURCE,
        HARD_TIMEOUT_KEY: USER_OVERRIDE_SOURCE,
    }

    manager.state_store.replace({})
    adapter_cfg = manager._build_adapter_config("codex-cli", backend_cfg)
    assert adapter_cfg.extra[IDLE_TIMEOUT_KEY] == 1200
    assert adapter_cfg.extra[HARD_TIMEOUT_KEY] == 8000
    assert adapter_cfg.extra[TIMEOUT_POLICY_META_KEY]["sources"] == {
        IDLE_TIMEOUT_KEY: BACKEND_CONFIG_SOURCE,
        HARD_TIMEOUT_KEY: BACKEND_CONFIG_SOURCE,
    }

    backend_cfg.pop("process_timeout")
    backend_cfg.pop("hard_timeout_sec")
    adapter_cfg = manager._build_adapter_config("codex-cli", backend_cfg)
    assert adapter_cfg.extra[IDLE_TIMEOUT_KEY] == 600
    assert adapter_cfg.extra[HARD_TIMEOUT_KEY] == 7200


@pytest.mark.asyncio
async def test_timeout_override_survives_recreation_and_is_scoped_per_backend(tmp_path, monkeypatch):
    import adapters.registry

    class FakeBackend(BaseBackend):
        DEFAULT_IDLE_TIMEOUT_SEC = 3600
        DEFAULT_HARD_TIMEOUT_SEC = 86400

        def _define_capabilities(self):
            return BackendCapabilities(False, False, False, False, True)

        async def initialize(self):
            return True

        async def generate_response(
            self,
            prompt,
            request_id,
            is_retry=False,
            silent=False,
            on_stream_event=None,
        ):
            return BackendResponse(text=prompt, duration_ms=0)

        async def shutdown(self):
            return None

        async def handle_new_session(self):
            return True

    monkeypatch.setattr(adapters.registry, "get_backend_class", lambda _engine: FakeBackend)
    workspace = tmp_path / "agent"
    manager = _make_manager(workspace)
    assert await manager.initialize_active_backend() is True

    saved = manager.set_active_timeout_override(
        idle_seconds=3600,
        hard_seconds=360000,
    )

    assert saved.idle_seconds == 3600
    assert saved.hard_seconds == 360000
    assert saved.idle_source == USER_OVERRIDE_SOURCE
    assert saved.hard_source == USER_OVERRIDE_SOURCE
    state = _read_state(workspace)
    assert state["backend_timeouts"]["codex-cli"] == {
        IDLE_TIMEOUT_KEY: 3600,
        HARD_TIMEOUT_KEY: 360000,
    }

    reloaded = _make_manager(workspace)
    assert await reloaded.initialize_active_backend() is True
    assert reloaded.get_active_timeout_policy() == saved

    assert await reloaded.switch_backend("claude-cli") is True
    claude_policy = reloaded.get_active_timeout_policy()
    assert claude_policy.hard_seconds == 86400
    reloaded.set_active_timeout_override(idle_seconds=7200, hard_seconds=172800)

    assert await reloaded.switch_backend("codex-cli") is True
    assert reloaded.get_active_timeout_policy().hard_seconds == 360000

    reset = reloaded.clear_active_timeout_override()
    assert reset.idle_seconds == 3600
    assert reset.hard_seconds == 86400
    state = _read_state(workspace)
    assert "codex-cli" not in state["backend_timeouts"]
    assert state["backend_timeouts"]["claude-cli"][HARD_TIMEOUT_KEY] == 172800


def test_provider_prefixed_model_is_normalized_for_adapter_resolution(tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    cfg = FlexibleAgentConfig(
        name="test-flex",
        workspace_dir=workspace,
        system_md=workspace / "AGENT.md",
        telegram_token_key="test-flex",
        allowed_backends=[
            {"engine": "her", "model": "deepseek/default"},
        ],
        active_backend="her",
        project_root=workspace,
    )
    global_cfg = GlobalConfig(
        authorized_id=1,
        base_logs_dir=workspace / "logs",
        base_media_dir=workspace / "media",
        project_root=workspace,
        her_providers={"providers": {"openrouter": {"base_url": "https://example.invalid/v1"}}},
    )
    manager = FlexibleBackendManager(cfg, global_cfg, secrets={"openrouter_key": "secret"})

    adapter_cfg = manager._build_adapter_config(
        "her",
        cfg.allowed_backends[0],
        target_model="openrouter:deepseek/deepseek-v4-flash",
    )

    assert adapter_cfg.model == "deepseek/deepseek-v4-flash"
    assert adapter_cfg.extra["provider"] == "openrouter"


def test_claw_provider_models_are_scoped_by_agent_backend_rows(tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    cfg = FlexibleAgentConfig(
        name="test-flex",
        workspace_dir=workspace,
        system_md=workspace / "AGENT.md",
        telegram_token_key="test-flex",
        allowed_backends=[
            {
                "engine": "her",
                "provider": "openrouter",
                "models": ["deepseek/deepseek-v4-flash", "openai/gpt-4.1-mini"],
            },
            {
                "engine": "her",
                "provider": "deepseek",
                "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
            },
            {
                "engine": "her",
                "provider": "ollama",
                "model": "qwen2.5-coder:32b",
            },
        ],
        active_backend="her",
        project_root=workspace,
    )
    global_cfg = GlobalConfig(
        authorized_id=1,
        base_logs_dir=workspace / "logs",
        base_media_dir=workspace / "media",
        project_root=workspace,
        her_providers={
            "providers": {
                "openrouter": {"base_url": "https://openrouter.invalid/v1"},
                "deepseek": {"base_url": "https://deepseek.invalid/v1"},
                "ollama": {"base_url": "http://localhost:11434/v1", "status": "disabled"},
            }
        },
    )
    manager = FlexibleBackendManager(cfg, global_cfg, secrets={})

    options = {option["name"]: option for option in manager.get_claw_provider_options()}

    assert options["openrouter"]["models"] == [
        "deepseek/deepseek-v4-flash",
        "openai/gpt-4.1-mini",
    ]
    assert options["deepseek"]["models"] == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert options["ollama"]["available"] is False
    assert options["ollama"]["reason"] == "provider is disabled"


def test_generic_claw_model_routes_do_not_leak_between_providers(tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    cfg = FlexibleAgentConfig(
        name="test-flex",
        workspace_dir=workspace,
        system_md=workspace / "AGENT.md",
        telegram_token_key="test-flex",
        allowed_backends=[
            {
                "engine": "her",
                "models": [
                    "openrouter:deepseek/deepseek-v4-flash",
                    "deepseek:deepseek-v4-pro",
                ],
            }
        ],
        active_backend="her",
        project_root=workspace,
    )
    global_cfg = GlobalConfig(
        authorized_id=1,
        base_logs_dir=workspace / "logs",
        base_media_dir=workspace / "media",
        project_root=workspace,
        her_providers={
            "providers": {
                "openrouter": {"base_url": "https://openrouter.invalid/v1"},
                "deepseek": {"base_url": "https://deepseek.invalid/v1"},
            }
        },
    )
    manager = FlexibleBackendManager(cfg, global_cfg, secrets={})

    options = {option["name"]: option for option in manager.get_claw_provider_options()}

    assert options["openrouter"]["models"] == ["deepseek/deepseek-v4-flash"]
    assert options["deepseek"]["models"] == ["deepseek-v4-pro"]


def test_claw_adapter_config_uses_provider_default_model_when_row_only_authorizes_provider(
    tmp_path,
):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    cfg = FlexibleAgentConfig(
        name="test-flex",
        workspace_dir=workspace,
        system_md=workspace / "AGENT.md",
        telegram_token_key="test-flex",
        allowed_backends=[{"engine": "her", "provider": "deepseek"}],
        active_backend="her",
        project_root=workspace,
    )
    global_cfg = GlobalConfig(
        authorized_id=1,
        base_logs_dir=workspace / "logs",
        base_media_dir=workspace / "media",
        project_root=workspace,
        her_providers={
            "providers": {
                "deepseek": {
                    "base_url": "https://deepseek.invalid/v1",
                    "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
                    "default_model": "deepseek-v4-pro",
                }
            }
        },
    )
    manager = FlexibleBackendManager(cfg, global_cfg, secrets={})

    adapter_cfg = manager._build_adapter_config("her", cfg.allowed_backends[0])

    assert adapter_cfg.extra["provider"] == "deepseek"
    assert adapter_cfg.model == "deepseek-v4-pro"


def test_claw_provider_and_model_persist_as_separate_state(tmp_path):
    workspace = tmp_path / "agent"
    manager = _make_manager(workspace)
    manager.config.active_backend = "her"
    manager.persist_state(
        active_provider="deepseek",
        active_model="deepseek-v4-flash",
    )

    state = _read_state(workspace)

    assert state["active_backend"] == "her"
    assert state["active_provider"] == "deepseek"
    assert state["active_model"] == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_failed_her_alias_switch_never_rolls_back_to_retired_her(tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    cfg = FlexibleAgentConfig(
        name="test-flex",
        workspace_dir=workspace,
        system_md=workspace / "AGENT.md",
        telegram_token_key="test-flex",
        allowed_backends=[
            {
                "engine": "her-v2",
                "model": "role-configured",
                "her_v2": {"profiles": {"configured": {}}},
            },
            {
                "engine": "codex-cli",
                "model": "gpt-test",
            },
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
    manager = FlexibleBackendManager(cfg, global_cfg, secrets={})
    manager.current_backend = SimpleNamespace(
        config=SimpleNamespace(
            model="gpt-test",
            extra={},
        )
    )
    initialize_calls: list[tuple[str, str | None, str | None]] = []

    async def fake_shutdown():
        manager.current_backend = None

    async def fake_initialize(target_model=None, target_provider=None):
        initialize_calls.append(
            (manager.config.active_backend, target_model, target_provider)
        )
        return len(initialize_calls) > 1

    manager.shutdown = fake_shutdown
    manager.initialize_active_backend = fake_initialize

    switched = await manager.switch_backend(
        "her",
        target_model="role-configured",
    )

    assert switched is False
    assert initialize_calls == [
        ("her-v2", "role-configured", None),
        ("codex-cli", "gpt-test", None),
    ]
    assert manager.config.active_backend == "codex-cli"
    assert manager._active_provider_override is None
    assert manager._active_model_override == "gpt-test"
    state = _read_state(workspace)
    assert state["active_backend"] == "codex-cli"
    assert "active_provider" not in state
    assert state["active_model"] == "gpt-test"


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


def test_update_audit_blocks_preserves_managed_state_and_unknown_keys(tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps(
            {
                "active_backend": "codex-cli",
                "agent_mode": "audit",
                "active_model": "gpt-5.5",
                "unrelated": {"keep": True},
            }
        ),
        encoding="utf-8",
    )
    manager = _make_manager(workspace)
    manager.agent_mode = "audit"

    manager.update_audit_blocks(
        core={"backend": "codex-cli", "model": "gpt-5.5"},
        audit={"backend": "claude-cli", "model": "claude-sonnet-4-6", "delivery": "issues_only"},
        audit_criteria={"1": "Flag approval bypass."},
    )

    state = _read_state(workspace)
    assert state["active_backend"] == "codex-cli"
    assert state["agent_mode"] == "audit"
    assert state["unrelated"] == {"keep": True}
    assert state["core"] == {"backend": "codex-cli", "model": "gpt-5.5"}
    assert state["audit"] == {"backend": "claude-cli", "model": "claude-sonnet-4-6", "delivery": "issues_only"}
    assert state["audit_criteria"] == {"1": "Flag approval bypass."}

    cfg = load_audit_config(state)
    assert cfg.audit_backend == "claude-cli"
    assert cfg.audit_model == "claude-sonnet-4-6"


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
    manager.state_store.replace(
        {
            "backend_timeouts": {
                "claude-cli": {
                    IDLE_TIMEOUT_KEY: 3600,
                    HARD_TIMEOUT_KEY: 360000,
                }
            }
        }
    )
    manager.current_backend = object()
    original_backend = manager.current_backend
    original_active = manager.config.active_backend

    backend = manager.create_ephemeral_backend("claude-cli", target_model="claude-haiku-4-5")

    assert backend is created[0]
    assert backend.config.engine == "claude-cli"
    assert backend.config.model == "claude-haiku-4-5"
    assert IDLE_TIMEOUT_KEY not in backend.config.extra
    assert HARD_TIMEOUT_KEY not in backend.config.extra
    assert manager.current_backend is original_backend
    assert manager.config.active_backend == original_active


def test_ephemeral_her_v2_backend_cannot_enable_habit_learning(tmp_path, monkeypatch):
    import adapters.registry

    class FakeBackend:
        def __init__(self, config, global_config, api_key):
            self.config = config

    monkeypatch.setattr(adapters.registry, "get_backend_class", lambda engine: FakeBackend)
    workspace = tmp_path / "agent"
    manager = _make_manager(workspace)
    manager.config.allowed_backends.append(
        {
            "engine": "her-v2",
            "model": "role-configured",
            "habit_meditation": {"enabled": True},
        }
    )

    backend = manager.create_ephemeral_backend(
        "her", target_model="role-configured"
    )

    assert backend.config.extra["habit_meditation"]["enabled"] is False
    assert backend.config.extra["habit_learning_eligible"] is False
    assert backend.config.extra["ephemeral_session"] is True


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


@pytest.mark.asyncio
async def test_generate_tool_free_ephemeral_response_clears_registry_and_shuts_down(
    tmp_path,
    monkeypatch,
):
    import adapters.registry

    created = []

    class FakeBackend:
        def __init__(self, config, global_config, api_key):
            self.config = config
            self.tool_registry = object()
            self.shutdown_called = False
            created.append(self)

        async def initialize(self):
            assert self.tool_registry is None
            return True

        async def generate_response(
            self,
            prompt,
            request_id,
            is_retry=False,
            silent=False,
            on_stream_event=None,
        ):
            assert self.tool_registry is None
            return SimpleNamespace(text=prompt, is_success=True, error=None)

        async def shutdown(self):
            self.shutdown_called = True

    monkeypatch.setattr(adapters.registry, "get_backend_class", lambda engine: FakeBackend)
    workspace = tmp_path / "agent"
    manager = _make_manager(workspace)
    manager.config.allowed_backends.append(
        {"engine": "openrouter-api", "model": "vendor/model"}
    )

    response = await manager.generate_tool_free_ephemeral_response(
        engine="openrouter-api",
        model="vendor/model",
        prompt="render Persona status",
        request_id="req-persona",
    )

    assert response.text == "render Persona status"
    assert created[0].tool_registry is None
    assert created[0].shutdown_called is True


@pytest.mark.asyncio
async def test_generate_tool_free_ephemeral_response_rejects_cli_backend(tmp_path):
    manager = _make_manager(tmp_path / "agent")

    with pytest.raises(ValueError, match="not a tool-free API renderer"):
        await manager.generate_tool_free_ephemeral_response(
            engine="codex-cli",
            model="gpt-5.4",
            prompt="render Persona status",
            request_id="req-persona",
        )


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


def test_flexible_runtime_persists_her_backend_events(tmp_path):
    agent_name = "habit-log-regression"
    runtime_loggers = [
        logging.getLogger(f"FlexRuntime.{agent_name}"),
        logging.getLogger(f"FlexRuntime.{agent_name}.telegram"),
        logging.getLogger(f"FlexRuntime.{agent_name}.messages"),
        logging.getLogger(f"FlexRuntime.{agent_name}.errors"),
        logging.getLogger(f"FlexRuntime.{agent_name}.maintenance"),
    ]
    backend_logger = logging.getLogger(f"Backend.HER.{agent_name}")
    all_loggers = [*runtime_loggers, backend_logger]
    for logger in all_loggers:
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)
        logger.propagate = True

    runtime = SimpleNamespace(
        name=agent_name,
        session_dir=tmp_path,
        logger=runtime_loggers[0],
        telegram_logger=runtime_loggers[1],
        message_logger=runtime_loggers[2],
        error_logger=runtime_loggers[3],
        maintenance_logger=runtime_loggers[4],
    )
    messages = [
        "HER Habit planning: request=req-0001 matched=1 ids=seeded effort=low",
        "HER Habit Meditation queued: request=req-0001 job=job-0001",
        "HER Habit Meditation completed: job=job-0001 actions=0 outcomes=no-change",
    ]
    try:
        FlexibleAgentRuntime._setup_logging(runtime)
        for message in messages:
            backend_logger.info(message)
        for handler in runtime.logger.handlers:
            handler.flush()

        events = (tmp_path / "events.log").read_text(encoding="utf-8")
        assert all(events.count(message) == 1 for message in messages)
    finally:
        handlers = {
            id(handler): handler
            for logger in all_loggers
            for handler in logger.handlers
        }
        for logger in all_loggers:
            logger.handlers.clear()
            logger.setLevel(logging.NOTSET)
            logger.propagate = True
        for handler in handlers.values():
            handler.close()
