"""Compact follows the live HERV3 model, including main-only configurations."""

from types import SimpleNamespace

import pytest

from orchestrator.context_compaction import (
    CompactionPolicy,
    ContextCompactionCoordinator,
    resolve_compact_route,
    resolve_target_capacity,
    resolve_trigger_budget,
)
from orchestrator.her_v2.v3_config import HERv3ModelTarget
from tests.test_context_compaction import _Runtime, _write_turns
from tests.test_her_v2_runtime_configuration import _manager


def _live_runtime(tmp_path):
    runtime = _Runtime(tmp_path)
    manager = _manager(tmp_path)
    runtime.backend_manager = manager
    runtime.global_config = manager.global_config
    return runtime, manager


def test_compact_uses_current_v3_main_instead_of_legacy_quick_slot(tmp_path):
    runtime, manager = _live_runtime(tmp_path)
    # The legacy Quick slot is Flash; the HERV3 main target is Pro.
    assert manager.get_her_v2_configuration().fast_model == "deepseek-v4-flash"
    assert manager.get_her_v3_target().model == "deepseek-v4-pro"
    route = resolve_compact_route(runtime)
    assert (route.provider, route.model) == ("deepseek-api", "deepseek-v4-pro")


def test_compact_follows_main_only_configuration_and_persisted_switch(tmp_path):
    runtime, manager = _live_runtime(tmp_path)
    manager._her_v2_backend_config()["her_v2"] = {
        "main": {"provider": "deepseek-api", "model": "deepseek-v4-pro"},
        "auxiliary": {"provider": "deepseek-api", "model": "deepseek-v4-flash"},
    }
    with pytest.raises(ValueError, match="profiles are required"):
        manager.get_her_v2_configuration()
    route = resolve_compact_route(runtime)
    assert (route.provider, route.model) == ("deepseek-api", "deepseek-v4-pro")

    manager.apply_her_v3_target(
        HERv3ModelTarget("openrouter-api", "anthropic/claude-sonnet-4.6")
    )
    route = resolve_compact_route(runtime)
    assert (route.provider, route.model) == (
        "openrouter-api", "anthropic/claude-sonnet-4.6"
    )


def test_capacity_ignores_stale_legacy_profiles(tmp_path):
    runtime, manager = _live_runtime(tmp_path)
    backends = manager.config.allowed_backends
    for row in backends:
        if row["engine"] == "deepseek-api":
            row.update(context_window_tokens=32768, response_headroom_tokens=2048)
        elif row["engine"] == "openrouter-api":
            row.update(context_window_tokens=200000, response_headroom_tokens=10000)
    raw = manager._her_v2_backend_config()["her_v2"]
    raw["main"] = {"provider": "openrouter-api", "model": "anthropic/claude-sonnet-4.6"}
    raw["auxiliary"] = dict(raw["main"])

    capacity = resolve_target_capacity(runtime)
    assert capacity.provider == "openrouter-api"
    assert capacity.model == "anthropic/claude-sonnet-4.6"
    assert capacity.context_window_tokens == 200000


def test_hli_capacity_trigger_uses_current_v3_main(tmp_path):
    runtime = _Runtime(tmp_path)
    runtime.backend_manager.config.allowed_backends.append({
        "engine": "hli-api",
        "model_capabilities": {
            "qwen-local": {
                "context_window_tokens": 8192,
                "response_headroom_tokens": 2048,
            }
        },
    })
    runtime.backend_manager.get_her_v3_target = lambda: HERv3ModelTarget(
        "hli-api", "qwen-local"
    )
    # A stale legacy target must not select a different provider.
    runtime.backend_manager._selected = SimpleNamespace(
        fast_provider="deepseek-api", fast_model="deepseek-v4-flash"
    )
    budget = resolve_trigger_budget(
        runtime,
        policy=CompactionPolicy(
            auto_trigger_tokens=128000, post_compaction_target_tokens=64000
        ),
    )
    assert budget.provenance == "hli_live_context_capacity_v1"
    assert budget.high_projected_tokens < 6144


@pytest.mark.asyncio
async def test_capacity_recovery_without_history_returns_truthful_outcome(tmp_path):
    runtime = _Runtime(tmp_path)
    _write_turns(runtime, 0)
    runtime.backend_manager.get_her_v3_target = lambda: HERv3ModelTarget(
        "deepseek-api", "deepseek-v4-pro"
    )

    def no_legacy_profiles():
        raise ValueError("HER v2 profiles are required")

    runtime.backend_manager.get_her_v2_configuration = no_legacy_profiles
    compactor = ContextCompactionCoordinator(runtime)
    outcome = await compactor.compact(
        trigger="typed_provider_capacity_rejection",
        request_ref="request-capacity",
        force=True,
    )
    assert outcome.status == "not_needed"
    assert outcome.code == "NO_ELIGIBLE_HISTORY_OUTSIDE_RECENT_GUARD"
    assert outcome.changed is False
    assert compactor._active_task is None
