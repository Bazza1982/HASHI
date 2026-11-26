"""Regression coverage for the P0 bugs fixed by the test-diet PR-1."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.base import BaseBackend
from orchestrator.backend_preflight import BackendPreflight
from tools.pricing_sources import PricingFact, calculate_cost


class _StalledBackend:
    IDLE_TIMEOUT_SEC = 0.15
    _last_activity_age = BaseBackend._last_activity_age
    _wait_for_task_with_timeouts = BaseBackend._wait_for_task_with_timeouts


@pytest.mark.asyncio
async def test_cold_start_without_output_still_hits_the_idle_timeout():
    backend = _StalledBackend()
    task = asyncio.create_task(asyncio.sleep(30))
    started = time.monotonic()
    try:
        kind = await backend._wait_for_task_with_timeouts(
            task, started_monotonic=started
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert kind == "idle"
    assert time.monotonic() - started < 5


def test_preflight_reports_unknown_engines_as_unavailable():
    preflight = BackendPreflight()
    config = SimpleNamespace(
        engine="mystery-api",
        name="tester",
        allowed_backends=[],
    )
    result = preflight.check_backend_availability(SimpleNamespace(claude_cmd="claude", codex_cmd="codex", agy_cmd="agy", grok_cmd="grok", dsh_cmd="dsh", agy_launch_mode="direct"), [config], {})
    available, reason = result["mystery-api"]
    assert available is False
    assert "no availability check" in reason


def test_preflight_never_assumes_a_missing_api_key_is_available():
    preflight = BackendPreflight()
    config = SimpleNamespace(engine="deepseek-api", name="tester", allowed_backends=[])
    result = preflight.check_backend_availability(SimpleNamespace(claude_cmd="claude", codex_cmd="codex", agy_cmd="agy", grok_cmd="grok", dsh_cmd="dsh", agy_launch_mode="direct"), [config], {})
    available, reason = result["deepseek-api"]
    assert available is False
    assert "no API key" in reason


def _preflight_global_config():
    return SimpleNamespace(
        claude_cmd="claude",
        codex_cmd="codex",
        agy_cmd="agy",
        grok_cmd="grok",
        dsh_cmd="dsh",
        agy_launch_mode="direct",
    )


@pytest.mark.parametrize("engine", ["her-v2", "her-v3"])
def test_preflight_never_blanket_disables_the_live_her_runtime(engine):
    preflight = BackendPreflight()
    config = SimpleNamespace(engine=engine, name="her-agent")
    result = preflight.check_backend_availability(_preflight_global_config(), [config], {})
    available, reason = result[engine]
    assert available is True
    assert "Agent" in reason

    startable, skipped = preflight.partition_agents_by_availability([config], result)
    assert [cfg.name for cfg in startable] == ["her-agent"]
    assert skipped == []


def test_preflight_treats_a_registry_engine_without_secrets_as_available():
    preflight = BackendPreflight()
    config = SimpleNamespace(engine="hashi-api", name="hashi-agent")
    result = preflight.check_backend_availability(_preflight_global_config(), [config], {})
    available, reason = result["hashi-api"]
    assert available is True
    assert "no API key required" in reason


def test_preflight_does_not_gate_ollama_on_startup_reachability():
    preflight = BackendPreflight()
    config = SimpleNamespace(engine="ollama-api", name="ollama-agent")
    global_cfg = _preflight_global_config()
    # Nothing listens on this port: a startup probe would mark the engine down.
    global_cfg.ollama_base_url = "http://127.0.0.1:9"

    result = preflight.check_backend_availability(global_cfg, [config], {})
    available, reason = result["ollama-api"]
    assert available is True
    assert "adapter" in reason

    startable, skipped = preflight.partition_agents_by_availability([config], result)
    assert [cfg.name for cfg in startable] == ["ollama-agent"]
    assert skipped == []


def _pricing_fact(*, thinking_per_unit):
    return PricingFact(
        status="known",
        scope="model",
        engine="deepseek-api",
        requested_model_id="deepseek-chat",
        canonical_model_id="deepseek-chat",
        currency="USD",
        unit="token",
        input_per_unit=0.000001,
        output_per_unit=0.000002,
        cache_read_per_unit=None,
        cache_write_per_unit=None,
        cache_write_tiers=(),
        thinking_per_unit=thinking_per_unit,
        request_per_unit=None,
        source_url=None,
        unit_source_url=None,
        source_kind=None,
        fetched_at="2026-11-25T00:00:00Z",
        expires_at="2026-11-26T00:00:00Z",
        source_revision=None,
        revision_kind=None,
        evidence_sha256=None,
        unknown_reason=None,
    )


def test_separate_thinking_without_a_rate_falls_back_to_output_rate():
    cost = calculate_cost(
        _pricing_fact(thinking_per_unit=None),
        input_tokens=1_000,
        output_tokens=1_000,
        thinking_tokens=2_000,
    )
    # 1k input * 1e-6 + (1k output + 2k thinking) * 2e-6
    assert cost == pytest.approx(0.007)


def test_package_ships_the_hal_runtime_and_not_the_removed_native_tree():
    package = json.loads(
        (Path(__file__).resolve().parents[1] / "package.json").read_text(
            encoding="utf-8"
        )
    )
    assert "hal/" in package["files"]
    assert "native/" not in package["files"]
# HASHI-Fingerprint: hfp_a1b6606d8aa942db84ec4b572b5a6dfa
