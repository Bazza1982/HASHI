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


class _BackendManager:
    def __init__(self, response=None, delay_s: float = 0.0):
        self.agent_mode = "flex"
        self.current_backend = SimpleNamespace(_session_id=None)
        self.response = response or SimpleNamespace(is_success=True, text="ok")
        self.delay_s = delay_s
        self.calls = []

    async def generate_response(self, final_prompt, request_id, *, is_retry, silent, on_stream_event):
        import asyncio

        self.calls.append(
            {
                "final_prompt": final_prompt,
                "request_id": request_id,
                "is_retry": is_retry,
                "silent": silent,
                "on_stream_event": on_stream_event,
            }
        )
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return self.response


def _item(**overrides):
    payload = {
        "request_id": "req-1",
        "source": "text",
        "summary": "Test",
        "prompt": "Hello",
        "silent": False,
        "created_at": (datetime.now() - timedelta(seconds=3)).isoformat(),
        "skip_memory_injection": False,
        "deliver_to_telegram": True,
        "is_retry": False,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _runtime():
    runtime = SimpleNamespace()
    runtime.config = SimpleNamespace(active_backend="codex-cli", extra={})
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
    runtime.backend_manager = _BackendManager()
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


@pytest.mark.asyncio
async def test_run_backend_generation_returns_foreground_response():
    runtime = _runtime()
    item = _item()

    generation = await runtime_pipeline.run_backend_generation(
        runtime,
        item,
        "final",
        on_stream_event=None,
        audit_active=False,
    )

    assert generation.detached is False
    assert generation.response.text == "ok"
    assert runtime.backend_manager.calls[0]["final_prompt"] == "final"
    assert runtime.is_generating is False


@pytest.mark.asyncio
async def test_run_backend_generation_detaches_background_task():
    runtime = _runtime()
    runtime.config.extra = {"background_mode": True, "background_detach_after": 0.01}
    runtime.backend_manager = _BackendManager(response=SimpleNamespace(is_success=True, text="late"), delay_s=0.05)
    item = _item()

    generation = await runtime_pipeline.run_backend_generation(
        runtime,
        item,
        "final",
        on_stream_event=None,
        audit_active=False,
    )

    assert generation.detached is True
    assert generation.response is None
    assert generation.generation_task is not None
    assert runtime.is_generating is False
    assert (await generation.generation_task).text == "late"
