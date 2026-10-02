from __future__ import annotations

import json
import sys
from types import SimpleNamespace

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


def test_v3_manager_persists_and_live_refreshes_model_target(tmp_path):
    manager = _manager(tmp_path)
    live_config = SimpleNamespace(engine="her-v2", extra={"her_v2": _raw_config()})
    manager.current_backend = SimpleNamespace(
        config=live_config,
        _v2_config=None,
    )

    target = manager.prepare_her_v3_model("deepseek-flash")
    manager.apply_her_v3_target(target)

    assert manager.get_her_v3_target() == target
    assert manager.current_backend._v2_config.profiles["main"].model == "deepseek-flash"
    assert live_config.extra["her_v2"]["profiles"]["main"]["model"] == "deepseek-flash"
    state = json.loads(manager.state_file.read_text(encoding="utf-8"))
    assert state[HER_V3_CONFIGURATION_STATE_KEY] == {
        "provider": "deepseek-api",
        "model": "deepseek-flash",
    }
    assert "her_v2_configuration" not in state


def test_v3_provider_options_derive_deepseek_models_from_provider_catalogue(tmp_path):
    manager = _manager(tmp_path)

    deepseek = manager._her_v3_provider_option("deepseek")

    assert deepseek is not None
    assert deepseek["available"] is True
    assert deepseek["models"] == ["deepseek-flash", "deepseek-v4-pro"]


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


def test_v3_effort_choices_are_deepseek_reasoning_levels_and_persist(tmp_path):
    manager = _manager(tmp_path)
    effective = apply_v3_target(
        _raw_config(),
        HERv3ModelTarget("deepseek-api", "deepseek-v4-pro"),
    )
    manager.current_backend = SimpleNamespace(
        effort="high",
        _v2_config=HERv2Config.from_mapping(effective),
        config=SimpleNamespace(engine="her-v2", extra={"her_v2": effective}),
    )
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.config = manager.config
    runtime.backend_manager = manager

    assert runtime._get_available_efforts() == ["off", "high", "max"]
    assert runtime._get_current_effort() == "high"

    runtime._set_active_effort("off")

    assert manager.current_backend.effort == "zero"
    assert runtime._get_current_effort() == "off"
    state = json.loads(manager.state_file.read_text(encoding="utf-8"))
    assert state["backend_efforts"]["her-v2"] == "off"
