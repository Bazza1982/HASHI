"""Question answers must be durably available before the next provider call."""

from unittest.mock import AsyncMock

import pytest

from adapters.openrouter_api import ProviderContextObserverError, _APIResult
from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
from tests.test_deepseek_api import _adapter as deepseek_adapter
from tests.test_hashi_api import _adapter as hashi_adapter
from tests.test_her_v2_control_plane import _accept


class _AnsweredQuestion:
    def __init__(self):
        self.waits = 0

    def get_tool_definitions(self, tiers=None):
        return []

    async def wait_before_terminal_response(self):
        self.waits += 1
        if self.waits > 1:
            return None
        return {
            "type": "hashi.run_question.resolution",
            "questions": [{
                "question_id": "que-1",
                "state": "consumed",
                "answer": {
                    "option_id": "tea",
                    "text": "",
                    "author": "authenticated_user",
                    "grants_permissions": False,
                },
            }],
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("make_adapter", [deepseek_adapter, hashi_adapter])
@pytest.mark.parametrize("storage_available", [True, False])
async def test_answered_question_is_saved_before_provider_continues(
    tmp_path, make_adapter, storage_available
):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "question-turn", "Choose first")
    adapter = make_adapter(tmp_path)
    adapter.tool_registry = _AnsweredQuestion()
    seen_at_second_call = []

    def persist(checkpoint):
        has_resolution = any(
            "HASHI_RUN_QUESTION_RESOLUTION" in str(message.get("content") or "")
            for message in checkpoint["messages"]
        )
        if has_resolution and not storage_available:
            raise OSError("synthetic durable storage failure")
        coordinator.record_provider_context_checkpoint(
            turn,
            {
                **checkpoint,
                "provider": "test-provider",
                "model": "test-model",
                "stage": "execution",
            },
        )

    async def call(payload, *args):
        if adapter._call_api_once.await_count == 1:
            return _APIResult("premature", None, "stop", 10, 2)
        seen_at_second_call.append(
            coordinator.store.provider_context_for_turn(turn.session_id, turn.turn_id)
        )
        assert any(
            "HASHI_RUN_QUESTION_RESOLUTION" in str(message.get("content") or "")
            and '"option_id":"tea"' in str(message.get("content") or "")
            for message in payload["messages"]
        )
        return _APIResult("answer used", None, "stop", 10, 2)

    adapter.set_provider_context_observer(persist)
    adapter._call_api_once = AsyncMock(side_effect=call)

    if storage_available:
        response = await adapter.generate_response("Choose first", "question-turn")
        assert response.is_success is True
        assert response.text == "answer used"
        assert adapter._call_api_once.await_count == 2
        assert len(seen_at_second_call) == 1
        saved = seen_at_second_call[0]
        assert saved["boundary"] == "ready_provider_request"
        assert any(
            "HASHI_RUN_QUESTION_RESOLUTION" in str(message.get("content") or "")
            and '"option_id":"tea"' in str(message.get("content") or "")
            for message in saved["messages"]
        )
    else:
        with pytest.raises(ProviderContextObserverError):
            await adapter.generate_response("Choose first", "question-turn")
        assert adapter._call_api_once.await_count == 1
        assert not seen_at_second_call

    await adapter.shutdown()
