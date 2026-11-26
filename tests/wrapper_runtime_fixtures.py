"""Minimal runtime fixtures retained for the SafeVoice discard coverage."""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from adapters.base import BackendResponse
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.runtime_common import QueuedRequest


class FakeMemoryStore:
    def __init__(self):
        self.turns = []
        self.exchanges = []

    def record_turn(self, role, source, text):
        self.turns.append((role, source, text))

    def record_exchange(self, user_text, assistant_text, source):
        self.exchanges.append((user_text, assistant_text, source))


class FakeHandoffBuilder:
    def __init__(self):
        self.transcript = []
        self.refresh_count = 0

    def get_recent_rounds(self, max_rounds=10):
        return [[{"role": "user", "text": "Earlier question", "source": "text"}]]

    def append_transcript(self, role, text, source="text", metadata=None):
        del metadata
        self.transcript.append((role, text, source))

    def refresh_recent_context(self):
        self.refresh_count += 1


class FakeProjectChatLogger:
    def __init__(self):
        self.exchanges = []

    def log_exchange(self, prompt, response, source):
        self.exchanges.append((prompt, response, source))


def _make_background_runtime(
    tmp_path: Path, wrapper_response: BackendResponse | None = None
):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.is_shutting_down = False
    runtime.name = "zelda"
    runtime.config = SimpleNamespace(active_backend="codex-cli", extra={})
    runtime.global_config = SimpleNamespace(project_root=tmp_path)
    runtime.workspace_dir = tmp_path
    runtime.core_transcript_log_path = tmp_path / "core_transcript.jsonl"
    runtime.session_id_dt = "session-test"
    runtime.telegram_connected = False
    runtime.memory_store = FakeMemoryStore()
    runtime.handoff_builder = FakeHandoffBuilder()
    runtime.project_chat_logger = FakeProjectChatLogger()
    runtime._last_prompt_audit = {}
    runtime._last_full_prompt_tokens = 10
    runtime._thinking_chars_this_req = 0
    runtime._verbose = False
    runtime.last_response = None
    runtime.logger = SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None
    )
    runtime.error_logger = SimpleNamespace(
        error=lambda *a, **k: None, exception=lambda *a, **k: None
    )
    runtime._request_listeners = {}
    runtime._pending_request_results = {}
    runtime._suppressed_transfer_results = []
    runtime._post_turn_observers = []
    runtime._pre_turn_context_providers = []

    async def fake_wrapper_response(**kwargs):
        return wrapper_response or BackendResponse(
            text="wrapped visible", duration_ms=1.0
        )

    runtime.backend_manager = SimpleNamespace(
        agent_mode="wrapper",
        get_state_snapshot=lambda: {
            "wrapper": {
                "backend": "claude-cli",
                "model": "claude-haiku-4-5",
                "context_window": 1,
            },
            "wrapper_slots": {"1": "Be warm."},
        },
        generate_ephemeral_response=fake_wrapper_response,
    )
    runtime._mark_success = lambda: None
    runtime._mark_error = lambda error: None
    runtime._should_buffer_during_transfer = lambda request_id: False
    runtime._record_suppressed_transfer_result = lambda item, **kwargs: (
        runtime._suppressed_transfer_results.append(kwargs)
    )
    runtime.get_current_model = lambda: "gpt-5.5"
    runtime._get_system_prompt_text = lambda: "system"
    runtime.typing_loop = FlexibleAgentRuntime.typing_loop.__get__(
        runtime, FlexibleAgentRuntime
    )
    runtime._wrapper_enabled = FlexibleAgentRuntime._wrapper_enabled.__get__(
        runtime, FlexibleAgentRuntime
    )
    runtime._wrapper_visible_context = (
        FlexibleAgentRuntime._wrapper_visible_context.__get__(
            runtime, FlexibleAgentRuntime
        )
    )
    runtime._wrapper_audit_fields = FlexibleAgentRuntime._wrapper_audit_fields.__get__(
        runtime, FlexibleAgentRuntime
    )
    runtime._core_memory_assistant_text = (
        FlexibleAgentRuntime._core_memory_assistant_text.__get__(
            runtime, FlexibleAgentRuntime
        )
    )
    runtime._format_wrapper_verbose_trace = (
        FlexibleAgentRuntime._format_wrapper_verbose_trace.__get__(
            runtime, FlexibleAgentRuntime
        )
    )
    runtime._send_wrapper_verbose_trace = (
        FlexibleAgentRuntime._send_wrapper_verbose_trace.__get__(
            runtime, FlexibleAgentRuntime
        )
    )
    runtime._append_core_transcript = (
        FlexibleAgentRuntime._append_core_transcript.__get__(
            runtime, FlexibleAgentRuntime
        )
    )
    runtime._send_wrapper_polishing_placeholder = (
        FlexibleAgentRuntime._send_wrapper_polishing_placeholder.__get__(
            runtime, FlexibleAgentRuntime
        )
    )
    runtime._delete_wrapper_polishing_placeholder = (
        FlexibleAgentRuntime._delete_wrapper_polishing_placeholder.__get__(
            runtime, FlexibleAgentRuntime
        )
    )
    runtime._apply_wrapper_to_visible_text = (
        FlexibleAgentRuntime._apply_wrapper_to_visible_text.__get__(
            runtime, FlexibleAgentRuntime
        )
    )
    async def notify_request_listeners(request_id, payload):
        # Wrapper tests exercise presentation and delivery plumbing. Canonical
        # terminal ownership is covered with real SessionStore fixtures in the
        # run-handoff suite, so keep this lightweight runtime at that boundary.
        callbacks = runtime._request_listeners.pop(request_id, [])
        if not callbacks:
            runtime._pending_request_results[request_id] = payload
        for callback in callbacks:
            result = callback(payload)
            if inspect.isawaitable(result):
                await result

    runtime._notify_request_listeners = notify_request_listeners
    sent = []
    voices = []

    async def send_long_message(
        chat_id,
        text,
        request_id=None,
        purpose=None,
        **delivery_options,
    ):
        sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "request_id": request_id,
                "purpose": purpose,
                **delivery_options,
            }
        )
        return 0.0, 1

    async def send_voice_reply(chat_id, text, request_id):
        voices.append({"chat_id": chat_id, "text": text, "request_id": request_id})

    runtime.send_long_message = send_long_message
    runtime._send_voice_reply = send_voice_reply
    return runtime, sent, voices


class FakeBot:
    def __init__(self):
        self.messages = []
        self.deleted = []

    async def send_message(self, chat_id, text, parse_mode=None, **kwargs):
        self.messages.append(
            {"chat_id": chat_id, "text": text, "parse_mode": parse_mode, **kwargs}
        )
        return SimpleNamespace(message_id=len(self.messages))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append({"chat_id": chat_id, "message_id": message_id})

    async def send_chat_action(self, chat_id, action):
        return None


class FakeContextAssembler:
    def build_prompt_payload(
        self,
        prompt,
        backend,
        *,
        extra_sections=None,
        inject_memory=True,
        incremental=False,
        pcm_profile="cloud",
    ):
        return {
            "final_prompt": f"system\n\n{prompt}",
            "audit": {
                "sections": [],
                "budget_applied": False,
                "budget_limit_chars": 24000,
                "context_chars_before_budget": 0,
                "time_fyi_chars": 0,
                "context_fingerprint": "test",
            },
        }


def _make_foreground_runtime(
    tmp_path: Path, wrapper_response: BackendResponse | None = None
):
    runtime, sent, voices = _make_background_runtime(
        tmp_path, wrapper_response=wrapper_response
    )
    runtime.queue = asyncio.Queue()
    runtime.app = SimpleNamespace(bot=FakeBot())
    runtime.telegram_logger = SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None
    )
    runtime.context_assembler = FakeContextAssembler()
    runtime.backend_manager.current_backend = SimpleNamespace(_session_id=None)
    runtime.backend_manager.generate_response = lambda *a, **k: _completed_task(
        BackendResponse(text="core raw foreground", duration_ms=1.0)
    )
    runtime._remote_backend_block_reason = lambda source: None
    runtime._mark_activity = lambda: None
    runtime._log_maintenance = lambda *a, **k: None
    runtime._consume_session_primer = lambda item: item.prompt
    runtime._workzone_prompt_section = lambda: []
    runtime.get_typing_placeholder = lambda: ("typing", None)
    runtime.typing_loop = lambda chat_id, stop_event, *, request_id=None: stop_event.wait()
    runtime._strip_transfer_accept_prefix = lambda item, text: text
    runtime._verbose = False
    runtime._think = False
    runtime._cos_enabled = False
    runtime.is_generating = False
    runtime.current_request_meta = None
    hchat_replies = []

    async def hchat_route_reply(item, text):
        hchat_replies.append(
            {"request_id": item.request_id, "text": text, "source": item.source}
        )

    runtime._hchat_route_reply = hchat_route_reply
    runtime.process_queue = FlexibleAgentRuntime.process_queue.__get__(
        runtime, FlexibleAgentRuntime
    )
    return runtime, sent, voices, hchat_replies


def _queued_request() -> QueuedRequest:
    return QueuedRequest(
        request_id="req-001",
        chat_id=123,
        prompt="hello",
        source="text",
        summary="hello",
        created_at=datetime.now().isoformat(),
    )


async def _completed_task(response):
    return response
