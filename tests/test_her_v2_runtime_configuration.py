from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator.config import FlexibleAgentConfig, GlobalConfig
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.her_v2.models import Stage


def _her_v2_config() -> dict:
    return {
        "profiles": {
            "lightweight": {
                "engine": "deepseek-api",
                "model": "deepseek-v4-flash",
                "reasoning": "high",
            },
            "triage": {
                "engine": "deepseek-api",
                "model": "deepseek-v4-flash",
                "reasoning": "high",
            },
            "premium": {
                "engine": "deepseek-api",
                "model": "deepseek-v4-pro",
                "reasoning": "high",
            },
            "reviewer": {
                "engine": "deepseek-api",
                "model": "deepseek-v4-pro",
                "reasoning": "max",
            },
            "orchestrator": {
                "engine": "deepseek-api",
                "model": "deepseek-v4-pro",
                "reasoning": "max",
            },
        }
    }


def _manager(tmp_path, *, state: dict | None = None) -> FlexibleBackendManager:
    workspace = tmp_path / "agent"
    workspace.mkdir(parents=True, exist_ok=True)
    if state is not None:
        (workspace / "state.json").write_text(json.dumps(state), encoding="utf-8")
    config = FlexibleAgentConfig(
        name="her-agent",
        workspace_dir=workspace,
        system_md=workspace / "AGENT.md",
        telegram_token_key="her-agent",
        allowed_backends=[
            {
                "engine": "deepseek-api",
                "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
                "default_model": "deepseek-v4-flash",
            },
            {
                "engine": "openrouter-api",
                "models": [
                    "deepseek/deepseek-v4-flash",
                    "anthropic/claude-sonnet-4.6",
                ],
                "default_model": "deepseek/deepseek-v4-flash",
            },
            {
                "engine": "her-v2",
                "model": "role-configured",
                "effort": "high",
                "her_v2": _her_v2_config(),
            },
        ],
        active_backend="her-v2",
        project_root=workspace,
    )
    global_config = GlobalConfig(
        authorized_id=1,
        base_logs_dir=workspace / "logs",
        base_media_dir=workspace / "media",
        project_root=workspace,
        her_providers={
            "providers": {
                "deepseek": {"engine": "deepseek-api", "status": "stable"},
                "openrouter": {"engine": "openrouter-api", "status": "stable"},
                "ollama": {"engine": "ollama-api", "status": "disabled"},
            }
        },
    )
    return FlexibleBackendManager(config, global_config, secrets={})


def test_her_v2_provider_options_are_concrete_call_providers(tmp_path):
    manager = _manager(tmp_path)
    manager.config.allowed_backends[0]["models"].append("role-configured")

    options = {item["engine"]: item for item in manager.get_her_v2_provider_options()}

    assert options["deepseek-api"] == {
        "name": "deepseek",
        "engine": "deepseek-api",
        "label": "deepseek",
        "status": "stable",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
        "fast_model": "deepseek-v4-flash",
        "pro_model": "deepseek-v4-pro",
        "available": True,
        "reason": None,
    }
    assert options["openrouter-api"]["available"] is True
    assert options["openrouter-api"]["fast_model"] == "deepseek/deepseek-v4-flash"
    assert options["openrouter-api"]["pro_model"] == "anthropic/claude-sonnet-4.6"
    assert options["ollama-api"]["available"] is False
    assert options["ollama-api"]["reason"] == "provider is disabled"
    assert all("role-configured" not in item["models"] for item in options.values())


def test_provider_switch_prepares_both_slots_and_preserves_reasoning(tmp_path):
    manager = _manager(tmp_path)
    before = manager.get_her_v2_configuration()

    candidate = manager.prepare_her_v2_provider("OpenRouter")

    assert candidate.provider == "openrouter-api"
    assert candidate.fast_model == "deepseek/deepseek-v4-flash"
    assert candidate.pro_model == "anthropic/claude-sonnet-4.6"
    assert candidate.profile_reasoning == before.profile_reasoning
    assert candidate.stage_reasoning == before.stage_reasoning


def test_one_model_provider_assigns_the_same_model_to_both_slots(tmp_path):
    manager = _manager(tmp_path)
    manager.config.allowed_backends.insert(
        0,
        {
            "engine": "xai-api",
            "models": ["grok-4.5"],
            "default_model": "grok-4.5",
        },
    )
    manager.global_config.her_providers["providers"]["xai"] = {
        "engine": "xai-api",
        "status": "stable",
    }

    selected = manager.prepare_her_v2_provider("xai")

    assert selected.fast_model == "grok-4.5"
    assert selected.pro_model == "grok-4.5"


def test_reselecting_active_provider_preserves_valid_slot_choices(tmp_path):
    manager = _manager(tmp_path)
    selected = manager.prepare_her_v2_provider("openrouter")
    selected = manager.prepare_her_v2_model(
        "fast",
        "anthropic/claude-sonnet-4.6",
        current=selected,
    )

    reselected = manager.prepare_her_v2_provider(
        "openrouter",
        current=selected,
    )

    assert reselected.fast_model == "anthropic/claude-sonnet-4.6"
    assert reselected.pro_model == "anthropic/claude-sonnet-4.6"


def test_slot_model_and_reasoning_updates_are_independent(tmp_path):
    manager = _manager(tmp_path)
    current = manager.get_her_v2_configuration()

    model_candidate = manager.prepare_her_v2_model(
        "fast", "deepseek-v4-pro", current=current
    )
    reasoning_candidate = manager.prepare_her_v2_reasoning(
        "review", "low", current=model_candidate
    )

    assert reasoning_candidate.fast_model == "deepseek-v4-pro"
    assert reasoning_candidate.pro_model == "deepseek-v4-pro"
    assert reasoning_candidate.stage_reasoning == {"review": "low"}
    assert reasoning_candidate.profile_reasoning == current.profile_reasoning
    assert reasoning_candidate.provider == "deepseek-api"


def test_apply_configuration_persists_and_refreshes_live_adapter_atomically(tmp_path):
    manager = _manager(tmp_path)
    manager.current_backend = SimpleNamespace(
        config=SimpleNamespace(
            engine="her-v2",
            model="role-configured",
            extra={"her_v2": _her_v2_config()},
        ),
        _v2_config=None,
        effort="high",
    )
    candidate = manager.prepare_her_v2_reasoning("fast", "medium")

    manager.apply_her_v2_configuration(candidate)

    state = json.loads(manager.state_file.read_text(encoding="utf-8"))
    assert state["her_v2_configuration"]["profile_reasoning"] == {
        "lightweight": "medium",
        "triage": "medium",
        "premium": "high",
        "reviewer": "max",
        "orchestrator": "max",
    }
    assert state["backend_efforts"] == {"her-v2": "high"}
    assert "provider_reasoning" not in state
    assert manager.current_backend.effort == "high"
    assert (
        manager.current_backend._v2_config.profile_for(Stage.TRIAGE).reasoning
        == "medium"
    )


def test_legacy_single_route_state_migrates_without_exposing_role_configured(tmp_path):
    manager = _manager(
        tmp_path,
        state={
            "active_backend": "her",
            "active_provider": "deepseek",
            "active_model": "deepseek-v4-flash",
            "agent_mode": "flex",
            "privacy_level": 1,
        },
    )

    state = json.loads(manager.state_file.read_text(encoding="utf-8"))
    selected = manager.get_her_v2_configuration()

    assert state["active_backend"] == "her-v2"
    assert "active_provider" not in state
    assert "active_model" not in state
    assert selected.provider == "deepseek-api"
    assert selected.fast_model == "deepseek-v4-flash"
    assert selected.pro_model == "deepseek-v4-flash"


def test_invalid_model_does_not_write_runtime_state(tmp_path):
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match="not allowed"):
        manager.prepare_her_v2_model("pro", "ungranted/model")

    assert not manager.state_file.exists()


def test_persisted_selection_is_applied_when_adapter_config_is_rebuilt(tmp_path):
    manager = _manager(tmp_path)
    manager.apply_her_v2_configuration(manager.prepare_her_v2_provider("openrouter"))
    reloaded = _manager(tmp_path)
    her_row = next(
        row for row in reloaded.config.allowed_backends if row["engine"] == "her-v2"
    )

    adapter_config = reloaded._build_adapter_config("her-v2", her_row)
    selected = reloaded.get_her_v2_configuration()
    profiles = adapter_config.extra["her_v2"]["profiles"]

    assert selected.provider == "openrouter-api"
    assert {profile["engine"] for profile in profiles.values()} == {"openrouter-api"}
    assert profiles["lightweight"]["model"] == "deepseek/deepseek-v4-flash"
    assert profiles["premium"]["model"] == "anthropic/claude-sonnet-4.6"


def test_persistence_failure_keeps_previous_live_configuration(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manager.current_backend = SimpleNamespace(
        config=SimpleNamespace(
            engine="her-v2",
            model="role-configured",
            extra={"her_v2": _her_v2_config()},
        ),
        _v2_config=None,
        effort="high",
    )
    before = manager.get_her_v2_configuration()
    candidate = manager.prepare_her_v2_provider("openrouter")

    def fail_replace(_state):
        raise OSError("disk unavailable")

    monkeypatch.setattr(manager.state_store, "replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        manager.apply_her_v2_configuration(candidate)

    assert manager.get_her_v2_configuration() == before
    assert manager.current_backend._v2_config is None
    assert manager.current_backend.config.extra["her_v2"] == _her_v2_config()


def test_state_read_failure_cannot_overwrite_or_activate_candidate(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    before = manager.get_her_v2_configuration()
    candidate = manager.prepare_her_v2_provider("openrouter")
    replace_calls = []

    def fail_read():
        raise OSError("state unreadable")

    monkeypatch.setattr(manager.state_store, "read", fail_read)
    monkeypatch.setattr(manager.state_store, "replace", replace_calls.append)

    with pytest.raises(OSError, match="state unreadable"):
        manager.apply_her_v2_configuration(candidate)

    assert replace_calls == []
    assert manager.get_her_v2_configuration() == before
