from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from orchestrator.anatta.models import EmergentTurnState, PromptInjection
from orchestrator.anatta.post_turn_observer import AnattaPostTurnObserver
from orchestrator.post_turn_observer import TurnContextRequest, TurnObservationRequest


class _Bridge:
    def get_recent_turns(self, limit: int = 8):
        return []

    def retrieve_memories(self, text: str, limit: int = 6):
        return []


class _Layer:
    def __init__(self, mode: str = "on"):
        self.bridge_adapter = _Bridge()
        self._mode = mode
        self.build_count = 0
        self.recorded_state = None
        self.recorded_text = None

    def mode(self):
        return self._mode

    def should_inject_prompt(self):
        return True

    def resolve_relationship_key(self, source, chat_id=None, metadata=None):
        return f"{source}:{chat_id}"

    async def build_turn_state(self, turn_context, model_name):
        self.build_count += 1
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
            body="Private response guidance for this turn.",
            metadata={"inject": True},
        )

    async def record_async(self, turn_context, assistant_response, state):
        self.recorded_state = state
        self.recorded_text = assistant_response


@pytest.mark.asyncio
async def test_anatta_pre_turn_provider_injects_and_reuses_cached_state():
    layer = _Layer("on")
    observer = AnattaPostTurnObserver(layer)

    sections = await observer.build_context_sections(
        TurnContextRequest(
            request_id="req-1",
            source="api",
            user_text="Explore this.",
            model_name="gpt-5.4",
            chat_id=123,
        )
    )
    assert sections == [("INTERACTION PRIORITIES", "Private response guidance for this turn.")]

    await observer._run_observation(
        TurnObservationRequest(
            request_id="req-1",
            source="api",
            user_text="Explore this.",
            assistant_text="Response.",
            model_name="gpt-5.4",
            chat_id=123,
        ),
        mode="on",
    )

    assert layer.build_count == 1
    assert layer.recorded_text == "Response."
    assert layer.recorded_state is not None
    assert layer.recorded_state.dominant_drives == ["SEEKING"]


def test_anatta_source_policy_skips_internal_sources():
    observer = AnattaPostTurnObserver(_Layer("on"))

    for source in ["startup", "system", "scheduler", "scheduler-skill", "loop_skill", "retry", "hchat-reply:akane"]:
        assert not observer.should_observe(source, is_bridge_request=False)
        assert not observer.should_provide(source, is_bridge_request=False)

    assert not observer.should_observe("api", is_bridge_request=True)
    assert not observer.should_provide("api", is_bridge_request=True)


@pytest.mark.asyncio
async def test_anatta_schedule_observation_tracks_background_task():
    layer = _Layer("shadow")
    observer = AnattaPostTurnObserver(layer)
    background_tasks = set()

    observer.schedule_observation(
        TurnObservationRequest(
            request_id="req-2",
            source="api",
            user_text="Explore this.",
            assistant_text="Shadow response.",
            model_name="gpt-5.4",
            chat_id=123,
        ),
        background_tasks,
    )

    assert len(background_tasks) == 1
    await asyncio.gather(*list(background_tasks))
    assert background_tasks == set()
    assert layer.recorded_text == "Shadow response."


def test_anatta_workspace_files_to_preserve_keeps_config():
    observer = AnattaPostTurnObserver(_Layer("off"))

    assert observer.workspace_files_to_preserve() == frozenset({"anatta_config.json"})
