from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from orchestrator import runtime_pipeline


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)


class _ContextAssembler:
    def build_prompt_payload(self, prompt, backend, *, extra_sections, inject_memory, incremental):
        section_text = "\n".join(f"{key}: {value}" for key, value in extra_sections)
        return {
            "final_prompt": f"{prompt}\n{backend}\n{section_text}",
            "audit": {"sections": [{"key": key, "chars": len(value)} for key, value in extra_sections]},
        }


def _item(**overrides):
    payload = {
        "request_id": "req-1",
        "source": "text",
        "summary": "Test",
        "prompt": "Hello",
        "silent": False,
        "created_at": (datetime.now() - timedelta(seconds=3)).isoformat(),
        "skip_memory_injection": False,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _runtime():
    runtime = SimpleNamespace()
    runtime.config = SimpleNamespace(active_backend="codex-cli")
    runtime.logger = _Logger()
    runtime.last_prompt = None
    runtime.current_request_meta = None
    runtime.is_generating = False
    runtime.maintenance_events = []
    runtime._mark_activity = lambda: setattr(runtime, "activity_marked", True)
    runtime._log_maintenance = lambda item, event, **fields: runtime.maintenance_events.append((event, fields))
    runtime._consume_session_primer = lambda item: f"primer\n{item.prompt}"
    runtime._build_habit_sections = lambda item, prompt: ([("Habit", "Be precise.")], ["precision"])
    runtime._workzone_prompt_section = lambda: [("Workzone", "/tmp/work")]

    async def _build_pre_turn_context_sections(item, prompt, *, is_bridge_request):
        return [("Anatta", "Observe only" if is_bridge_request else "Guide")]

    runtime._build_pre_turn_context_sections = _build_pre_turn_context_sections
    runtime.backend_manager = SimpleNamespace(
        agent_mode="flex",
        current_backend=SimpleNamespace(_session_id=None),
    )
    runtime.context_assembler = _ContextAssembler()
    runtime._last_prompt_audit = {}
    runtime._thinking_chars_this_req = 99
    runtime._last_full_prompt_tokens = 0
    return runtime


def test_begin_queue_item_records_processing_metadata():
    runtime = _runtime()
    item = _item(source="bridge:api")

    start = runtime_pipeline.begin_queue_item(runtime, item)

    assert start.is_bridge_request is True
    assert start.queue_wait_s >= 0
    assert runtime.last_prompt is item
    assert runtime.current_request_meta["request_id"] == "req-1"
    assert runtime.current_request_meta["source"] == "bridge:api"
    assert runtime.is_generating is True
    assert runtime.maintenance_events[0][0] == "processing"


@pytest.mark.asyncio
async def test_build_turn_prompt_collects_context_sections_and_updates_audit_state():
    runtime = _runtime()
    item = _item()
    runtime.current_request_meta = {}

    prompt = await runtime_pipeline.build_turn_prompt(runtime, item, is_bridge_request=False)

    assert prompt.effective_prompt == "primer\nHello"
    assert prompt.habit_ids == ["precision"]
    assert prompt.extra_sections == [
        ("Workzone", "/tmp/work"),
        ("Habit", "Be precise."),
        ("Anatta", "Guide"),
    ]
    assert "codex-cli" in prompt.final_prompt
    assert runtime.current_request_meta["habit_ids"] == ["precision"]
    assert runtime._thinking_chars_this_req == 0
    assert runtime._last_full_prompt_tokens == len(prompt.final_prompt) // 4
