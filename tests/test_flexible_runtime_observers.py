from __future__ import annotations

import asyncio
import logging
import sys
import types
from types import SimpleNamespace

import pytest

sys.modules.setdefault("edge_tts", types.SimpleNamespace(Communicate=object))

from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


def _runtime() -> FlexibleAgentRuntime:
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime._background_tasks = set()
    runtime._post_turn_observers = []
    runtime._pre_turn_context_providers = []
    runtime.logger = logging.getLogger("test.flex-runtime-observers")
    runtime.get_current_model = lambda: "gpt-test"
    return runtime


def _item(source: str = "api") -> SimpleNamespace:
    return SimpleNamespace(
        request_id="req-1",
        chat_id=123,
        prompt="User prompt",
        source=source,
        summary="Summary",
        created_at="2026-05-05T00:00:00",
    )


class _Provider:
    def __init__(self):
        self.requests = []

    def should_provide(self, source: str, *, is_bridge_request: bool) -> bool:
        return source == "api" and not is_bridge_request

    async def build_context_sections(self, request):
        self.requests.append(request)
        return [("OBSERVER", f"{request.user_text}:{request.model_name}")]

    def workspace_files_to_preserve(self):
        return frozenset({"observer.json"})


class _FailingProvider(_Provider):
    async def build_context_sections(self, request):
        raise RuntimeError("provider failed")


class _Observer:
    def __init__(self):
        self.requests = []
        self.background_sets = []

    def should_observe(self, source: str, *, is_bridge_request: bool) -> bool:
        return source == "api" and not is_bridge_request

    def schedule_observation(self, request, background_tasks: set[asyncio.Task]):
        self.requests.append(request)
        self.background_sets.append(background_tasks)

    def workspace_files_to_preserve(self):
        return frozenset({"observer.json"})


class _FailingObserver(_Observer):
    def schedule_observation(self, request, background_tasks: set[asyncio.Task]):
        raise RuntimeError("observer failed")


@pytest.mark.asyncio
async def test_pre_turn_context_sections_are_built_for_allowed_sources():
    runtime = _runtime()
    provider = _Provider()
    runtime._pre_turn_context_providers = [provider]

    sections = await runtime._build_pre_turn_context_sections(
        _item(),
        "Effective prompt",
        is_bridge_request=False,
    )

    assert sections == [("OBSERVER", "Effective prompt:gpt-test")]
    assert provider.requests[0].request_id == "req-1"
    assert provider.requests[0].chat_id == 123


@pytest.mark.asyncio
async def test_pre_turn_context_sections_skip_bridge_requests():
    runtime = _runtime()
    provider = _Provider()
    runtime._pre_turn_context_providers = [provider]

    sections = await runtime._build_pre_turn_context_sections(
        _item("bridge:api"),
        "Effective prompt",
        is_bridge_request=True,
    )

    assert sections == []
    assert provider.requests == []


@pytest.mark.asyncio
async def test_pre_turn_context_provider_failure_logs_and_continues(caplog):
    runtime = _runtime()
    runtime._pre_turn_context_providers = [_FailingProvider(), _Provider()]

    sections = await runtime._build_pre_turn_context_sections(
        _item(),
        "Effective prompt",
        is_bridge_request=False,
    )

    assert sections == [("OBSERVER", "Effective prompt:gpt-test")]
    assert "Pre-turn context provider failed" in caplog.text


def test_post_turn_observer_is_scheduled_for_allowed_sources():
    runtime = _runtime()
    observer = _Observer()
    runtime._post_turn_observers = [observer]

    runtime._schedule_post_turn_observers(
        _item(),
        "User memory",
        "Assistant memory",
        is_bridge_request=False,
    )

    assert observer.requests[0].assistant_text == "Assistant memory"
    assert observer.requests[0].user_text == "User memory"
    assert observer.background_sets == [runtime._background_tasks]


def test_post_turn_observer_failure_logs_and_continues(caplog):
    runtime = _runtime()
    observer = _Observer()
    runtime._post_turn_observers = [_FailingObserver(), observer]

    runtime._schedule_post_turn_observers(
        _item(),
        "User memory",
        "Assistant memory",
        is_bridge_request=False,
    )

    assert observer.requests
    assert "Post-turn observer failed to schedule" in caplog.text


def test_observer_workspace_keep_names_collects_observer_files():
    runtime = _runtime()
    runtime._post_turn_observers = [_Observer()]

    assert runtime._observer_workspace_keep_names() == {"observer.json"}
