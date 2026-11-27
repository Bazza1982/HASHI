"""Guardrail: BackendPreflight must be honest for every registered and unknown engine."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from adapters.registry import registered_backend_engines
from orchestrator.backend_preflight import BackendPreflight
from orchestrator.flexible_backend_registry import get_secret_lookup_order


def _global_cfg():
    return SimpleNamespace(
        claude_cmd="__missing_claude_cli_for_preflight_guard__",
        codex_cmd="__missing_codex_cli_for_preflight_guard__",
        agy_cmd="__missing_agy_cli_for_preflight_guard__",
        grok_cmd="__missing_grok_cli_for_preflight_guard__",
        dsh_cmd="__missing_dsh_cli_for_preflight_guard__",
        agy_launch_mode="direct",
        ollama_base_url="http://127.0.0.1:9",
        hermes_home="",
    )


def test_every_registered_engine_has_an_explicit_preflight_check():
    preflight = BackendPreflight()
    registered = sorted(registered_backend_engines())
    assert registered, "registered_backend_engines() must not be empty"

    configs = [
        SimpleNamespace(engine=engine, name=f"agent-{engine}", allowed_backends=[])
        for engine in registered
    ]
    result = preflight.check_backend_availability(_global_cfg(), configs, {})

    for engine in registered:
        assert engine in result, f"registered engine {engine!r} omitted from preflight result"
        available, reason = result[engine]
        assert isinstance(available, bool)
        assert isinstance(reason, str) and reason.strip()
        assert "unknown engine" not in reason.casefold(), (
            f"registered engine {engine!r} fell through to unknown-engine lie: {reason!r}"
        )
        assert "assuming available" not in reason.casefold(), (
            f"registered engine {engine!r} lied about availability: {reason!r}"
        )


@pytest.mark.parametrize("engine", ["unknown-future-api", "typo-codex", "nonexistent-cli"])
def test_unregistered_engines_never_lie_available(engine: str):
    preflight = BackendPreflight()
    cfg = SimpleNamespace(engine=engine, name="tester", allowed_backends=[])
    result = preflight.check_backend_availability(_global_cfg(), [cfg], {})
    available, reason = result[engine]

    assert available is False
    assert "assuming available" not in reason.casefold()


@pytest.mark.parametrize("engine", ["openrouter-api", "deepseek-api", "openai-compatible-api"])
def test_secret_backed_api_engines_require_real_keys(engine: str):
    preflight = BackendPreflight()
    cfg = SimpleNamespace(engine=engine, name="api-agent", allowed_backends=[])

    # Missing secret -> honestly unavailable
    missing = preflight.check_backend_availability(_global_cfg(), [cfg], {})
    assert missing[engine][0] is False

    # Present secret -> honestly available
    secret_keys = get_secret_lookup_order(engine, "api-agent")
    assert secret_keys, f"{engine} expected to declare secret lookup keys"
    present = preflight.check_backend_availability(
        _global_cfg(),
        [cfg],
        {secret_keys[0]: "sk-test-valid-placeholder-key"},
    )
    assert present[engine][0] is True


def test_local_and_self_managed_engines_remain_honestly_startable():
    preflight = BackendPreflight()
    configs = [
        SimpleNamespace(engine="her-v2", name="her-agent", allowed_backends=[]),
        SimpleNamespace(engine="hashi-api", name="hashi-agent", allowed_backends=[]),
        SimpleNamespace(engine="ollama-api", name="ollama-agent", allowed_backends=[]),
    ]
    result = preflight.check_backend_availability(_global_cfg(), configs, {})
    for engine in ("her-v2", "hashi-api", "ollama-api"):
        available, reason = result[engine]
        assert available is True, f"{engine} should remain startable, got {reason!r}"
        assert "assuming available" not in reason.casefold()
