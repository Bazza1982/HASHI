"""Minimal smoke coverage for the live wrapper runtime path.

``runtime_wrapper`` stays reachable from ``flexible_agent_runtime`` (mode gate,
audit fields, transcript, visible-text pipeline). The retired mode's full
command suites were pruned, so this keeps a thin behavioural check on that
still-live path without restoring them.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import runtime_wrapper


def _runtime(mode: str):
    return SimpleNamespace(
        backend_manager=SimpleNamespace(agent_mode=mode),
        workspace_dir=None,
    )


def test_wrapper_gate_and_audit_fields_follow_the_agent_mode():
    flex = _runtime("flex")
    wrapper = _runtime("wrapper")
    result = SimpleNamespace(
        wrapper_used=True,
        wrapper_failed=False,
        latency_ms=12.3456,
        fallback_reason=None,
        final_text="hello",
    )

    assert runtime_wrapper.wrapper_enabled(flex) is False
    assert runtime_wrapper.wrapper_enabled(wrapper) is True

    fields = runtime_wrapper.wrapper_audit_fields(wrapper, result)
    assert fields == {
        "wrapper_mode": True,
        "wrapper_used": True,
        "wrapper_failed": False,
        "wrapper_latency_ms": 12.346,
        "wrapper_fallback_reason": None,
        "wrapper_final_chars": 5,
    }

    listener = runtime_wrapper.wrapper_listener_fields("raw", "visible", result)
    assert listener["core_raw"] == "raw"
    assert listener["visible_text"] == "visible"
    assert listener["wrapper_used"] is True


def test_core_memory_text_prefers_raw_core_only_when_the_wrapper_ran():
    wrapper = _runtime("wrapper")
    used = SimpleNamespace(wrapper_used=True, wrapper_failed=False)
    bypassed = SimpleNamespace(wrapper_used=False, wrapper_failed=False)

    assert (
        runtime_wrapper.core_memory_assistant_text(wrapper, "core", "visible", used)
        == "core"
    )
    assert (
        runtime_wrapper.core_memory_assistant_text(wrapper, "core", "visible", bypassed)
        == "visible"
    )


@pytest.mark.asyncio
async def test_disabled_wrapper_returns_the_visible_text_unchanged():
    visible_text, result = await runtime_wrapper.apply_wrapper_to_visible_text(
        _runtime("flex"), object(), "plain text"
    )

    assert visible_text == "plain text"
    assert result.final_text == "plain text"
    assert result.wrapper_used is False
    assert result.fallback_reason == "wrapper_mode_disabled"


@pytest.mark.asyncio
async def test_enabled_wrapper_runs_the_processor_and_returns_its_text(monkeypatch):
    calls: list[dict] = []

    class _FakeProcessor:
        def __init__(self, config, *, backend_invoker):
            self.config = config
            self.backend_invoker = backend_invoker

        async def process(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                final_text="polished",
                wrapper_used=True,
                wrapper_failed=False,
                fallback_reason=None,
                latency_ms=1.5,
            )

    monkeypatch.setattr(runtime_wrapper, "WrapperProcessor", _FakeProcessor)
    monkeypatch.setattr(
        runtime_wrapper.telegram_stream_policy,
        "get_display_policy",
        lambda runtime: SimpleNamespace(typing_enabled=False),
    )

    runtime = SimpleNamespace(
        backend_manager=SimpleNamespace(
            agent_mode="wrapper",
            get_state_snapshot=lambda: {"wrapper": {"context_window": 2}},
            generate_ephemeral_response=AsyncMock(),
        ),
        workspace_dir=None,
        config=SimpleNamespace(extra={}),
        telegram_connected=False,
        _send_wrapper_polishing_placeholder=AsyncMock(return_value="placeholder"),
        _delete_wrapper_polishing_placeholder=AsyncMock(return_value=None),
        _wrapper_visible_context=lambda window, item=None: [
            {"role": "user", "text": "earlier"}
        ],
    )
    item = SimpleNamespace(
        request_id="req-1",
        source="user",
        prompt="hello",
        silent=True,
        deliver_to_telegram=False,
        chat_id=1,
    )

    visible_text, result = await runtime_wrapper.apply_wrapper_to_visible_text(
        runtime, item, "core reply"
    )

    assert visible_text == "polished"
    assert result.wrapper_used is True
    assert len(calls) == 1
    assert calls[0]["core_raw"] == "core reply"
    assert calls[0]["user_request"] == "hello"
    assert calls[0]["visible_context"] == [{"role": "user", "text": "earlier"}]
    runtime._delete_wrapper_polishing_placeholder.assert_awaited_once()


def test_wrapper_verbose_excerpt_keeps_both_ends_of_a_long_trace():
    text = "a" * 2000 + "b" * 2000
    excerpt = runtime_wrapper.wrapper_verbose_excerpt(text, limit=400)

    assert len(excerpt) < len(text)
    assert excerpt.startswith("a")
    assert excerpt.endswith("b")
    assert "truncated for verbose display" in excerpt
