from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator.config import FlexibleAgentConfig, GlobalConfig
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.her_v2.models import Route, Stage, TriageClassification
from orchestrator.her_v2.runtime_configuration import (
    HER_V2_CAPABILITY_REVISION,
    HER_V2_PRICING_REVISION,
    resolve_her_v2_configuration,
)


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


def test_retired_configured_her_mode_normalizes_to_planned(tmp_path):
    manager = _manager(tmp_path)
    her_backend = next(
        item for item in manager.config.allowed_backends if item["engine"] == "her-v2"
    )

    assert her_backend["effort"] == "medium"


def test_retired_persisted_her_mode_migrates_to_planned(tmp_path):
    manager = _manager(
        tmp_path,
        state={
            "active_backend": "her-v2",
            "backend_efforts": {"her-v2": "max"},
        },
    )
    her_backend = next(
        item for item in manager.config.allowed_backends if item["engine"] == "her-v2"
    )
    state = json.loads(manager.state_file.read_text(encoding="utf-8"))

    assert her_backend["effort"] == "medium"
    assert state["backend_efforts"] == {"her-v2": "medium"}


def test_her_v2_provider_options_are_concrete_call_providers(tmp_path):
    manager = _manager(tmp_path)
    manager.config.allowed_backends[0]["models"].append("role-configured")

    options = {item["engine"]: item for item in manager.get_her_v2_provider_options()}

    assert options["deepseek-api"] == {
        "name": "deepseek",
        "engine": "deepseek-api",
        "label": "deepseek",
        "status": "stable",
        "models": [
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "deepseek-v4-flash-vision-exp",
        ],
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
    before = manager.prepare_her_v2_route_model_slot("review", "fast")
    before = manager.prepare_her_v2_route_reasoning(
        "review",
        "low",
        current=before,
    )

    candidate = manager.prepare_her_v2_provider("OpenRouter", current=before)

    assert candidate.provider == "openrouter-api"
    assert candidate.fast_model == "deepseek/deepseek-v4-flash"
    assert candidate.pro_model == "anthropic/claude-sonnet-4.6"
    assert candidate.profile_reasoning == before.profile_reasoning
    assert candidate.stage_reasoning == before.stage_reasoning
    assert candidate.route_model_slots == before.route_model_slots
    assert candidate.route_reasoning == before.route_reasoning


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


def test_instance_configured_provider_does_not_need_agent_backend_row(tmp_path):
    manager = _manager(tmp_path)
    manager.global_config.her_providers["providers"]["xai"] = {
        "engine": "xai-api",
        "status": "stable",
    }

    option = manager._her_v2_provider_option("xai")
    selected = manager.prepare_her_v2_provider("xai")

    assert option is not None
    assert option["available"] is True
    assert "grok-4.5" in option["models"]
    assert selected.fast_provider == "xai-api"
    assert selected.pro_provider == "xai-api"


def test_instance_configured_provider_can_create_her_ephemeral_backend(tmp_path):
    manager = _manager(tmp_path)
    manager.global_config.her_providers["providers"]["hashi"] = {
        "base_url": "http://127.0.0.1:18801/v1",
        "status": "provisional",
    }

    option = manager._her_v2_provider_option("hashi")

    backend = manager.create_ephemeral_backend(
        "hashi-api",
        target_model="gpt-5.6-luna",
    )

    assert option is not None
    assert option["status"] == "provisional"
    assert option["available"] is True
    assert backend.config.engine == "hashi-api"
    assert backend.config.model == "gpt-5.6-luna"
    assert backend.hashi_url == "http://127.0.0.1:18801/v1/chat/completions"


def test_hybrid_draft_applies_full_targets_and_custom_route_atomically(tmp_path):
    manager = _manager(tmp_path)
    manager.current_backend = SimpleNamespace(
        config=SimpleNamespace(
            engine="her-v2",
            model="role-configured",
            extra={"her_v2": _her_v2_config()},
        ),
        _v2_config=None,
        effort="medium",
    )
    active_before = manager.get_her_v2_configuration()
    draft = manager.begin_her_v2_hybrid_draft()
    draft = manager.prepare_her_v2_model(
        "fast",
        "deepseek/deepseek-v4-flash",
        provider="openrouter-api",
        current=draft,
    )
    draft = manager.prepare_her_v2_route_target(
        "review",
        "openrouter-api",
        "anthropic/claude-sonnet-4.6",
        current=draft,
    )
    manager.stage_her_v2_configuration(draft)

    assert manager.get_her_v2_configuration() == active_before
    assert manager.current_backend._v2_config is None
    staged_state = json.loads(manager.state_file.read_text(encoding="utf-8"))
    assert staged_state["her_v2_configuration_draft"]["routing_mode"] == "hybrid"

    applied = manager.apply_her_v2_configuration_draft()

    assert applied.routing_mode == "hybrid"
    assert applied.fast_provider == "openrouter-api"
    assert applied.pro_provider == "deepseek-api"
    assert applied.provider == "mixed"
    assert applied.target_for_route("review").provider == "openrouter-api"
    assert applied.target_for_route("review").model == "anthropic/claude-sonnet-4.6"
    state = json.loads(manager.state_file.read_text(encoding="utf-8"))
    assert "her_v2_configuration_draft" not in state
    assert set(state["her_v2_configuration_presets"]) == {"single", "hybrid"}
    configured = manager.current_backend._v2_config
    assert configured.profile_for(Stage.TRIAGE).engine == "openrouter-api"
    assert configured.profile_for(Stage.PLANNING).engine == "deepseek-api"
    assert configured.profile_for(Stage.REVIEW).engine == "openrouter-api"
    assert configured.profile_for_name("orchestrator").engine == "deepseek-api"


def test_last_hybrid_configuration_is_restored_after_single_mode(tmp_path):
    manager = _manager(tmp_path)
    hybrid = manager.begin_her_v2_hybrid_draft()
    hybrid = manager.prepare_her_v2_model(
        "fast",
        "deepseek/deepseek-v4-flash",
        provider="openrouter-api",
        current=hybrid,
    )
    manager.stage_her_v2_configuration(hybrid)
    manager.apply_her_v2_configuration_draft()
    manager.apply_her_v2_configuration(
        manager.prepare_her_v2_provider("openrouter-api")
    )

    restored = manager.begin_her_v2_hybrid_draft()

    assert restored.routing_mode == "hybrid"
    assert restored.fast_provider == "openrouter-api"
    assert restored.pro_provider == "deepseek-api"


def test_apply_replaces_live_config_without_mutating_turn_snapshot(tmp_path):
    manager = _manager(tmp_path)
    manager.current_backend = SimpleNamespace(
        config=SimpleNamespace(
            engine="her-v2",
            model="role-configured",
            extra={"her_v2": _her_v2_config()},
        ),
        _v2_config=None,
        effort="medium",
    )
    manager.apply_her_v2_configuration(
        manager.prepare_her_v2_provider("openrouter-api")
    )
    frozen_turn_config = manager.current_backend._v2_config

    manager.apply_her_v2_configuration(
        manager.prepare_her_v2_provider("deepseek-api")
    )

    assert frozen_turn_config.profile_for(Stage.PLANNING).engine == "openrouter-api"
    assert manager.current_backend._v2_config is not frozen_turn_config
    assert manager.current_backend._v2_config.profile_for(Stage.PLANNING).engine == (
        "deepseek-api"
    )


def test_routing_revision_advances_only_when_default_route_changes(tmp_path):
    manager = _manager(tmp_path)
    assert manager.get_her_v2_configuration().routing_revision == 1

    manager.apply_her_v2_configuration(
        manager.prepare_her_v2_provider("openrouter-api")
    )
    changed = manager.get_her_v2_configuration()
    assert changed.routing_revision == 2

    manager.apply_her_v2_configuration(changed)
    unchanged = manager.get_her_v2_configuration()
    assert unchanged.routing_revision == 2
    persisted = json.loads(manager.state_file.read_text(encoding="utf-8"))
    assert persisted["her_v2_configuration"]["routing_revision"] == 2


def test_saved_route_cannot_pin_stale_capability_or_pricing_revisions():
    selected = resolve_her_v2_configuration(
        _her_v2_config(),
        {
            "routing_revision": 9,
            "capability_revision": 999,
            "pricing_revision": "stale-prices",
        },
    )

    assert selected.routing_revision == 9
    assert selected.capability_revision == HER_V2_CAPABILITY_REVISION
    assert selected.pricing_revision == HER_V2_PRICING_REVISION


def test_provider_switch_preflight_rejects_known_too_small_context(tmp_path):
    manager = _manager(tmp_path)
    for grant in manager.config.allowed_backends:
        if grant.get("engine") == "openrouter-api":
            grant["context_window_tokens"] = 32_000
            grant["response_headroom_tokens"] = 4_000
    runtime = SimpleNamespace(
        backend_manager=manager,
        global_config=manager.global_config,
        workspace_dir=manager.config.workspace_dir,
        _last_full_prompt_tokens=999_000,
    )
    manager.runtime = runtime
    candidate = manager.prepare_her_v2_provider("openrouter-api")

    with pytest.raises(ValueError, match="context is too small"):
        manager.apply_her_v2_configuration(candidate)

    assert manager.get_her_v2_configuration().routing_revision == 1


def test_reasoning_only_switch_does_not_run_target_context_preflight(tmp_path):
    manager = _manager(tmp_path)
    runtime = SimpleNamespace(
        backend_manager=manager,
        global_config=manager.global_config,
        workspace_dir=manager.config.workspace_dir,
        _last_full_prompt_tokens=999_000,
    )
    manager.runtime = runtime

    manager.apply_her_v2_configuration(
        manager.prepare_her_v2_route_reasoning("planning", "medium")
    )

    assert manager.get_her_v2_configuration().routing_revision == 2
    persisted = json.loads(manager.state_file.read_text(encoding="utf-8"))
    assert persisted["her_v2_last_route_preflight"]["status"] == "not_required"


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


def test_slot_model_and_route_controls_are_independent(tmp_path):
    manager = _manager(tmp_path)
    current = manager.get_her_v2_configuration()

    model_candidate = manager.prepare_her_v2_model(
        "fast", "deepseek-v4-pro", current=current
    )
    route_candidate = manager.prepare_her_v2_route_model_slot(
        "review", "fast", current=model_candidate
    )
    reasoning_candidate = manager.prepare_her_v2_route_reasoning(
        "review", "low", current=route_candidate
    )

    assert reasoning_candidate.fast_model == "deepseek-v4-pro"
    assert reasoning_candidate.pro_model == "deepseek-v4-pro"
    assert reasoning_candidate.model_slot_for_route(Route.REVIEW) == "fast"
    assert reasoning_candidate.route_reasoning == {"review": "low"}
    assert reasoning_candidate.stage_reasoning == {}
    assert reasoning_candidate.profile_reasoning == current.profile_reasoning
    assert reasoning_candidate.provider == "deepseek-api"


def test_default_routes_expose_actual_execution_classification_profiles(tmp_path):
    manager = _manager(tmp_path)
    selected = manager.get_her_v2_configuration()

    assert selected.model_slot_for_route(Route.EXECUTION_SIMPLE) == "fast"
    assert selected.model_slot_for_route(Route.EXECUTION_COMPLEX) == "pro"
    assert selected.model_slot_for_route(Route.EXECUTION_HIGH_VOLUME) == "pro"


def test_direct_route_is_fixed_to_quick_with_overridable_high_reasoning(tmp_path):
    manager = _manager(tmp_path)
    selected = manager.get_her_v2_configuration()

    assert selected.model_slot_for_route(Route.DIRECT) == "fast"
    assert selected.target_for_route(Route.DIRECT) == selected.target_for_slot("fast")
    assert selected.reasoning_for_route(_her_v2_config(), Route.DIRECT) == "high"

    overridden = manager.prepare_her_v2_route_reasoning(
        "direct",
        "xhigh",
        current=selected,
    )
    assert overridden.reasoning_for_route(_her_v2_config(), Route.DIRECT) == "xhigh"

    with pytest.raises(ValueError, match="always uses the Quick"):
        manager.prepare_her_v2_route_model_slot(
            "direct",
            "pro",
            current=selected,
        )

    hybrid = manager.prepare_her_v2_hybrid(current=selected)
    with pytest.raises(ValueError, match="always uses the Quick"):
        manager.prepare_her_v2_route_target(
            "direct",
            "openrouter-api",
            "deepseek/deepseek-v4-flash",
            current=hybrid,
        )


def test_apply_configuration_persists_and_refreshes_live_adapter_atomically(tmp_path):
    manager = _manager(tmp_path)
    manager.current_backend = SimpleNamespace(
        config=SimpleNamespace(
            engine="her-v2",
            model="role-configured",
            extra={"her_v2": _her_v2_config()},
        ),
        _v2_config=None,
        effort="medium",
    )
    candidate = manager.prepare_her_v2_route_model_slot("planning", "fast")
    candidate = manager.prepare_her_v2_route_reasoning(
        "planning",
        "medium",
        current=candidate,
    )

    manager.apply_her_v2_configuration(candidate)

    state = json.loads(manager.state_file.read_text(encoding="utf-8"))
    assert state["her_v2_configuration"]["route_model_slots"]["planning"] == "fast"
    assert state["her_v2_configuration"]["route_reasoning"] == {
        "planning": "medium"
    }
    assert state["backend_efforts"] == {"her-v2": "medium"}
    assert "provider_reasoning" not in state
    assert manager.current_backend.effort == "medium"
    planning = manager.current_backend._v2_config.profile_for(Stage.PLANNING)
    assert planning.model == "deepseek-v4-flash"
    assert planning.reasoning == "medium"
    simple = manager.current_backend._v2_config.execution_profile_for(
        TriageClassification.SIMPLE_TASK
    )
    complex_task = manager.current_backend._v2_config.execution_profile_for(
        TriageClassification.COMPLEX_TASK
    )
    assert simple.model == "deepseek-v4-flash"
    assert complex_task.model == "deepseek-v4-pro"


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


@pytest.mark.parametrize(
    ("persisted_backend", "persisted_model"),
    [
        ("deepseek-api", "deepseek-v4-flash"),
        ("openrouter-api", "deepseek/deepseek-v4-flash"),
    ],
)
def test_direct_provider_state_migrates_to_her_v2(
    tmp_path, persisted_backend, persisted_model
):
    manager = _manager(
        tmp_path,
        state={
            "active_backend": persisted_backend,
            "active_model": persisted_model,
            "agent_mode": "flex",
        },
    )

    state = json.loads(manager.state_file.read_text(encoding="utf-8"))
    selected = manager.get_her_v2_configuration()

    assert manager.config.active_backend == "her-v2"
    assert state["active_backend"] == "her-v2"
    assert "active_provider" not in state
    assert "active_model" not in state
    assert selected.provider == persisted_backend
    assert selected.fast_model == persisted_model
    assert selected.pro_model == persisted_model


@pytest.mark.parametrize("persisted_backend", ["her", "her-v2"])
def test_her_v2_fixed_mode_is_preserved(
    tmp_path,
    persisted_backend,
):
    manager = _manager(
        tmp_path,
        state={
            "active_backend": persisted_backend,
            "agent_mode": "fixed",
            "unrelated": {"keep": True},
        },
    )

    state = json.loads(manager.state_file.read_text(encoding="utf-8"))

    assert manager.config.active_backend == "her-v2"
    assert manager.agent_mode == "fixed"
    assert state["active_backend"] == "her-v2"
    assert state["agent_mode"] == "fixed"
    assert state["unrelated"] == {"keep": True}


def test_persisted_hybrid_provider_names_normalize_to_engine_ids(tmp_path):
    manager = _manager(
        tmp_path,
        state={
            "active_backend": "her-v2",
            "her_v2_configuration": {
                "routing_mode": "hybrid",
                "targets": {
                    "fast": {
                        "provider": "openrouter",
                        "model": "deepseek/deepseek-v4-flash",
                    },
                    "pro": {
                        "provider": "deepseek",
                        "model": "deepseek-v4-pro",
                    },
                },
            },
        },
    )

    selected = manager.get_her_v2_configuration()

    assert selected.fast_provider == "openrouter-api"
    assert selected.pro_provider == "deepseek-api"


def test_invalid_model_does_not_write_runtime_state(tmp_path):
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match="not allowed"):
        manager.prepare_her_v2_model("pro", "ungranted/model")

    assert not manager.state_file.exists()


def test_persisted_selection_is_applied_when_adapter_config_is_rebuilt(tmp_path):
    manager = _manager(tmp_path)
    selected = manager.prepare_her_v2_provider("openrouter")
    selected = manager.prepare_her_v2_route_model_slot(
        "review",
        "fast",
        current=selected,
    )
    selected = manager.prepare_her_v2_route_reasoning(
        "review",
        "low",
        current=selected,
    )
    manager.apply_her_v2_configuration(selected)
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
    assert adapter_config.extra["her_v2"]["route_model_slots"]["review"] == "fast"
    assert adapter_config.extra["her_v2"]["route_reasoning"] == {"review": "low"}


def test_persistence_failure_keeps_previous_live_configuration(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manager.current_backend = SimpleNamespace(
        config=SimpleNamespace(
            engine="her-v2",
            model="role-configured",
            extra={"her_v2": _her_v2_config()},
        ),
        _v2_config=None,
        effort="medium",
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
