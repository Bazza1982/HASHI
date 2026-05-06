from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
import sys
import types

import pytest

from orchestrator import runtime_pipeline


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)

    def error(self, message):
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


class _MemoryStore:
    def __init__(self):
        self.turns = []
        self.exchanges = []

    def record_turn(self, role, source, text):
        self.turns.append((role, source, text))

    def record_exchange(self, user_text, assistant_text, source):
        self.exchanges.append((user_text, assistant_text, source))


class _HandoffBuilder:
    def __init__(self):
        self.transcript = []
        self.refreshed = False

    def append_transcript(self, role, text, source=None):
        self.transcript.append((role, text, source))

    def refresh_recent_context(self):
        self.refreshed = True


class _ProjectChatLogger:
    def __init__(self):
        self.exchanges = []

    def log_exchange(self, prompt, visible_text, source):
        self.exchanges.append((prompt, visible_text, source))


def _item(**overrides):
    payload = {
        "request_id": "req-1",
        "chat_id": 123,
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
    runtime.name = "zelda"
    runtime.workspace_dir = "/tmp/hashi-test"
    runtime.session_id_dt = "session-1"
    runtime.logger = _Logger()
    runtime.telegram_logger = _Logger()
    runtime.error_logger = _Logger()
    runtime.last_prompt = None
    runtime.current_request_meta = None
    runtime.is_generating = False
    runtime.maintenance_events = []
    runtime._mark_activity = lambda: setattr(runtime, "activity_marked", True)
    runtime._mark_error = lambda error: setattr(runtime, "last_error", error)
    runtime._log_maintenance = lambda item, event, **fields: runtime.maintenance_events.append((event, fields))
    runtime._safe_excerpt = lambda text, limit: text[:limit]
    runtime.success_marked = False
    runtime.habit_outcomes = []
    runtime.transcripts = []
    runtime.listener_payloads = []
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
    runtime._last_prompt_audit = {
        "sections": [{"key": "Workzone", "chars": 8, "tokens_est": 2, "item_count": 1}],
        "budget_applied": False,
        "context_fingerprint": "fp",
    }
    runtime._thinking_chars_this_req = 12
    runtime.get_current_model = lambda: "gpt-test"
    runtime._wrapper_audit_fields = lambda wrapper_result: {"wrapper_applied": bool(wrapper_result)}
    runtime.memory_store = _MemoryStore()
    runtime.handoff_builder = _HandoffBuilder()
    runtime.project_chat_logger = _ProjectChatLogger()
    runtime.post_turn_calls = []
    runtime._core_memory_assistant_text = lambda core_raw, visible_text, wrapper_result: f"memory:{visible_text}"
    runtime._schedule_post_turn_observers = (
        lambda item, user_text, assistant_text, is_bridge_request: runtime.post_turn_calls.append(
            (user_text, assistant_text, is_bridge_request)
        )
    )
    runtime._strip_transfer_accept_prefix = lambda item, text: text.removeprefix("ACCEPTED:")
    runtime._mark_success = lambda: setattr(runtime, "success_marked", True)
    runtime._record_habit_outcome = lambda item, **fields: runtime.habit_outcomes.append(fields)
    runtime._should_buffer_during_transfer = lambda request_id: False
    runtime._record_suppressed_transfer_result = lambda item, **fields: setattr(runtime, "suppressed", fields)
    runtime._should_retry_codex_scheduler_failure = lambda item, error: False
    runtime._schedule_codex_scheduler_retry = lambda item: setattr(runtime, "retry_scheduled", True)

    async def _send_long_message(**kwargs):
        runtime.sent_message = kwargs
        return 0.25, 1

    runtime.send_long_message = _send_long_message
    runtime._cos_enabled = False
    runtime.cos_queries = []

    async def _cos_query(text):
        runtime.cos_queries.append(text)
        return {"answered": False}

    runtime.cos_query = _cos_query
    runtime.wrapper_traces = []

    async def _send_wrapper_verbose_trace(item, core_raw, visible_text, wrapper_result):
        runtime.wrapper_traces.append((core_raw, visible_text, wrapper_result))

    runtime._send_wrapper_verbose_trace = _send_wrapper_verbose_trace
    runtime.voice_replies = []

    async def _send_voice_reply(chat_id, text, request_id):
        runtime.voice_replies.append((chat_id, text, request_id))
        return True

    runtime._send_voice_reply = _send_voice_reply
    runtime.audit_followups = []
    runtime._schedule_audit_followup = lambda item, **fields: runtime.audit_followups.append(fields)
    runtime.hchat_routes = []

    async def _hchat_route_reply(item, text):
        runtime.hchat_routes.append((item.request_id, text))

    runtime._hchat_route_reply = _hchat_route_reply

    async def _apply_wrapper_to_visible_text(item, text):
        return f"wrapped:{text}", {"mode": "wrapper"}

    runtime._apply_wrapper_to_visible_text = _apply_wrapper_to_visible_text
    runtime._append_core_transcript = lambda item, **fields: runtime.transcripts.append(fields)
    runtime._wrapper_listener_fields = lambda core_raw, visible_text, wrapper_result: {"wrapped": True}

    async def _notify_request_listeners(request_id, payload):
        runtime.listener_payloads.append(payload)

    runtime._notify_request_listeners = _notify_request_listeners
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


def test_log_backend_finished_records_structured_maintenance():
    runtime = _runtime()
    item = _item()
    response = SimpleNamespace(is_success=True, text="hello", error=None)

    runtime_pipeline.log_backend_finished(
        runtime,
        item,
        response,
        backend_elapsed_s=1.234,
        final_prompt="final prompt",
    )

    assert "Backend finished req-1" in runtime.logger.messages[0]
    event, fields = runtime.maintenance_events[0]
    assert event == "backend_finished"
    assert fields["elapsed_s"] == "1.23"
    assert fields["text_len"] == 5
    assert fields["final_prompt_len"] == len("final prompt")


@pytest.mark.asyncio
async def test_cleanup_interactive_feedback_stops_typing_and_deletes_placeholder():
    runtime = _runtime()
    deleted = []
    flushed = []
    stop_typing = None

    async def _typing_task():
        await stop_typing.wait()

    stop_typing = __import__("asyncio").Event()
    typing_task = __import__("asyncio").create_task(_typing_task())
    runtime._flush_thinking = lambda chat_id: flushed.append(chat_id)

    async def _delete_message(**kwargs):
        deleted.append(kwargs)

    runtime.app = SimpleNamespace(bot=SimpleNamespace(delete_message=_delete_message))
    placeholder = SimpleNamespace(message_id=99)

    await runtime_pipeline.cleanup_interactive_feedback(
        runtime,
        _item(),
        stop_typing=stop_typing,
        typing_task=typing_task,
        escalation_task=None,
        think_flush_task=None,
        placeholder=placeholder,
    )

    assert typing_task.done()
    assert deleted == [{"chat_id": 123, "message_id": 99}]
    assert flushed == []


@pytest.mark.asyncio
async def test_prepare_successful_response_applies_wrapper_and_notifies_listeners():
    runtime = _runtime()
    item = _item()
    response = SimpleNamespace(text="ACCEPTED:core text")

    result = await runtime_pipeline.prepare_successful_response(
        runtime,
        item,
        response,
        completion_path="foreground",
    )

    assert result.display_text == "core text"
    assert result.visible_text == "wrapped:core text"
    assert runtime.success_marked is True
    assert runtime.habit_outcomes == [{"success": True, "response_text": "ACCEPTED:core text"}]
    assert runtime.transcripts == [
        {
            "core_raw": "ACCEPTED:core text",
            "visible_text": "wrapped:core text",
            "completion_path": "foreground",
            "wrapper_result": {"mode": "wrapper"},
        }
    ]
    assert runtime.listener_payloads[0]["text"] == "wrapped:core text"
    assert runtime.listener_payloads[0]["wrapped"] is True


def test_record_foreground_usage_audit_records_estimated_usage(monkeypatch):
    runtime = _runtime()
    item = _item()
    usage_records = []
    audit_records = []
    fake_module = types.SimpleNamespace(
        estimate_tokens=lambda text: len(text) // 2,
        record_usage=lambda *args, **kwargs: usage_records.append((args, kwargs)),
        record_audit_event=lambda *args, **kwargs: audit_records.append((args, kwargs)),
    )
    monkeypatch.setitem(sys.modules, "tools.token_tracker", fake_module)
    response = SimpleNamespace(
        is_success=True,
        text="core text",
        usage=None,
        tool_call_count=2,
        tool_loop_count=1,
    )

    runtime_pipeline.record_foreground_usage_audit(
        runtime,
        item,
        response,
        visible_text="visible text",
        wrapper_result={"mode": "wrapper"},
        final_prompt="final prompt",
        effective_prompt="effective prompt",
        incremental=False,
    )

    assert usage_records[0][1]["input_tokens"] == len("final prompt") // 2
    assert usage_records[0][1]["output_tokens"] == len("visible text") // 2
    event = audit_records[0][0][1]
    assert event["request_id"] == "req-1"
    assert event["token_source"] == "estimated"
    assert event["section_chars"] == {"Workzone": 8}
    assert event["wrapper_applied"] is True


def test_persist_success_memory_records_human_exchange_and_handoff():
    runtime = _runtime()
    item = _item(prompt="user text", source="text")
    response = SimpleNamespace(text="core text")

    runtime_pipeline.persist_success_memory(
        runtime,
        item,
        response,
        visible_text="visible text",
        wrapper_result={"mode": "wrapper"},
        is_bridge_request=False,
        session_reset_source="session_reset",
    )

    assert runtime.memory_store.turns == [
        ("user", "text", "user text"),
        ("assistant", "codex-cli", "memory:visible text"),
    ]
    assert runtime.memory_store.exchanges == [("user text", "memory:visible text", "text")]
    assert runtime.post_turn_calls == [("user text", "memory:visible text", False)]
    assert runtime.handoff_builder.transcript == [
        ("user", "user text", "text"),
        ("assistant", "visible text", None),
    ]
    assert runtime.handoff_builder.refreshed is True
    assert runtime.project_chat_logger.exchanges == [("user text", "visible text", "text")]


def test_persist_success_memory_skips_bridge_memory_and_handoff():
    runtime = _runtime()
    item = _item(source="bridge:api")

    runtime_pipeline.persist_success_memory(
        runtime,
        item,
        SimpleNamespace(text="core"),
        visible_text="visible",
        wrapper_result=None,
        is_bridge_request=True,
        session_reset_source="session_reset",
    )

    assert runtime.memory_store.turns == []
    assert runtime.handoff_builder.transcript == []
    assert runtime.project_chat_logger.exchanges == []


@pytest.mark.asyncio
async def test_handle_backend_error_notifies_and_delivers_error():
    runtime = _runtime()
    item = _item()
    response = SimpleNamespace(error="backend failed")

    await runtime_pipeline.handle_backend_error(
        runtime,
        item,
        response,
        queued_at=datetime.now() - timedelta(seconds=1),
        queue_wait_s=0.5,
        backend_elapsed_s=0.25,
    )

    assert runtime.last_error == "backend failed"
    assert runtime.habit_outcomes == [{"success": False, "error_text": "backend failed"}]
    assert runtime.listener_payloads[0]["success"] is False
    assert runtime.listener_payloads[0]["error"] == "backend failed"
    assert runtime.sent_message["purpose"] == "error"
    assert "Flex Backend Error" in runtime.sent_message["text"]
    assert runtime.maintenance_events[0][0] == "send_error"


@pytest.mark.asyncio
async def test_handle_backend_error_buffers_transfer_without_delivery():
    runtime = _runtime()
    runtime._should_buffer_during_transfer = lambda request_id: True
    item = _item()

    await runtime_pipeline.handle_backend_error(
        runtime,
        item,
        SimpleNamespace(error="buffer me"),
        queued_at=datetime.now(),
        queue_wait_s=0,
        backend_elapsed_s=0,
    )

    assert runtime.suppressed == {"success": False, "error": "buffer me"}
    assert not hasattr(runtime, "sent_message")


@pytest.mark.asyncio
async def test_handle_success_delivery_sends_response_and_routes_hchat():
    runtime = _runtime()
    item = _item(prompt="user text")
    response = SimpleNamespace(text="core text")

    await runtime_pipeline.handle_success_delivery(
        runtime,
        item,
        response,
        visible_text="visible text",
        wrapper_result={"mode": "wrapper"},
        is_bridge_request=False,
        session_reset_source="session_reset",
        queued_at=datetime.now() - timedelta(seconds=1),
        queue_wait_s=0.2,
        backend_elapsed_s=0.3,
        audit_collector="audit",
    )

    assert runtime.last_response["text"] == "visible text"
    assert runtime.sent_message["text"] == "visible text"
    assert runtime.voice_replies == [(123, "visible text", "req-1")]
    assert runtime.audit_followups[0]["audit_collector"] == "audit"
    assert runtime.hchat_routes == [("req-1", "visible text")]
    assert runtime.maintenance_events[-1][0] == "send_success"


@pytest.mark.asyncio
async def test_handle_success_delivery_uses_cos_answer_without_hchat_route():
    runtime = _runtime()
    runtime._cos_enabled = True

    async def _cos_query(text):
        return {"answered": True, "response": "cos answer"}

    runtime.cos_query = _cos_query

    await runtime_pipeline.handle_success_delivery(
        runtime,
        _item(),
        SimpleNamespace(text="core?"),
        visible_text="visible?",
        wrapper_result=None,
        is_bridge_request=False,
        session_reset_source="session_reset",
        queued_at=datetime.now(),
        queue_wait_s=0,
        backend_elapsed_s=0,
        audit_collector=None,
    )

    assert runtime.sent_message["text"] == "cos answer"
    assert runtime.hchat_routes == [("req-1", "cos answer")]
