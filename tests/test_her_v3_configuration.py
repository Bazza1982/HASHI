from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters.her_v2_provider import HashiStageProvider
from orchestrator.config import FlexibleAgentConfig, GlobalConfig
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.her_v2.config import HERv2Config
from orchestrator.her_v2.v3_config import (
    HER_V3_CONFIGURATION_STATE_KEY,
    HERv3ModelTarget,
    apply_v3_target,
    normalise_v3_config,
    resolve_v3_target,
)
from orchestrator.privacy_levels import PrivacyLevel, PrivacyPolicyError


def _raw_config() -> dict:
    return {
        "main": {
            "provider": "deepseek-api",
            "model": "deepseek-v4-pro",
        },
        "route_targets": {
            "execution_complex": {
                "provider": "hashi-api",
                "model": "old-route-model",
            }
        },
        "slot_models": {"pro": "old-route-model"},
    }


def _manager(tmp_path) -> FlexibleBackendManager:
    workspace = tmp_path / "agent"
    workspace.mkdir()
    config = FlexibleAgentConfig(
        name="v3-agent",
        workspace_dir=workspace,
        system_md=workspace / "agent.md",
        telegram_token_key="v3-agent",
        active_backend="her-v2",
        allowed_backends=[
            {
                "engine": "her-v2",
                "model": "role-configured",
                "effort": "high",
                "her_v2": _raw_config(),
            },
            {
                "engine": "deepseek-api",
                "models": ["deepseek-flash", "deepseek-v4-pro"],
            },
        ],
        project_root=workspace,
    )
    global_config = GlobalConfig(
        authorized_id=1,
        base_logs_dir=workspace / "logs",
        base_media_dir=workspace / "media",
        project_root=workspace,
        her_providers={
            "providers": {
                "deepseek": {
                    "engine": "deepseek-api",
                    "status": "stable",
                }
            }
        },
    )
    return FlexibleBackendManager(config, global_config, secrets={})


def test_v3_target_replaces_old_route_matrix_with_one_main_model():
    target = HERv3ModelTarget("deepseek-api", "deepseek-flash")

    effective = apply_v3_target(_raw_config(), target)

    assert resolve_v3_target(effective) == target
    assert effective["profiles"]["main"]["model"] == "deepseek-flash"
    assert effective["profiles"]["auxiliary"]["model"] == "deepseek-flash"
    assert "route_targets" not in effective
    assert "slot_models" not in effective
    assert effective["routing_mode"] == "single"


def test_request_local_herv3_target_reaches_effective_adapter_config(
    tmp_path,
    monkeypatch,
):
    manager = _manager(tmp_path)
    backend_cfg = manager.config.allowed_backends[0]
    backend_cfg["her_v2"]["auxiliary"] = {
        "provider": "hashi-api",
        "model": "unauthorised-auxiliary-model",
    }
    monkeypatch.setattr(
        manager,
        "get_her_v3_target",
        Mock(side_effect=ValueError("stale persisted target")),
    )

    adapter_cfg = manager._build_adapter_config(
        "her-v2",
        backend_cfg,
        target_provider="deepseek-api",
        target_model="deepseek-flash",
    )

    effective = HERv2Config.from_mapping(adapter_cfg.extra["her_v2"])
    assert {
        (profile.engine, profile.model)
        for profile in effective.all_provider_profiles()
    } == {("deepseek-api", "deepseek-flash")}


def test_v3_deepseek_allowlist_overrides_legacy_openai_profiles(tmp_path):
    manager = _manager(tmp_path)
    config = manager.config.allowed_backends[0]["her_v2"]
    config.update({
        "profiles": {"premium": {"engine": "hashi-api", "model": "gpt-5.6-sol"}},
        "main": {"provider": "deepseek-api", "model": "deepseek-flash", "reasoning": "high"},
        "auxiliary": {"provider": "deepseek-api", "model": "deepseek-flash", "reasoning": "high"},
        "v3_provider_allowlist": ["deepseek-api"],
    })
    manager.config.allowed_backends.append({"engine": "hashi-api", "model": "gpt-5.6-sol"})

    assert resolve_v3_target(config) == HERv3ModelTarget("deepseek-api", "deepseek-flash")
    assert {item["engine"] for item in manager.get_her_v3_provider_options()} == {"deepseek-api"}
    assert manager.get_her_v3_target().model == "deepseek-flash"
    with pytest.raises(ValueError, match="allowlist"):
        normalise_v3_config({**config, "main": {"provider": "hashi-api", "model": "gpt-5.6-sol"}})


def test_v3_repairs_persisted_effort_incompatible_with_deepseek(tmp_path):
    manager = _manager(tmp_path)
    manager.state_store.update(lambda state: {
        **state, "backend_efforts": {"her-v2": "medium"},
    })

    manager._load_state()

    backend = manager.config.allowed_backends[0]
    assert backend["effort"] == "high"
    assert manager.state_store.read()["backend_efforts"]["her-v2"] == "high"


@pytest.mark.parametrize(
    "provider,engine,public_provider,initial_model,selected_model",
    [
        ("ollama", "ollama-api", "ollama", "local-gemma:31b", "local-qwen:27b"),
        ("ollama-api", "ollama-api", "ollama", "local-gemma:31b", "local-qwen:27b"),
        ("deepseek", "deepseek-api", "deepseek", "deepseek-v4-pro", "deepseek-flash"),
        ("deepseek-api", "deepseek-api", "deepseek", "deepseek-v4-pro", "deepseek-flash"),
    ],
)
def test_v3_manager_persists_and_live_refreshes_model_target(
    tmp_path, provider, engine, public_provider, initial_model, selected_model
):
    manager = _manager(tmp_path)
    raw = {
        "main": {"provider": provider, "model": initial_model},
        "v3_provider_allowlist": [provider],
    }
    manager.config.allowed_backends[0]["her_v2"] = raw
    manager.config.allowed_backends.append({
        "engine": engine, "models": [initial_model, selected_model],
    })
    manager.global_config.her_providers["providers"][public_provider] = {
        "engine": engine,
    }
    live_config = SimpleNamespace(engine="her-v2", extra={"her_v2": raw})
    manager.current_backend = SimpleNamespace(config=live_config, _v2_config=None)

    target = manager.prepare_her_v3_model(selected_model)
    manager.apply_her_v3_target(target)

    assert manager.get_her_v3_target() == target
    profiles = manager.current_backend._v2_config.profiles
    assert {(p.engine, p.model) for p in profiles.values()} == {(engine, selected_model)}
    assert live_config.extra["her_v2"]["main"]["provider"] == public_provider
    state = json.loads(manager.state_file.read_text(encoding="utf-8"))
    assert state[HER_V3_CONFIGURATION_STATE_KEY] == {
        "provider": public_provider, "model": selected_model,
    }
    assert "her_v2_configuration" not in state
    reloaded = _manager_reloaded(manager)
    assert reloaded.get_her_v3_target() == target
    assert reloaded._her_v3_provider_option(public_provider)["engine"] == engine


@pytest.mark.parametrize("previous,expected", [("off", "high"), ("low", "low")])
def test_v3_target_switch_adopts_supported_reasoning_in_live_and_durable_state(tmp_path, previous, expected):
    manager = _manager(tmp_path)
    manager.config.allowed_backends.append({"engine": "hashi-api", "model": "gpt-5.6-luna"})
    manager.config.allowed_backends[0]["effort"] = previous
    manager.current_backend = SimpleNamespace(
        config=SimpleNamespace(engine="her-v2", extra={"her_v2": _raw_config(), "effort": previous}),
        effort="zero" if previous == "off" else previous, _v2_config=None,
    )
    manager.apply_her_v3_target(HERv3ModelTarget("hashi-api", "gpt-5.6-luna"))
    assert manager.config.allowed_backends[0]["effort"] == expected
    assert manager.current_backend.effort == expected
    assert manager.current_backend.config.extra["effort"] == expected
    assert manager.state_store.read()["backend_efforts"]["her-v2"] == expected


def test_v3_provider_options_derive_deepseek_models_from_provider_catalogue(tmp_path):
    manager = _manager(tmp_path)

    deepseek = manager._her_v3_provider_option("deepseek")

    assert deepseek is not None
    assert deepseek["available"] is True
    assert deepseek["models"] == ["deepseek-flash", "deepseek-v4-pro"]


@pytest.mark.parametrize("available", [["local-qwen:27b", "local-gemma:31b"], []])
def test_v3_provider_choices_respect_instance_model_restrictions(tmp_path, available):
    manager = _manager(tmp_path)
    manager.config.allowed_backends.append({
        "engine": "ollama-api",
        "model": "local-qwen:27b",
        "models": ["local-qwen:27b", "local-gemma:31b"],
        "available_models": available,
    })
    manager.global_config.her_providers["providers"]["ollama"] = {
        "engine": "ollama-api",
        "models": ["profile-only-model"],
    }

    option = manager._her_v3_provider_option("ollama")

    assert option["models"] == available
    assert option["available"] is bool(available)
    assert option["default_model"] == next(iter(available), "")
    if not available:
        with pytest.raises(ValueError, match="no configured models"):
            manager.prepare_her_v3_provider("ollama")
        return

    target = manager.prepare_her_v3_provider("ollama")
    assert target == HERv3ModelTarget("ollama-api", available[0])
    manager._her_v3_configuration_override = target.to_dict()
    assert manager.prepare_her_v3_model(available[1]).model == available[1]
    with pytest.raises(ValueError, match="not available"):
        manager.prepare_her_v3_model("profile-only-model")
    with pytest.raises(ValueError, match="not available"):
        manager.prepare_her_v3_model("gemma4:26b")


def test_level_two_persists_only_with_qualified_herv3_provider(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    monkeypatch.setenv("HASHI_PRIVACY_FILTER_PYTHON", sys.executable)

    assert manager.set_privacy_level(2) is PrivacyLevel.BASIC_REDACTION
    assert manager.state_store.read()["privacy_level"] == 2
    assert _manager_reloaded(manager).privacy_level is PrivacyLevel.BASIC_REDACTION
    with pytest.raises(PrivacyPolicyError, match="does not support"):
        manager.create_ephemeral_backend("deepseek-api", target_model="deepseek-flash")

    manager.config.active_backend = "codex-cli"
    with pytest.raises(PrivacyPolicyError, match="requires the HERV3 backend"):
        manager.create_herv3_provider_backend(
            "deepseek-api", target_model="deepseek-flash"
        )
    manager.config.active_backend = "her-v2"

    provider = HashiStageProvider(backend_manager=manager)._create_provider_backend(
        "deepseek-api", target_model="deepseek-flash"
    )
    assert provider.privacy_level is PrivacyLevel.BASIC_REDACTION
    assert provider._herv3_privacy_scope is True


def _manager_reloaded(manager: FlexibleBackendManager) -> FlexibleBackendManager:
    return FlexibleBackendManager(manager.config, manager.global_config, secrets={})


def test_level_two_rejects_unqualified_auxiliary_provider(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manager.config.allowed_backends[0]["her_v2"]["auxiliary"] = {
        "provider": "hashi-api", "model": "gpt-5.6-luna",
    }
    monkeypatch.setenv("HASHI_PRIVACY_FILTER_PYTHON", sys.executable)

    with pytest.raises(PrivacyPolicyError, match="does not support"):
        manager.set_privacy_level(2)
    assert manager.privacy_level is PrivacyLevel.PROVIDER_TRUST


def test_level_two_downgrade_does_not_take_effect_if_state_write_fails(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    monkeypatch.setenv("HASHI_PRIVACY_FILTER_PYTHON", sys.executable)
    manager.set_privacy_level(2)

    def failed_write(_update):
        raise OSError("synthetic state write failure")

    monkeypatch.setattr(manager.state_store, "update", failed_write)
    with pytest.raises(OSError, match="synthetic state write failure"):
        manager.set_privacy_level(1)

    assert manager.privacy_level is PrivacyLevel.BASIC_REDACTION


@pytest.mark.asyncio
async def test_v3_manager_initializes_from_public_main_target_without_v2_profiles(
    tmp_path,
):
    manager = _manager(tmp_path)
    manager.config.allowed_backends[0]["her_v2"].update({
        "profiles": {"premium": {"engine": "hashi-api", "model": "gpt-5.6-sol"}},
        "v3_provider_allowlist": ["deepseek-api"],
    })
    manager.runtime = SimpleNamespace(backend_manager=manager)

    assert await manager.initialize_active_backend() is True
    assert set(manager.current_backend._v2_config.profiles) == {"main", "auxiliary"}
    assert {
        (profile.engine, profile.model)
        for profile in manager.current_backend._v2_config.profiles.values()
    } == {("deepseek-api", "deepseek-v4-pro")}


@pytest.mark.parametrize("provider,model,choices,initial,nested", [
    ("deepseek-api", "deepseek-v4-pro", ["off", "high", "max"], "high", False),
    ("ollama", "local-qwen:27b", ["enabled", "off"], "enabled", False),
    ("ollama", "local-qwen:27b", ["enabled", "off"], "enabled", True),
])
def test_v3_effort_choices_follow_provider_model_and_persist(
    tmp_path, provider, model, choices, initial, nested
):
    manager = _manager(tmp_path)
    if provider == "ollama":
        policy = {
            "engine": "ollama-api", "models": [model], "available_models": [model],
            "model_efforts": {model: choices},
        }
        if nested:
            manager.config.allowed_backends[0]["her_v2"]["provider_policies"] = [policy]
        else:
            manager.config.allowed_backends.append(policy)
    target = HERv3ModelTarget(provider, model)
    manager.apply_her_v3_target(target)
    effective = apply_v3_target(_raw_config(), target)
    manager.current_backend = SimpleNamespace(
        effort=initial,
        _v2_config=HERv2Config.from_mapping(effective),
        config=SimpleNamespace(engine="her-v2", extra={"her_v2": effective}),
    )
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.config = manager.config
    runtime.backend_manager = manager

    assert runtime._get_available_efforts() == choices
    assert runtime._get_current_effort() == initial
    buttons = [b for row in runtime._effort_keyboard().inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == [f"effort:{choice}" for choice in choices]
    if provider == "ollama":
        assert buttons[0].text != "✓ enabled"
        assert "<code>enabled</code>" not in runtime._build_effort_followup_text()

    for selected in ("off", initial):
        runtime._set_active_effort(selected)
        assert manager.current_backend.effort == ("zero" if selected == "off" else selected)
        assert runtime._get_current_effort() == selected
        reloaded = _manager_reloaded(manager)
        backend = reloaded.config.allowed_backends[0]
        assert backend["effort"] == selected
        assert reloaded.state_store.read()["backend_efforts"]["her-v2"] == selected


@pytest.mark.parametrize("policies,message", [
    ({}, "must be a list"),
    (["invalid-row"], "must be an object"),
    ([{"engine": "her-v3"}], "requires a concrete Provider"),
    ([{"engine": "unknown-provider"}], "requires a concrete Provider"),
    ([{"engine": "ollama"}], "outside the Provider allowlist"),
])
def test_v3_rejects_malformed_or_out_of_scope_provider_policy(policies, message):
    raw = {
        **_raw_config(),
        "v3_provider_allowlist": ["deepseek"],
        "provider_policies": policies,
    }

    with pytest.raises(ValueError, match=message):
        normalise_v3_config(raw)
