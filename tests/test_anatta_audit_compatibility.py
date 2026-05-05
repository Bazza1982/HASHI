from __future__ import annotations

import asyncio
import logging
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.modules.setdefault("edge_tts", types.SimpleNamespace(Communicate=object))

from orchestrator.anatta.models import EmergentTurnState, PromptInjection
from orchestrator.anatta.post_turn_observer import AnattaPostTurnObserver
from orchestrator.audit_mode import should_audit_source
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


class _Bridge:
    def get_recent_turns(self, limit: int = 8):
        return []

    def retrieve_memories(self, text: str, limit: int = 6):
        return []


class _Layer:
    bridge_adapter = _Bridge()

    def __init__(self, mode: str = "on"):
        self._mode = mode
        self.recorded = []

    def mode(self):
        return self._mode

    def should_inject_prompt(self):
        return True

    def resolve_relationship_key(self, source, chat_id=None, metadata=None):
        return f"{source}:{chat_id}"

    async def build_turn_state(self, turn_context, model_name):
        state = EmergentTurnState(
            drive_values={"SEEKING": 70.0},
            dominant_drives=["SEEKING"],
            contributions=[],
            contributing_annotation_ids=[],
            rationale=[],
            relationship_key=turn_context.relationship_key,
            generated_at=datetime.now(timezone.utc),
        )
        return state, PromptInjection(
            title="INTERACTION PRIORITIES",
            body="Private Anatta guidance.",
            metadata={"inject": True},
        )

    async def record_async(self, turn_context, assistant_response, state):
        self.recorded.append((turn_context, assistant_response, state))


def _runtime_with_anatta(observer: AnattaPostTurnObserver) -> FlexibleAgentRuntime:
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.backend_manager = SimpleNamespace(agent_mode="audit")
    runtime._background_tasks = set()
    runtime._post_turn_observers = [observer]
    runtime._pre_turn_context_providers = [observer]
    runtime.logger = logging.getLogger("test.anatta-audit")
    runtime.get_current_model = lambda: "gpt-test"
    return runtime


def _item(source: str = "api") -> SimpleNamespace:
    return SimpleNamespace(
        request_id="req-compat",
        chat_id=123,
        prompt="User asks for a thoughtful answer.",
        source=source,
        summary="Compatibility",
        created_at="2026-05-05T00:00:00",
    )


@pytest.mark.asyncio
async def test_anatta_on_and_audit_mode_compose_without_wrapper():
    layer = _Layer("on")
    observer = AnattaPostTurnObserver(layer)
    runtime = _runtime_with_anatta(observer)
    item = _item("api")

    assert runtime._audit_enabled() is True
    assert runtime._wrapper_enabled() is False
    assert should_audit_source(item.source) is True

    sections = await runtime._build_pre_turn_context_sections(
        item,
        item.prompt,
        is_bridge_request=False,
    )
    assert sections == [("INTERACTION PRIORITIES", "Private Anatta guidance.")]

    runtime._schedule_post_turn_observers(
        item,
        item.prompt,
        "Core response influenced by Anatta.",
        is_bridge_request=False,
    )

    assert len(runtime._background_tasks) == 1
    await asyncio.gather(*list(runtime._background_tasks))
    assert runtime._background_tasks == set()
    assert layer.recorded[0][1] == "Core response influenced by Anatta."


@pytest.mark.asyncio
async def test_anatta_shadow_and_audit_mode_schedule_observation_without_injection():
    layer = _Layer("shadow")
    observer = AnattaPostTurnObserver(layer)
    runtime = _runtime_with_anatta(observer)
    item = _item("api")

    assert should_audit_source(item.source) is True
    sections = await runtime._build_pre_turn_context_sections(
        item,
        item.prompt,
        is_bridge_request=False,
    )
    assert sections == []

    runtime._schedule_post_turn_observers(
        item,
        item.prompt,
        "Audited visible response.",
        is_bridge_request=False,
    )

    assert len(runtime._background_tasks) == 1
    await asyncio.gather(*list(runtime._background_tasks))
    assert layer.recorded[0][1] == "Audited visible response."


@pytest.mark.asyncio
async def test_anatta_and_audit_skip_the_same_internal_sources():
    observer = AnattaPostTurnObserver(_Layer("on"))
    runtime = _runtime_with_anatta(observer)

    for source in ["startup", "system", "scheduler", "scheduler-skill", "loop_skill", "retry", "bridge:hchat"]:
        item = _item(source)
        sections = await runtime._build_pre_turn_context_sections(
            item,
            item.prompt,
            is_bridge_request=source.startswith("bridge:"),
        )

        assert should_audit_source(source) is False
        assert sections == []
        runtime._schedule_post_turn_observers(
            item,
            item.prompt,
            "Internal response.",
            is_bridge_request=source.startswith("bridge:"),
        )
        assert runtime._background_tasks == set()
