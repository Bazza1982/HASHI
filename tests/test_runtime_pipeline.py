from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import RetryAfter

from adapters.her import (
    HER_COMMENTARY_EFFORTS,
    _claw_jsonl_to_stream_events,
    _HERStreamCadenceController,
)
from adapters.stream_events import (
    DELIVERY_CONTROL,
    DELIVERY_FINAL,
    DELIVERY_TECHNICAL,
    DELIVERY_USER_COMMENTARY,
    KIND_ACKNOWLEDGEMENT,
    KIND_COMMENTARY,
    KIND_PROGRESS,
    KIND_REVIEW,
    KIND_TEXT_DELTA,
    StreamEvent,
)
from orchestrator import (
    runtime_cross_session,
    runtime_pipeline,
    runtime_retry,
    telegram_stream_policy,
)
from orchestrator import telegram_delivery_failover as failover


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)

    def warning(self, message):
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
        self.current_backend = SimpleNamespace(
            _session_id=None,
            capabilities=SimpleNamespace(
                supports_thinking_stream=True,
                supports_progress_stream=True,
                supports_tool_stream=True,
                supports_answer_stream=True,
            ),
        )
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


class _Bot:
    def __init__(self, *, edit_error=None, send_error=None):
        self.sent = []
        self.edits = []
        self.deleted = []
        self.edit_error = edit_error
        self.send_error = send_error

    async def send_message(self, **kwargs):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=77)

    async def edit_message_text(self, **kwargs):
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(kwargs)

    async def delete_message(self, **kwargs):
        self.deleted.append(kwargs)


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
    runtime.config = SimpleNamespace(
        active_backend="codex-cli",
        extra={"telegram_stream_enabled": True},
    )
    runtime.name = "zelda"
    runtime.config.telegram_token_key = runtime.name
    runtime.global_config = SimpleNamespace(project_root=Path(tempfile.mkdtemp(prefix="hashi-pipeline-test-")))
    runtime.workspace_dir = runtime.global_config.project_root / "workspaces" / runtime.name
    runtime.workspace_dir.mkdir(parents=True, exist_ok=True)
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
    runtime.transcripts = []
    runtime.listener_payloads = []
    runtime._consume_session_primer = lambda item: f"primer\n{item.prompt}"
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
    runtime.app = SimpleNamespace(bot=_Bot())
    runtime.get_typing_placeholder = lambda: ("typing", None)
    runtime.get_progress_placeholder = lambda: ("working", None)
    runtime._verbose = False
    runtime._think = False
    runtime._think_buffer = []
    runtime._openrouter_think_chunk = ""
    runtime._last_openrouter_think_snippet = None
    runtime.stream_callbacks = []
    runtime.escalating_loops = []
    runtime.streaming_loops = []

    async def _typing_loop(chat_id, stop_typing):
        await stop_typing.wait()

    runtime.typing_loop = _typing_loop

    async def _escalating_placeholder_loop(chat_id, placeholder, request_id, stop_typing, *, backend):
        runtime.escalating_loops.append(request_id)
        await stop_typing.wait()

    runtime._escalating_placeholder_loop = _escalating_placeholder_loop

    async def _streaming_display_loop(chat_id, placeholder, request_id, stop_typing, stream_queue, *, backend):
        runtime.streaming_loops.append((request_id, stream_queue is not None))
        await stop_typing.wait()

    runtime._streaming_display_loop = _streaming_display_loop

    async def _thinking_flush_loop(chat_id, stop_typing):
        await stop_typing.wait()

    runtime._thinking_flush_loop = _thinking_flush_loop

    def _make_stream_callback(**kwargs):
        runtime.stream_callbacks.append(kwargs)
        return ("stream", kwargs)

    runtime._make_stream_callback = _make_stream_callback
    runtime.post_turn_calls = []
    runtime._core_memory_assistant_text = lambda core_raw, visible_text, wrapper_result: f"memory:{visible_text}"
    runtime._schedule_post_turn_observers = (
        lambda item, user_text, assistant_text, is_bridge_request: runtime.post_turn_calls.append(
            (user_text, assistant_text, is_bridge_request)
        )
    )
    runtime._strip_transfer_accept_prefix = lambda item, text: text.removeprefix("ACCEPTED:")
    runtime._mark_success = lambda: setattr(runtime, "success_marked", True)
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


def _set_stream_policy(runtime, **values):
    for name, enabled in values.items():
        telegram_stream_policy.set_policy_value(runtime, name, enabled)


def test_begin_queue_item_records_processing_metadata():
    runtime = _runtime()
    item = _item(source="bridge:api")

    start = runtime_pipeline.begin_queue_item(runtime, item)

    assert start.is_bridge_request is True
    assert start.queue_wait_s >= 0
    assert runtime.last_prompt is item
    assert runtime.current_request_meta["request_id"] == "req-1"
    assert runtime.current_request_meta["source"] == "bridge:api"
    assert runtime.current_request_meta["verbose_at_start"] is False
    assert runtime.current_request_meta["silent"] is False
    assert runtime.current_request_meta["deliver_to_telegram"] is True
    assert runtime.current_request_meta["habit_learning_eligible"] is True
    assert runtime.is_generating is True
    assert runtime.maintenance_events[0][0] == "processing"


def test_begin_queue_item_preserves_explicit_habit_ineligibility():
    runtime = _runtime()
    item = _item(habit_learning_eligible=False)

    runtime_pipeline.begin_queue_item(runtime, item)

    assert runtime.current_request_meta["habit_learning_eligible"] is False
    assert (
        runtime._request_meta_by_id[item.request_id]["habit_learning_eligible"]
        is False
    )


def test_begin_queue_item_uses_monotonic_queue_age(monkeypatch):
    runtime = _runtime()
    item = _item(queued_monotonic=40.0)
    monkeypatch.setattr(runtime_pipeline.time, "monotonic", lambda: 42.5)

    start = runtime_pipeline.begin_queue_item(runtime, item)

    assert start.queued_monotonic == 40.0
    assert start.queue_wait_s == pytest.approx(2.5)


def test_queued_elapsed_uses_monotonic_clock(monkeypatch):
    item = _item(queued_monotonic=40.0)
    monkeypatch.setattr(runtime_pipeline.time, "monotonic", lambda: 43.0)

    assert runtime_pipeline.queued_elapsed_s(item) == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_build_turn_prompt_collects_context_sections_and_updates_audit_state():
    runtime = _runtime()
    item = _item()
    runtime.current_request_meta = {}

    prompt = await runtime_pipeline.build_turn_prompt(runtime, item, is_bridge_request=False)

    assert prompt.effective_prompt == "primer\nHello"
    assert prompt.extra_sections == [
        ("Workzone", "/tmp/work"),
        ("Anatta", "Guide"),
    ]
    assert "codex-cli" in prompt.final_prompt
    assert runtime._thinking_chars_this_req == 0
    assert runtime._last_full_prompt_tokens == len(prompt.final_prompt) // 4


@pytest.mark.asyncio
async def test_scheduler_her_turn_uses_isolated_full_context_session():
    runtime = _runtime()
    runtime.config.active_backend = "her"
    runtime.backend_manager.agent_mode = "fixed"
    runtime.backend_manager.current_backend = SimpleNamespace(
        _session_id="persistent-session",
        persistent_session_busy=False,
        capabilities=SimpleNamespace(
            supports_sessions=True,
            supports_thinking_stream=True,
        ),
    )
    item = _item(source="scheduler")

    runtime_pipeline.begin_queue_item(runtime, item)
    prompt = await runtime_pipeline.build_turn_prompt(
        runtime,
        item,
        is_bridge_request=False,
    )

    assert runtime.current_request_meta["session_scope"] == "isolated_per_run"
    assert runtime._request_meta_by_id[item.request_id]["session_scope"] == "isolated_per_run"
    assert prompt.incremental is False


@pytest.mark.asyncio
async def test_new_direct_her_turn_waits_for_busy_persistent_session():
    runtime = _runtime()
    runtime.config.active_backend = "her"
    runtime.backend_manager.agent_mode = "fixed"
    runtime.backend_manager.current_backend = SimpleNamespace(
        _session_id="persistent-session",
        persistent_session_busy=True,
        capabilities=SimpleNamespace(
            supports_sessions=True,
            supports_thinking_stream=True,
        ),
    )
    item = _item(source="text")

    runtime_pipeline.begin_queue_item(runtime, item)
    prompt = await runtime_pipeline.build_turn_prompt(
        runtime,
        item,
        is_bridge_request=False,
    )

    assert runtime.current_request_meta["session_scope"] == "persistent"
    assert prompt.incremental is True


@pytest.mark.asyncio
async def test_build_turn_prompt_binds_bare_continue_to_persisted_stopped_task():
    runtime = _runtime()
    original_item = _item(
        request_id="req-original",
        prompt="Research the topic deeply and write the requested report",
        summary="Deep research",
    )
    runtime_pipeline.begin_queue_item(runtime, original_item)
    runtime_retry.remember_interrupted_task(
        runtime,
        runtime.current_request_meta,
        backend="her",
    )

    continuation = _item(
        request_id="req-continue",
        prompt="You can continue now",
        summary="Continue",
    )
    runtime_pipeline.begin_queue_item(runtime, continuation)
    prompt = await runtime_pipeline.build_turn_prompt(
        runtime,
        continuation,
        is_bridge_request=False,
    )

    assert "[HASHI /stop continuation" in prompt.effective_prompt
    assert original_item.prompt in prompt.effective_prompt
    assert "You can continue now" in prompt.effective_prompt
    assert runtime.current_request_meta["resumed_interrupted_task"]["request_id"] == "req-original"
    assert continuation._resumed_interrupted_task["prompt"] == original_item.prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fixed", "flex"])
async def test_build_turn_prompt_prefers_newer_scheduler_receipt_over_stopped_task(mode):
    runtime = _runtime()
    runtime.config.active_backend = "her"
    runtime.backend_manager.agent_mode = mode
    runtime.backend_manager.current_backend = SimpleNamespace(
        _session_id="primary-session",
        _claw_model=lambda: "local/deepseek-v4-pro",
        persistent_session_busy=False,
        capabilities=SimpleNamespace(
            supports_sessions=True,
            supports_thinking_stream=True,
        ),
    )
    runtime_retry.remember_interrupted_task(
        runtime,
        {
            "request_id": "req-stopped",
            "chat_id": 123,
            "prompt": "Older primary-session task",
            "source": "text",
            "summary": "Older task",
        },
        backend="her",
    )
    scheduler_item = _item(
        request_id="req-scheduler",
        source="scheduler",
        prompt="Run the newer scheduled task",
        summary="Cron Task [newer]",
    )
    scheduler_response = SimpleNamespace(
        is_success=True,
        stop_reason="max_iterations",
        stream_metadata={
            "claw_completion_status": "incomplete",
            "claw_stop_reason": "max_iterations",
            "recommended_action": "continue",
            "her_session_scope": "isolated_per_run",
            "her_session_id": "scheduler-session",
            "her_model": "local/deepseek-v4-pro",
        },
    )
    runtime_cross_session.record_turn_result(
        runtime,
        scheduler_item,
        assistant_text="Newer scheduler task is incomplete. CONTINUE.",
        response=scheduler_response,
        delivered=True,
        completion_path="foreground",
    )
    continuation = _item(
        request_id="req-continue",
        prompt="continue.",
        summary="Continue",
    )
    runtime_pipeline.begin_queue_item(runtime, continuation)

    prompt = await runtime_pipeline.build_turn_prompt(
        runtime,
        continuation,
        is_bridge_request=False,
    )

    assert "HASHI cross-session reply binding" in prompt.effective_prompt
    assert "Newer scheduler task is incomplete" in prompt.effective_prompt
    assert "HASHI /stop continuation" not in prompt.effective_prompt
    assert runtime.current_request_meta["session_scope"] == "isolated_resume"
    assert runtime.current_request_meta["resume_session_id"] == "scheduler-session"
    assert prompt.incremental is True


@pytest.mark.asyncio
async def test_build_turn_prompt_leaves_unrelated_request_unchanged_with_stopped_task_saved():
    runtime = _runtime()
    runtime_retry.remember_interrupted_task(
        runtime,
        {
            "request_id": "req-original",
            "chat_id": 123,
            "prompt": "Finish the original task",
            "source": "text",
            "summary": "Original",
        },
        backend="her",
    )
    item = _item(prompt="Write a new report")
    runtime_pipeline.begin_queue_item(runtime, item)

    prompt = await runtime_pipeline.build_turn_prompt(runtime, item, is_bridge_request=False)

    assert prompt.effective_prompt == "primer\nWrite a new report"
    assert "resumed_interrupted_task" not in runtime.current_request_meta


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "supports_sessions", "session_id", "expected_profile", "expected_incremental"),
    [
        ("fixed", True, "cli-session", "memory_plus_session", True),
        ("flex", False, None, "memory_plus_stateless", False),
    ],
)
async def test_build_turn_prompt_routes_memory_plus_by_backend_capability(
    mode,
    supports_sessions,
    session_id,
    expected_profile,
    expected_incremental,
):
    runtime = _runtime()
    item = _item()
    runtime.current_request_meta = {}
    (runtime.workspace_dir / "state.json").write_text(
        json.dumps(
            {
                "agent_mode": mode,
                "memory_plus": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )
    runtime.backend_manager.agent_mode = mode
    runtime.backend_manager.current_backend = SimpleNamespace(
        _session_id=session_id,
        capabilities=SimpleNamespace(
            supports_sessions=supports_sessions,
            supports_thinking_stream=True,
        ),
    )
    observed: dict[str, object] = {}

    async def build_sections(
        item,
        prompt,
        *,
        is_bridge_request,
        metadata,
    ):
        observed["metadata"] = metadata
        return [("Memory+ Continuity", "compact card")]

    class Assembler:
        def build_prompt_payload(
            self,
            prompt,
            backend,
            *,
            extra_sections,
            inject_memory,
            incremental,
            context_profile,
        ):
            observed["profile"] = context_profile
            observed["incremental"] = incremental
            return {
                "final_prompt": prompt,
                "audit": {"sections": []},
            }

    runtime._build_pre_turn_context_sections = build_sections
    runtime.context_assembler = Assembler()

    await runtime_pipeline.build_turn_prompt(runtime, item, is_bridge_request=False)

    assert observed["profile"] == expected_profile
    assert observed["incremental"] is expected_incremental
    assert observed["metadata"]["supports_sessions"] is supports_sessions
    assert observed["metadata"]["incremental"] is expected_incremental


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

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_typing_task())
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
async def test_cleanup_interactive_feedback_can_leave_stream_owned_placeholder():
    runtime = _runtime()
    stop_typing = asyncio.Event()

    async def _typing_task():
        await stop_typing.wait()

    typing_task = asyncio.create_task(_typing_task())
    runtime._flush_thinking = lambda chat_id: None
    placeholder = SimpleNamespace(message_id=99)

    await runtime_pipeline.cleanup_interactive_feedback(
        runtime,
        _item(),
        stop_typing=stop_typing,
        typing_task=typing_task,
        escalation_task=None,
        think_flush_task=None,
        placeholder=placeholder,
        delete_placeholder=False,
    )

    assert typing_task.done()
    assert runtime.app.bot.deleted == []


@pytest.mark.asyncio
async def test_setup_interactive_feedback_creates_placeholder_and_cleanup_tasks():
    runtime = _runtime()
    telegram_stream_policy.set_typing_enabled(runtime, True)

    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )

    assert runtime.app.bot.sent == [
        {"chat_id": 123, "text": "typing", "parse_mode": None, "disable_notification": True}
    ]
    assert feedback.placeholder.message_id == 77
    assert feedback.typing_task is not None
    assert feedback.escalation_task is None
    assert feedback.answer_preview_task is None
    assert feedback.answer_stream_state is None
    assert feedback.on_stream_event is None
    feedback.stop_typing.set()
    await feedback.typing_task


@pytest.mark.asyncio
async def test_medium_claw_sends_each_task_acknowledgement_event_once():
    runtime = _runtime()
    runtime.config.active_backend = "her"
    runtime.backend_manager.current_backend.effort = "medium"
    sent = []

    async def _send_text(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=len(sent))

    runtime._send_text = _send_text
    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )

    assert feedback.on_stream_event is not None
    event = StreamEvent(
        kind=KIND_ACKNOWLEDGEMENT,
        summary="I will inspect the requested logs and report only the findings.",
        event_id="req-1:ack:initial",
        delivery_class=DELIVERY_USER_COMMENTARY,
        origin="her_planner",
        phase="initial",
    )
    await feedback.on_stream_event(event)
    await feedback.on_stream_event(event)

    assert len(sent) == 1
    assert sent[0][0] == 123
    assert sent[0][2]["_purpose"] == "task_acknowledgement"
    assert any("acknowledgement policy" in message for message in runtime.logger.messages)
    assert any(
        "acknowledgement accepted by transport" in message
        for message in runtime.logger.messages
    )


@pytest.mark.asyncio
async def test_her_message_audit_preserves_exact_commentary_and_transport_status():
    runtime = _runtime()
    runtime.config.active_backend = "her"
    runtime.backend_manager.current_backend.effort = "ultra"
    exact_text = "了解しました。計画の作成を開始しますね。\n次の確認段階で報告します。"

    async def _send_text(_chat_id, _text, **_kwargs):
        return SimpleNamespace(message_id=91)

    runtime._send_text = _send_text
    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )
    await feedback.on_stream_event(
        StreamEvent(
            kind=KIND_COMMENTARY,
            summary=exact_text,
            event_id="ultra-run:persona:planning:started",
            delivery_class=DELIVERY_USER_COMMENTARY,
            origin="her_ultra",
            phase="planning",
            provenance="persona_renderer",
            detail="persona_renderer_fallback=false",
        )
    )

    audit_path = runtime.workspace_dir / "backend_state" / "her_message_audit.jsonl"
    records = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["status"] for record in records] == [
        "generated",
        "transport_accepted",
    ]
    assert records[0]["text"] == exact_text
    assert records[0]["text_sha256"] == records[1]["text_sha256"]
    assert records[0]["provenance"] == "persona_renderer"
    assert records[0]["detail"] == "persona_renderer_fallback=false"
    assert audit_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_her_message_audit_does_not_report_missing_transport_receipt_as_sent():
    runtime = _runtime()
    runtime.config.active_backend = "her"
    runtime.backend_manager.current_backend.effort = "high"

    async def _send_text(_chat_id, _text, **_kwargs):
        return None

    runtime._send_text = _send_text
    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )
    await feedback.on_stream_event(
        StreamEvent(
            kind=KIND_COMMENTARY,
            summary="Still working.",
            event_id="req-1:commentary:1",
            delivery_class=DELIVERY_USER_COMMENTARY,
            origin="her_planner",
            phase="execution",
        )
    )

    records = [
        json.loads(line)
        for line in (
            runtime.workspace_dir / "backend_state" / "her_message_audit.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [record["status"] for record in records] == ["generated", "not_sent"]
    assert not any(
        "task_commentary accepted by transport" in message
        for message in runtime.logger.messages
    )


@pytest.mark.asyncio
async def test_medium_claw_acknowledgement_composes_with_request_activity():
    runtime = _runtime()
    runtime.config.active_backend = "her"
    runtime.backend_manager.current_backend.effort = "medium"
    sent = []
    published = []

    async def _send_text(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))

    runtime._send_text = _send_text
    runtime.request_activity = SimpleNamespace(
        publish_stream=lambda request_id, event: published.append((request_id, event.kind))
    )
    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )

    await feedback.on_stream_event(
        StreamEvent(
            kind=KIND_PROGRESS,
            summary="Task started",
            event_id="req-1:technical:start",
            delivery_class=DELIVERY_TECHNICAL,
            origin="her_runtime",
            phase="initial",
        )
    )
    await feedback.on_stream_event(
        StreamEvent(
            kind=KIND_ACKNOWLEDGEMENT,
            summary="I will inspect the requested logs.",
            event_id="req-1:ack:initial",
            delivery_class=DELIVERY_USER_COMMENTARY,
            origin="her_planner",
            phase="initial",
        )
    )

    assert published == [
        ("req-1", KIND_PROGRESS),
        ("req-1", KIND_ACKNOWLEDGEMENT),
    ]
    assert len(sent) == 1
    assert sent[0][2]["_purpose"] == "task_acknowledgement"


@pytest.mark.asyncio
async def test_high_her_commentary_delivers_independently_of_think_and_verbose():
    runtime = _runtime()
    runtime.config.active_backend = "her"
    runtime.backend_manager.current_backend.effort = "high"
    runtime._commentary = True
    runtime._think = False
    runtime._verbose = False
    telegram_stream_policy.set_typing_enabled(runtime, False)
    sent = []

    async def _send_text(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))

    runtime._send_text = _send_text
    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )

    assert feedback.on_stream_event is not None
    await feedback.on_stream_event(
        StreamEvent(
            kind=KIND_COMMENTARY,
            summary="Sunny is still checking the verified results. ☀️",
            event_id="req-1:commentary:replan:1",
            delivery_class=DELIVERY_USER_COMMENTARY,
            origin="her_planner",
            phase="replan",
            revision=1,
        )
    )

    assert len(sent) == 1
    assert sent[0][0] == 123
    assert sent[0][2]["_purpose"] == "task_commentary"


@pytest.mark.asyncio
async def test_pending_her_commentary_superseded_by_final_is_audited_not_sent():
    runtime = _runtime()
    runtime.config.active_backend = "her"
    runtime.backend_manager.current_backend.effort = "max+"
    runtime._commentary = True
    sent = []

    async def _send_text(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=len(sent))

    runtime._send_text = _send_text
    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )
    await feedback.on_stream_event(
        StreamEvent(
            kind=KIND_COMMENTARY,
            summary="Sunny reached a checkpoint that the final answer now replaces. ☀️",
            detail="suppressed_reason=superseded_by_final",
            event_id="req-1:commentary:final-pending",
            delivery_class="internal",
            origin="her_planner",
            phase="finalization",
            revision=3,
        )
    )

    assert sent == []
    records = [
        json.loads(line)
        for line in (
            runtime.workspace_dir / "backend_state" / "her_message_audit.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [record["status"] for record in records] == [
        "generated",
        "superseded",
    ]
    assert records[-1]["reason"] == "superseded_by_final"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effort", "expected_purposes"),
    [
        ("low", []),
        ("medium", ["task_acknowledgement"]),
        ("high", ["task_acknowledgement", "task_commentary"]),
        ("xhigh", ["task_acknowledgement", "task_commentary"]),
        ("max", ["task_acknowledgement", "task_commentary"]),
        ("max+", ["task_acknowledgement", "task_commentary"]),
        ("ultra", ["task_commentary"]),
    ],
)
async def test_her_effort_commentary_matrix_reaches_transport_receipt(
    effort, expected_purposes
):
    runtime = _runtime()
    runtime.config.active_backend = "her"
    runtime.backend_manager.current_backend.effort = effort
    runtime._commentary = True
    runtime._think = False
    runtime._verbose = False
    telegram_stream_policy.set_typing_enabled(runtime, False)
    sent = []

    async def _send_text(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=len(sent))

    runtime._send_text = _send_text
    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )

    if effort == "ultra":
        await feedback.on_stream_event(
            StreamEvent(
                kind=KIND_COMMENTARY,
                summary="Sunny has accepted the Ultra plan. ☀️",
                event_id="ultra:persona:plan_accepted",
                delivery_class=DELIVERY_USER_COMMENTARY,
                origin="her_ultra",
                phase="plan_accepted",
                provenance="persona_renderer",
            )
        )
    elif effort != "low":
        controller = _HERStreamCadenceController(
            feedback.on_stream_event,
            request_id="req-1",
            prompt="Inspect and report.",
            progress_enabled=effort in HER_COMMENTARY_EFFORTS,
            first_update_s=0.001,
            target_interval_s=0.002,
            hard_interval_s=0.003,
            activity_grace_s=0,
        )
        cadence_task = (
            asyncio.create_task(controller.run())
            if controller.progress_enabled
            else None
        )
        raw_events = [
            {
                "kind": "task_acknowledgement",
                "event_id": "task-acknowledgement:1",
                "text": "Sunny will inspect the requested evidence. ☀️",
            },
            {
                "kind": "task_commentary",
                "event_id": "task-commentary:2",
                "phase": "replan",
                "revision": 2,
                "text": "Sunny completed the inspection and is validating it. ☀️",
            },
        ]
        for raw_event in raw_events:
            for event in _claw_jsonl_to_stream_events(
                raw_event,
                request_id="req-1",
            ):
                await controller.forward(event)
        if cadence_task is not None:
            deadline = asyncio.get_running_loop().time() + 0.2
            while len(sent) < len(expected_purposes):
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.001)
            await controller.finish()
            await cadence_task
        else:
            await controller.finish()

    assert [entry[2]["_purpose"] for entry in sent] == expected_purposes


@pytest.mark.asyncio
async def test_her_commentary_off_suppresses_acknowledgement_and_progress_live():
    runtime = _runtime()
    runtime.config.active_backend = "her"
    runtime.backend_manager.current_backend.effort = "high"
    runtime._commentary = False
    telegram_stream_policy.set_typing_enabled(runtime, False)
    sent = []

    async def _send_text(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))

    runtime._send_text = _send_text
    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )

    await feedback.on_stream_event(
        StreamEvent(
            kind=KIND_ACKNOWLEDGEMENT,
            summary="Sunny will inspect the request. ☀️",
            event_id="req-1:ack:initial",
            delivery_class=DELIVERY_USER_COMMENTARY,
            origin="her_planner",
            phase="initial",
        )
    )
    await feedback.on_stream_event(
        StreamEvent(
            kind=KIND_COMMENTARY,
            summary="Sunny has a verified update. ☀️",
            event_id="req-1:commentary:replan:1",
            delivery_class=DELIVERY_USER_COMMENTARY,
            origin="her_planner",
            phase="replan",
            revision=1,
        )
    )

    assert sent == []
    records = [
        json.loads(line)
        for line in (
            runtime.workspace_dir / "backend_state" / "her_message_audit.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [record["status"] for record in records] == [
        "generated",
        "suppressed",
        "generated",
        "suppressed",
    ]
    assert {record.get("reason") for record in records if record["status"] == "suppressed"} == {
        "commentary_disabled"
    }


@pytest.mark.asyncio
async def test_her_router_trusts_emitted_commentary_without_rechecking_effort():
    runtime = _runtime()
    runtime.config.active_backend = "her"
    runtime.backend_manager.current_backend.effort = "medium"
    runtime._commentary = True
    telegram_stream_policy.set_typing_enabled(runtime, False)
    sent = []

    async def _send_text(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))

    runtime._send_text = _send_text
    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )

    await feedback.on_stream_event(
        StreamEvent(
            kind=KIND_COMMENTARY,
            summary="Sunny has a progress update. ☀️",
            event_id="req-1:commentary:replan:1",
            delivery_class=DELIVERY_USER_COMMENTARY,
            origin="her_planner",
            phase="replan",
            revision=1,
        )
    )
    await feedback.on_stream_event(
        StreamEvent(
            kind=KIND_ACKNOWLEDGEMENT,
            summary="Sunny will inspect the request. ☀️",
            event_id="req-1:ack:initial",
            delivery_class=DELIVERY_USER_COMMENTARY,
            origin="her_planner",
            phase="initial",
        )
    )

    assert [entry[2]["_purpose"] for entry in sent] == [
        "task_commentary",
        "task_acknowledgement",
    ]


@pytest.mark.asyncio
async def test_non_deliverable_her_activity_persists_without_presentation():
    runtime = _runtime()
    runtime.config.active_backend = "her"
    runtime.backend_manager.current_backend.effort = "high"
    runtime._commentary = True
    published = []
    runtime.request_activity = SimpleNamespace(
        publish_stream=lambda request_id, event: published.append((request_id, event.kind))
    )

    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(deliver_to_telegram=False),
        audit_active=False,
        audit_collector=None,
    )

    assert feedback.on_stream_event is not None
    assert feedback.her_message_router is not None
    await feedback.on_stream_event(
        StreamEvent(
            kind=KIND_COMMENTARY,
            summary="This stays local and is not delivered.",
            event_id="req-1:commentary:replan:1",
            delivery_class=DELIVERY_USER_COMMENTARY,
            origin="her_planner",
            phase="replan",
            revision=1,
        )
    )
    assert published == [("req-1", KIND_COMMENTARY)]


@pytest.mark.asyncio
async def test_required_her_control_is_visible_with_all_optional_channels_off():
    runtime = _runtime()
    runtime.config.active_backend = "her"
    runtime.backend_manager.current_backend.effort = "high"
    runtime._commentary = False
    runtime._verbose = False
    runtime._think = False
    telegram_stream_policy.set_typing_enabled(runtime, False)
    sent = []

    async def _send_text(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))

    runtime._send_text = _send_text
    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )
    await feedback.on_stream_event(
        StreamEvent(
            kind=KIND_PROGRESS,
            summary="HER permission required for browser control",
            event_id="req-1:control:permission:1",
            delivery_class=DELIVERY_CONTROL,
            origin="her_runtime",
            phase="execution",
            required=True,
        )
    )

    assert len(sent) == 1
    assert sent[0][2]["_purpose"] == "her_control"


@pytest.mark.asyncio
async def test_setup_interactive_feedback_placeholder_retry_after_records_failover(tmp_path):
    runtime = _runtime()
    runtime.name = "kasumi"
    runtime.workspace_dir = tmp_path / "workspaces" / "kasumi"
    runtime.workspace_dir.mkdir(parents=True, exist_ok=True)
    runtime.global_config = SimpleNamespace(project_root=tmp_path)
    runtime.config.telegram_token_key = "kasumi"
    runtime.telegram_connected = True
    runtime.startup_success = True
    runtime.token = "token-kasumi"
    runtime.app.bot = _Bot(send_error=RetryAfter(60))
    _set_stream_policy(runtime, placeholder=True)

    failover_runtime = SimpleNamespace(
        name="lin_yueru",
        workspace_dir=tmp_path / "workspaces" / "lin_yueru",
        config=SimpleNamespace(extra={}, telegram_token_key="lin_yueru"),
        global_config=SimpleNamespace(project_root=tmp_path),
        app=SimpleNamespace(bot=_Bot()),
        telegram_connected=True,
        startup_success=True,
        token="token-lin-yueru",
    )
    failover_runtime.workspace_dir.mkdir(parents=True, exist_ok=True)
    orchestrator = SimpleNamespace(runtimes=[runtime, failover_runtime], raw_config={})
    runtime.orchestrator = orchestrator
    failover_runtime.orchestrator = orchestrator

    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )

    feedback.stop_typing.set()
    assert feedback.typing_task is None

    saved = failover.load_health_state(runtime)
    record = saved["agents"]["kasumi"]
    assert record["status"] == "blocked"
    assert record["retry_after_s"] == 60
    assert failover_runtime.app.bot.sent
    assert feedback.placeholder is None
    assert feedback.answer_preview_task is None


@pytest.mark.asyncio
async def test_setup_interactive_feedback_skips_placeholder_when_delivery_blocked(tmp_path):
    runtime = _runtime()
    runtime.name = "kasumi"
    runtime.workspace_dir = tmp_path / "workspaces" / "kasumi"
    runtime.workspace_dir.mkdir(parents=True, exist_ok=True)
    runtime.global_config = SimpleNamespace(project_root=tmp_path)
    runtime.config.telegram_token_key = "kasumi"
    runtime.telegram_connected = True
    runtime.startup_success = True
    runtime.token = "token-kasumi"
    runtime.app.bot = _Bot()

    failover_runtime = SimpleNamespace(
        name="lin_yueru",
        workspace_dir=tmp_path / "workspaces" / "lin_yueru",
        config=SimpleNamespace(extra={}, telegram_token_key="lin_yueru"),
        global_config=SimpleNamespace(project_root=tmp_path),
        app=SimpleNamespace(bot=_Bot()),
        telegram_connected=True,
        startup_success=True,
        token="token-lin-yueru",
    )
    failover_runtime.workspace_dir.mkdir(parents=True, exist_ok=True)
    orchestrator = SimpleNamespace(runtimes=[runtime, failover_runtime], raw_config={})
    runtime.orchestrator = orchestrator
    failover_runtime.orchestrator = orchestrator

    state_path = tmp_path / "state" / "telegram_delivery_health.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "agents": {
                    "kasumi": {
                        "token_key": "telegram:kasumi",
                        "status": "blocked",
                        "blocked_until": "2030-01-01T00:00:00+10:00",
                        "retry_after_s": 60,
                        "incident_id": "tg-kasumi-test",
                        "per_chat": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )

    assert not runtime.app.bot.sent
    assert feedback.stop_typing is None
    assert feedback.typing_task is None
    assert feedback.placeholder is None


@pytest.mark.asyncio
async def test_typing_off_skips_typing_ui_and_uses_final_delivery_once():
    runtime = _runtime()
    runtime.config.extra = {}
    runtime._verbose = False
    runtime._think = False
    telegram_stream_policy.set_typing_enabled(runtime, False)
    sends = []

    async def _send_long_message(**kwargs):
        sends.append(kwargs)
        return 0.1, 1

    runtime.send_long_message = _send_long_message
    item = _item(prompt="user text")

    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        item,
        audit_active=False,
        audit_collector=None,
    )

    assert feedback.stop_typing is None
    assert feedback.typing_task is None
    assert feedback.escalation_task is None
    assert feedback.answer_preview_task is None
    assert feedback.placeholder is None
    assert feedback.on_stream_event is None
    assert runtime.app.bot.sent == []
    assert runtime.app.bot.edits == []

    await runtime_pipeline.handle_success_delivery(
        runtime,
        item,
        SimpleNamespace(text="final answer"),
        visible_text="final answer",
        wrapper_result=None,
        is_bridge_request=False,
        session_reset_source="session_reset",
        queued_at=datetime.now(),
        queue_wait_s=0,
        backend_elapsed_s=0,
        audit_collector=None,
    )

    assert len(sends) == 1
    assert sends[0]["purpose"] == "response"
    assert sends[0]["text"] == "final answer"


@pytest.mark.asyncio
async def test_typing_off_keeps_thinking_delivery_independent_without_placeholder():
    runtime = _runtime()
    runtime.config.extra = {}
    runtime._think = True
    telegram_stream_policy.set_typing_enabled(runtime, False)

    async def _flush_thinking(_chat_id):
        return None

    runtime._flush_thinking = _flush_thinking

    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )

    assert feedback.placeholder is None
    assert feedback.typing_task is None
    assert feedback.answer_preview_task is None
    assert feedback.think_flush_task is not None
    assert feedback.on_stream_event is not None

    await runtime_pipeline.cleanup_interactive_feedback(
        runtime,
        _item(),
        stop_typing=feedback.stop_typing,
        typing_task=feedback.typing_task,
        escalation_task=feedback.escalation_task,
        answer_preview_task=feedback.answer_preview_task,
        think_flush_task=feedback.think_flush_task,
        placeholder=feedback.placeholder,
    )


@pytest.mark.asyncio
async def test_legacy_preview_flags_do_not_create_answer_stream_state():
    runtime = _runtime()
    runtime.config.extra = {
        "telegram_stream_enabled": True,
        "answer_stream_final_delivery": True,
    }
    runtime.backend_manager.current_backend.capabilities.supports_answer_stream = True
    _set_stream_policy(runtime, placeholder=True, preview=True, promote=True)

    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )

    assert feedback.answer_stream_state is None
    assert feedback.answer_preview_task is None
    feedback.stop_typing.set()
    await feedback.typing_task


@pytest.mark.asyncio
async def test_typing_only_does_not_route_answer_deltas_or_edit_placeholder():
    runtime = _runtime()
    runtime.config.extra = {
        "telegram_stream_enabled": True,
        "answer_stream_edit_interval_s": 0.01,
        "answer_stream_min_chars": 1,
    }
    _set_stream_policy(runtime, placeholder=True, preview=True)

    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )

    assert feedback.on_stream_event is None
    assert feedback.answer_preview_task is None
    assert feedback.answer_stream_state is None
    feedback.stop_typing.set()
    await feedback.typing_task
    assert runtime.app.bot.edits == []


@pytest.mark.asyncio
async def test_answer_preview_records_stream_state_deltas_and_edits():
    runtime = _runtime()
    runtime.config.extra = {
        "telegram_stream_enabled": True,
        "answer_stream_edit_interval_s": 0.01,
        "answer_stream_min_chars": 1,
    }
    stream_state = runtime_pipeline.StreamedAnswerState(
        request_id="req-1",
        chat_id=123,
        placeholder=SimpleNamespace(message_id=77),
        buffer=[],
        started_at=datetime.now(),
    )
    event_queue = asyncio.Queue()
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        runtime_pipeline.answer_preview_loop(
            runtime,
            _item(),
            placeholder=SimpleNamespace(message_id=77),
            stop_event=stop_event,
            event_queue=event_queue,
            stream_state=stream_state,
        )
    )

    await event_queue.put(StreamEvent(kind=KIND_TEXT_DELTA, summary="Hello "))
    await event_queue.put(StreamEvent(kind=KIND_TEXT_DELTA, summary="world"))
    for _ in range(20):
        if runtime.app.bot.edits:
            break
        await asyncio.sleep(0.02)

    stop_event.set()
    await task

    assert "".join(stream_state.buffer) == "Hello world"
    assert stream_state.delta_count == 2
    assert stream_state.char_count == len("Hello world")
    assert stream_state.edit_count >= 1


@pytest.mark.asyncio
async def test_answer_preview_stops_after_per_request_edit_budget():
    runtime = _runtime()
    runtime.config.extra = {
        "telegram_stream_enabled": True,
        "answer_stream_edit_interval_s": 0.01,
        "answer_stream_min_chars": 1,
        "answer_stream_max_edits": 2,
    }
    event_queue = asyncio.Queue()
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        runtime_pipeline.answer_preview_loop(
            runtime,
            _item(),
            placeholder=SimpleNamespace(message_id=77),
            stop_event=stop_event,
            event_queue=event_queue,
        )
    )

    for index, text in enumerate(("one", "two", "three"), start=1):
        await event_queue.put(StreamEvent(kind=KIND_TEXT_DELTA, summary=text))
        for _ in range(20):
            if len(runtime.app.bot.edits) >= min(index, 2):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.02)

    stop_event.set()
    await task

    assert len(runtime.app.bot.edits) == 2
    assert any("preview budget exhausted" in message for message in runtime.telegram_logger.messages)


@pytest.mark.asyncio
async def test_answer_preview_disables_after_retry_after():
    runtime = _runtime()
    runtime.config.extra = {
        "telegram_stream_enabled": True,
        "answer_stream_edit_interval_s": 0.01,
        "answer_stream_min_chars": 1,
    }
    runtime.app.bot = _Bot(edit_error=RetryAfter(123))
    stream_state = runtime_pipeline.StreamedAnswerState(
        request_id="req-1",
        chat_id=123,
        placeholder=SimpleNamespace(message_id=77),
        buffer=[],
        started_at=datetime.now(),
    )
    event_queue = asyncio.Queue()
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        runtime_pipeline.answer_preview_loop(
            runtime,
            _item(),
            placeholder=SimpleNamespace(message_id=77),
            stop_event=stop_event,
            event_queue=event_queue,
            stream_state=stream_state,
        )
    )

    await event_queue.put(StreamEvent(kind=KIND_TEXT_DELTA, summary="Hello"))
    for _ in range(20):
        if stream_state.failed:
            break
        await asyncio.sleep(0.02)

    stop_event.set()
    await task

    assert runtime.app.bot.edits == []
    assert stream_state.failed is True
    assert "Flood control exceeded" in stream_state.failure_reason
    assert any("Answer stream preview disabled" in message for message in runtime.telegram_logger.messages)


@pytest.mark.asyncio
async def test_answer_preview_skips_edits_when_delivery_blocked(tmp_path):
    runtime = _runtime()
    runtime.global_config = SimpleNamespace(project_root=tmp_path)
    runtime.config.extra = {
        "telegram_stream_enabled": True,
        "answer_stream_edit_interval_s": 0.01,
        "answer_stream_min_chars": 1,
    }
    state_path = tmp_path / "state" / "telegram_delivery_health.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "agents": {
                    "zelda": {
                        "token_key": "telegram:zelda",
                        "status": "blocked",
                        "blocked_until": "2030-01-01T00:00:00+10:00",
                        "retry_after_s": 60,
                        "incident_id": "tg-zelda-test",
                        "per_chat": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    event_queue = asyncio.Queue()
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        runtime_pipeline.answer_preview_loop(
            runtime,
            _item(),
            placeholder=SimpleNamespace(message_id=77),
            stop_event=stop_event,
            event_queue=event_queue,
        )
    )

    await event_queue.put(StreamEvent(kind=KIND_TEXT_DELTA, summary="Hello"))
    await asyncio.sleep(0.05)

    stop_event.set()
    await task

    assert runtime.app.bot.edits == []


@pytest.mark.asyncio
async def test_answer_preview_does_not_edit_without_event_before_heartbeat():
    runtime = _runtime()
    runtime.config.extra = {
        "telegram_stream_enabled": True,
        "answer_stream_edit_interval_s": 0.01,
        "answer_stream_min_chars": 1,
    }

    event_queue = asyncio.Queue()
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        runtime_pipeline.answer_preview_loop(
            runtime,
            _item(),
            placeholder=SimpleNamespace(message_id=77),
            stop_event=stop_event,
            event_queue=event_queue,
        )
    )

    for _ in range(20):
        if runtime.app.bot.edits:
            break
        await asyncio.sleep(0.02)

    stop_event.set()
    await task

    assert runtime.app.bot.edits == []


@pytest.mark.asyncio
async def test_answer_preview_shows_progress_when_text_delta_absent():
    runtime = _runtime()
    runtime.config.extra = {
        "telegram_stream_enabled": True,
        "answer_stream_edit_interval_s": 0.01,
        "answer_stream_min_chars": 1,
    }

    event_queue = asyncio.Queue()
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        runtime_pipeline.answer_preview_loop(
            runtime,
            _item(),
            placeholder=SimpleNamespace(message_id=77),
            stop_event=stop_event,
            event_queue=event_queue,
        )
    )

    await event_queue.put(StreamEvent(kind=KIND_PROGRESS, summary="Codex started reasoning"))
    for _ in range(20):
        if runtime.app.bot.edits:
            break
        await asyncio.sleep(0.02)

    stop_event.set()
    await task

    assert runtime.app.bot.edits
    assert "Codex started reasoning" in runtime.app.bot.edits[-1]["text"]


@pytest.mark.asyncio
async def test_answer_preview_keeps_review_visible_after_answer_deltas_start():
    runtime = _runtime()
    runtime.config.extra = {
        "telegram_stream_enabled": True,
        "answer_stream_edit_interval_s": 0.01,
        "answer_stream_min_chars": 1,
    }
    event_queue = asyncio.Queue()
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        runtime_pipeline.answer_preview_loop(
            runtime,
            _item(),
            placeholder=SimpleNamespace(message_id=77),
            stop_event=stop_event,
            event_queue=event_queue,
        )
    )

    await event_queue.put(StreamEvent(kind=KIND_TEXT_DELTA, summary="Draft answer"))
    await event_queue.put(
        StreamEvent(kind=KIND_REVIEW, summary="Review final_claim r1: PASS")
    )
    for _ in range(20):
        if runtime.app.bot.edits and "Review final_claim" in runtime.app.bot.edits[-1]["text"]:
            break
        await asyncio.sleep(0.02)

    stop_event.set()
    await task

    assert "Review final_claim r1: PASS" in runtime.app.bot.edits[-1]["text"]
    assert "Draft answer" in runtime.app.bot.edits[-1]["text"]


@pytest.mark.asyncio
async def test_legacy_preview_flag_stays_inactive_without_verbose():
    runtime = _runtime()
    runtime.backend_manager.current_backend.capabilities.supports_thinking_stream = False
    _set_stream_policy(runtime, placeholder=True, preview=True)

    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )

    assert feedback.answer_preview_task is None
    assert feedback.answer_stream_state is None
    assert feedback.escalation_task is None
    assert feedback.on_stream_event is None
    feedback.stop_typing.set()
    await feedback.typing_task


@pytest.mark.asyncio
async def test_verbose_takes_precedence_over_retired_preview_flag():
    runtime = _runtime()
    runtime._verbose = True
    _set_stream_policy(runtime, placeholder=True, preview=True)

    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )

    assert feedback.answer_preview_task is None
    assert feedback.answer_stream_state is None
    assert feedback.escalation_task is not None
    assert runtime.stream_callbacks[0]["event_queue"] is not None
    feedback.stop_typing.set()
    await feedback.typing_task
    await feedback.escalation_task
    assert runtime.streaming_loops == [("req-1", True)]


@pytest.mark.asyncio
async def test_verbose_backend_without_reasoning_still_uses_progress_events():
    runtime = _runtime()
    runtime._verbose = True
    runtime.config.extra = {
        "telegram_stream_enabled": True,
        "answer_stream_preview": False,
    }
    runtime.backend_manager.current_backend.capabilities.supports_thinking_stream = False
    _set_stream_policy(runtime, placeholder=True, progress=True, preview=False)

    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )

    assert feedback.on_stream_event[0] == "stream"
    assert runtime.stream_callbacks[0]["event_queue"] is not None
    feedback.stop_typing.set()
    await feedback.typing_task
    await feedback.escalation_task
    assert runtime.streaming_loops == [("req-1", True)]
    assert runtime.escalating_loops == []


@pytest.mark.asyncio
async def test_verbose_streaming_backend_uses_streaming_display():
    runtime = _runtime()
    runtime._verbose = True
    runtime.config.extra = {
        "telegram_stream_enabled": True,
        "answer_stream_preview": False,
    }
    _set_stream_policy(runtime, placeholder=True, progress=True, preview=False)

    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )

    assert feedback.on_stream_event[0] == "stream"
    assert runtime.stream_callbacks[0]["event_queue"] is not None
    feedback.stop_typing.set()
    await feedback.typing_task
    await feedback.escalation_task
    assert runtime.streaming_loops == [("req-1", True)]
    assert runtime.escalating_loops == []


@pytest.mark.asyncio
async def test_verbose_alone_forces_placeholder_and_progress_stream():
    runtime = _runtime()
    runtime._verbose = True
    runtime.config.extra = {"telegram_stream_enabled": True}

    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(),
        audit_active=False,
        audit_collector=None,
    )

    assert feedback.placeholder is not None
    assert runtime.app.bot.sent
    assert feedback.on_stream_event[0] == "stream"
    assert runtime.stream_callbacks[0]["event_queue"] is not None
    feedback.stop_typing.set()
    await feedback.typing_task
    await feedback.escalation_task
    assert runtime.streaming_loops == [("req-1", True)]


@pytest.mark.asyncio
async def test_setup_interactive_feedback_creates_audit_stream_for_silent_item():
    runtime = _runtime()

    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        _item(silent=True),
        audit_active=True,
        audit_collector="audit",
    )

    assert feedback.placeholder is None
    assert feedback.on_stream_event[0] == "stream"
    assert runtime.stream_callbacks == [{"audit_collector": "audit"}]


@pytest.mark.asyncio
async def test_handle_empty_success_response_uses_tool_failure_message():
    runtime = _runtime()
    item = _item()

    await runtime_pipeline.handle_empty_success_response(runtime, item)

    assert runtime.last_error == runtime_pipeline.EMPTY_SUCCESS_TOOL_FAILURE_MESSAGE
    assert runtime.sent_message["purpose"] == "error"
    assert "tool I tried to use" in runtime.listener_payloads[0]["error"]


@pytest.mark.asyncio
async def test_handle_empty_success_response_buffers_transfer():
    runtime = _runtime()
    runtime._should_buffer_during_transfer = lambda request_id: True

    await runtime_pipeline.handle_empty_success_response(runtime, _item())

    assert runtime.suppressed == {
        "success": False,
        "error": runtime_pipeline.EMPTY_SUCCESS_TOOL_FAILURE_MESSAGE,
    }
    assert not hasattr(runtime, "sent_message")


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


@pytest.mark.asyncio
async def test_prepare_successful_response_does_not_mark_hidden_only_text_successful():
    runtime = _runtime()

    async def _strip_hidden_control_content(_item, _text):
        return "", {"mode": "memory_plus"}

    runtime._apply_wrapper_to_visible_text = _strip_hidden_control_content
    response = SimpleNamespace(
        text='<memory_plus_update>{"write":false}</memory_plus_update>'
    )

    result = await runtime_pipeline.prepare_successful_response(
        runtime,
        _item(),
        response,
        completion_path="foreground",
    )

    assert result.visible_text == ""
    assert runtime.success_marked is False
    assert runtime.transcripts == []
    assert runtime.listener_payloads == []


@pytest.mark.asyncio
async def test_prepare_successful_response_blocks_dangling_tool_markup_globally():
    runtime = _runtime()
    item = _item(prompt="请汇报执行结果")
    response = SimpleNamespace(
        text='<｜DSML｜tool_calls><｜DSML｜invoke name="bash">',
        stop_reason="end_turn",
        stream_metadata={"claw_completion_status": "completed"},
    )

    result = await runtime_pipeline.prepare_successful_response(
        runtime,
        item,
        response,
        completion_path="foreground",
    )

    assert "DSML" not in result.visible_text
    assert "不视为已执行或已完成" in result.visible_text
    assert response.stop_reason == "no_final_text"
    assert response.stream_metadata["claw_completion_status"] == "incomplete"
    assert response.stream_metadata["dangling_tool_markup_blocked"] is True
    assert "DSML" not in runtime.transcripts[0]["core_raw"]
    assert "DSML" not in runtime.listener_payloads[0]["text"]


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
        stream_metadata={
            "claw_thinking": {
                "thinking_chars": 44,
                "thinking_event_count": 2,
                "thinking_redacted_count": 1,
                "thinking_sources": ["reasoning", "reasoning_details.encrypted"],
            }
        },
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
    assert event["thinking_chars"] == 44
    assert event["thinking_event_count"] == 2
    assert event["thinking_redacted_count"] == 1
    assert event["thinking_sources"] == ["reasoning", "reasoning_details.encrypted"]
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
        ("assistant", "visible text", "text"),
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
    assert runtime.listener_payloads[0]["success"] is False
    assert runtime.listener_payloads[0]["error"] == "backend failed"
    assert runtime.sent_message["purpose"] == "error"
    assert runtime.sent_message["text"] == "backend failed"
    assert runtime.maintenance_events[0][0] == "send_error"


@pytest.mark.asyncio
async def test_handle_backend_error_passes_raw_failure_to_delivery_boundary():
    runtime = _runtime()
    item = _item()
    response = SimpleNamespace(
        error=(
            '{"type":"error","status":400,"error":{"type":"invalid_request_error",'
            '"message":"The \'gpt-5.6-sol\' model requires a newer version of Codex. '
            'Please upgrade to the latest app or CLI and try again."}}'
        )
    )

    await runtime_pipeline.handle_backend_error(
        runtime,
        item,
        response,
        queued_at=datetime.now() - timedelta(seconds=1),
        queue_wait_s=0.5,
        backend_elapsed_s=0.25,
    )

    assert runtime.sent_message["text"] == response.error
    assert runtime.sent_message["purpose"] == "error"
    assert runtime.listener_payloads[0]["error"] == response.error


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
async def test_scheduler_delivery_persists_cross_session_receipt_after_send():
    runtime = _runtime()
    runtime.config.active_backend = "her"
    item = _item(
        request_id="req-scheduler",
        source="scheduler",
        prompt="Run scheduled work",
        summary="Cron Task [scheduled-work]",
    )
    response = SimpleNamespace(
        text="core incomplete report",
        is_success=True,
        stop_reason="max_iterations",
        stream_metadata={
            "claw_completion_status": "incomplete",
            "claw_stop_reason": "max_iterations",
            "recommended_action": "continue",
            "her_session_scope": "isolated_per_run",
            "her_session_id": "scheduler-session",
            "her_model": "local/deepseek-v4-pro",
        },
    )

    await runtime_pipeline.handle_success_delivery(
        runtime,
        item,
        response,
        visible_text="Visible incomplete report. CONTINUE.",
        wrapper_result=None,
        is_bridge_request=False,
        session_reset_source="session_reset",
        queued_at=datetime.now(),
        queue_wait_s=0,
        backend_elapsed_s=0,
        audit_collector=None,
    )

    receipt = runtime_cross_session.load_receipts(runtime)[0]
    assert receipt["delivered"] is True
    assert receipt["status"] == "incomplete"
    assert receipt["session_id"] == "scheduler-session"
    assert receipt["active"] is True


@pytest.mark.asyncio
async def test_her_direct_response_stream_event_is_not_sent_before_final_delivery():
    runtime = _runtime()
    runtime.config.active_backend = "her"
    runtime.backend_manager.current_backend.effort = "medium"
    runtime._commentary = True
    runtime._verbose = False
    runtime._think = False
    telegram_stream_policy.set_typing_enabled(runtime, False)
    item = _item(prompt="hello")
    feedback = await runtime_pipeline.setup_interactive_feedback(
        runtime,
        item,
        audit_active=False,
        audit_collector=None,
    )
    await feedback.on_stream_event(
        StreamEvent(
            kind=KIND_ACKNOWLEDGEMENT,
            summary="complete direct answer",
            event_id="req-1:final",
            delivery_class=DELIVERY_FINAL,
            origin="her_planner",
            phase="finalization",
            required=True,
        )
    )

    assert not hasattr(runtime, "sent_message")

    await runtime_pipeline.handle_success_delivery(
        runtime,
        item,
        SimpleNamespace(text="complete direct answer"),
        visible_text="complete direct answer",
        wrapper_result=None,
        is_bridge_request=False,
        session_reset_source="session_reset",
        queued_at=datetime.now(),
        queue_wait_s=0,
        backend_elapsed_s=0,
        audit_collector=None,
        her_message_router=feedback.her_message_router,
    )

    assert runtime.sent_message["text"] == "complete direct answer"
    assert feedback.her_message_router.deferred_final is not None


@pytest.mark.asyncio
async def test_finalize_streamed_answer_promotes_placeholder_to_final_text():
    runtime = _runtime()
    item = _item()
    stream_state = runtime_pipeline.StreamedAnswerState(
        request_id=item.request_id,
        chat_id=item.chat_id,
        placeholder=SimpleNamespace(message_id=77),
        buffer=["raw ", "preview"],
        started_at=datetime.now(),
        delta_count=2,
        char_count=len("raw preview"),
        edit_count=1,
    )

    result = await runtime_pipeline.finalize_streamed_answer(
        runtime,
        item,
        stream_state=stream_state,
        final_text="wrapped final text",
    )

    assert result.final_delivered is True
    assert result.fallback_required is False
    assert stream_state.final_promoted is True
    assert runtime.app.bot.edits[-1]["text"] == "wrapped final text"
    assert not hasattr(runtime, "sent_message")


@pytest.mark.asyncio
async def test_finalize_streamed_answer_skips_promotion_when_delivery_blocked(tmp_path):
    runtime = _runtime()
    runtime.global_config = SimpleNamespace(project_root=tmp_path)
    runtime.workspace_dir = tmp_path / "workspaces" / "zelda"
    runtime.workspace_dir.mkdir(parents=True, exist_ok=True)
    item = _item()
    stream_state = runtime_pipeline.StreamedAnswerState(
        request_id=item.request_id,
        chat_id=item.chat_id,
        placeholder=SimpleNamespace(message_id=77),
        buffer=["raw ", "preview"],
        started_at=datetime.now(),
        delta_count=2,
        char_count=len("raw preview"),
        edit_count=1,
    )
    state_path = tmp_path / "state" / "telegram_delivery_health.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "agents": {
                    "zelda": {
                        "token_key": "telegram:zelda",
                        "status": "blocked",
                        "blocked_until": "2030-01-01T00:00:00+10:00",
                        "retry_after_s": 60,
                        "incident_id": "tg-zelda-test",
                        "per_chat": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = await runtime_pipeline.finalize_streamed_answer(
        runtime,
        item,
        stream_state=stream_state,
        final_text="wrapped final text",
    )

    assert result.final_delivered is False
    assert result.fallback_required is False
    assert result.error == "delivery blocked"
    assert runtime.app.bot.edits == []
    assert (tmp_path / "workspaces" / "zelda" / "undelivered" / f"{item.request_id}.md").exists()


@pytest.mark.asyncio
async def test_finalize_streamed_answer_without_deltas_deletes_placeholder_and_falls_back():
    runtime = _runtime()
    item = _item()
    stream_state = runtime_pipeline.StreamedAnswerState(
        request_id=item.request_id,
        chat_id=item.chat_id,
        placeholder=SimpleNamespace(message_id=77),
        buffer=[],
        started_at=datetime.now(),
    )

    result = await runtime_pipeline.finalize_streamed_answer(
        runtime,
        item,
        stream_state=stream_state,
        final_text="wrapped final text",
    )

    assert result.final_delivered is False
    assert result.fallback_required is True
    assert runtime.app.bot.deleted == [{"chat_id": 123, "message_id": 77}]


@pytest.mark.asyncio
async def test_handle_success_delivery_promotes_streamed_final_after_wrapper_text():
    runtime = _runtime()
    item = _item(prompt="user text")
    stream_state = runtime_pipeline.StreamedAnswerState(
        request_id=item.request_id,
        chat_id=item.chat_id,
        placeholder=SimpleNamespace(message_id=77),
        buffer=["raw backend"],
        started_at=datetime.now(),
        delta_count=1,
        char_count=len("raw backend"),
        edit_count=1,
    )

    await runtime_pipeline.handle_success_delivery(
        runtime,
        item,
        SimpleNamespace(text="core text"),
        visible_text="wrapped final text",
        wrapper_result={"mode": "wrapper"},
        is_bridge_request=False,
        session_reset_source="session_reset",
        queued_at=datetime.now() - timedelta(seconds=1),
        queue_wait_s=0.2,
        backend_elapsed_s=0.3,
        audit_collector="audit",
        answer_stream_state=stream_state,
    )

    assert runtime.app.bot.edits[-1]["text"] == "wrapped final text"
    assert not hasattr(runtime, "sent_message")
    assert runtime.voice_replies == [(123, "wrapped final text", "req-1")]
    assert runtime.hchat_routes == [("req-1", "wrapped final text")]


@pytest.mark.asyncio
async def test_handle_success_delivery_buffered_transfer_deletes_stream_placeholder():
    runtime = _runtime()
    runtime._should_buffer_during_transfer = lambda request_id: True
    item = _item(prompt="user text")
    stream_state = runtime_pipeline.StreamedAnswerState(
        request_id=item.request_id,
        chat_id=item.chat_id,
        placeholder=SimpleNamespace(message_id=77),
        buffer=["raw backend"],
        started_at=datetime.now(),
        delta_count=1,
        char_count=len("raw backend"),
    )

    await runtime_pipeline.handle_success_delivery(
        runtime,
        item,
        SimpleNamespace(text="core text"),
        visible_text="wrapped final text",
        wrapper_result={"mode": "wrapper"},
        is_bridge_request=False,
        session_reset_source="session_reset",
        queued_at=datetime.now(),
        queue_wait_s=0,
        backend_elapsed_s=0,
        audit_collector=None,
        answer_stream_state=stream_state,
    )

    assert runtime.suppressed == {"success": True, "text": "wrapped final text"}
    assert runtime.app.bot.deleted == [{"chat_id": 123, "message_id": 77}]


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
