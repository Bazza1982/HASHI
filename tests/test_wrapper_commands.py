from __future__ import annotations

import asyncio
import json
import sys
import types
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import pytest

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from adapters.base import BackendResponse
from adapters.stream_events import KIND_SHELL_EXEC, StreamEvent
from orchestrator.audit_mode import AuditTelemetryCollector, DEFAULT_AUDIT_CRITERION_SLOT_TEXT
from orchestrator.config import FlexibleAgentConfig, GlobalConfig
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.runtime_common import QueuedRequest
from orchestrator.wrapper_mode import DEFAULT_WRAPPER_STYLE_SLOT_TEXT, SESSION_RESET_SOURCE


def _make_manager(workspace: Path) -> FlexibleBackendManager:
    workspace.mkdir(parents=True, exist_ok=True)
    cfg = FlexibleAgentConfig(
        name="test-flex",
        workspace_dir=workspace,
        system_md=workspace / "AGENT.md",
        telegram_token_key="test-flex",
        allowed_backends=[
            {"engine": "codex-cli", "model": "gpt-5.4"},
            {"engine": "claude-cli", "model": "claude-haiku-4-5"},
        ],
        active_backend="codex-cli",
        project_root=workspace,
    )
    global_cfg = GlobalConfig(
        authorized_id=1,
        base_logs_dir=workspace / "logs",
        base_media_dir=workspace / "media",
        project_root=workspace,
    )
    return FlexibleBackendManager(cfg, global_cfg, secrets={})


def _make_runtime(manager: FlexibleBackendManager) -> tuple[FlexibleAgentRuntime, list[str]]:
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.backend_manager = manager
    runtime.config = manager.config
    runtime._post_turn_observers = []
    runtime._pre_turn_context_providers = []
    runtime._is_authorized_user = lambda user_id: user_id == 1
    runtime._backend_busy = lambda: False
    runtime._workzone_dir = None
    runtime._sync_workzone_to_backend_config = lambda: None
    runtime._clear_handoff_state = lambda: None
    runtime._arm_session_primer = lambda note: None
    runtime.get_current_model = lambda: getattr(manager, "_active_model_override", None) or "gpt-5.4"
    runtime._get_current_effort = lambda: None
    runtime.last_backend_switch_at = None
    messages: list[str] = []
    runtime._reply_payloads = []

    async def _reply_text(update, text, **kwargs):
        messages.append(text)
        runtime._reply_payloads.append({"text": text, **kwargs})

    async def _switch_backend_mode(chat_id, target_engine, target_model=None, with_context=False):
        manager.config.active_backend = target_engine
        manager._save_state(active_model=target_model)
        return True, f"Backend switched to: {target_engine}\nModel: {target_model}"

    runtime._reply_text = _reply_text
    runtime._switch_backend_mode = _switch_backend_mode
    return runtime, messages


def _update(args: list[str] | None = None):
    return (
        SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(id=123)),
        SimpleNamespace(args=args or []),
    )


def _read_state(workspace: Path) -> dict:
    return json.loads((workspace / "state.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_cmd_hchat_legacy_path_enqueues_bridge_hchat_source(tmp_path):
    manager = _make_manager(tmp_path)
    runtime, messages = _make_runtime(manager)
    runtime.name = "zelda"
    enqueued = []

    async def enqueue_api_text(prompt, **kwargs):
        enqueued.append({"prompt": prompt, **kwargs})

    runtime.enqueue_api_text = enqueue_api_text

    update, context = _update(["akane", "review", "the", "delivery", "plan"])

    await FlexibleAgentRuntime.cmd_hchat(runtime, update, context)

    assert messages == ["💬 Composing Hchat message to <b>akane</b>..."]
    assert len(enqueued) == 1
    assert enqueued[0]["source"] == "bridge:hchat"
    assert enqueued[0]["deliver_to_telegram"] is True
    assert "[HCHAT TASK]" in enqueued[0]["prompt"]
    assert "--to akane --from zelda" in enqueued[0]["prompt"]
    assert "tools/hchat_send.py" in enqueued[0]["prompt"]


@pytest.mark.asyncio
async def test_cmd_hchat_legacy_path_preserves_remote_target_in_prompt(tmp_path):
    manager = _make_manager(tmp_path)
    runtime, _messages = _make_runtime(manager)
    runtime.name = "zelda"
    enqueued = []

    async def enqueue_api_text(prompt, **kwargs):
        enqueued.append({"prompt": prompt, **kwargs})

    runtime.enqueue_api_text = enqueue_api_text

    update, context = _update(["rika@HASHI2", "check", "the", "remote", "route"])

    await FlexibleAgentRuntime.cmd_hchat(runtime, update, context)

    assert len(enqueued) == 1
    assert enqueued[0]["source"] == "bridge:hchat"
    assert 'agent "rika@hashi2"' in enqueued[0]["prompt"]
    assert "--to rika@hashi2 --from zelda" in enqueued[0]["prompt"]


@pytest.mark.asyncio
async def test_cmd_hchat_draft_delivery_flag_enqueues_draft_source(tmp_path):
    manager = _make_manager(tmp_path)
    manager.config.extra = {"hchat_draft_delivery": True}
    runtime, messages = _make_runtime(manager)
    runtime.name = "zelda"
    enqueued = []

    async def enqueue_api_text(prompt, **kwargs):
        enqueued.append({"prompt": prompt, **kwargs})

    runtime.enqueue_api_text = enqueue_api_text

    update, context = _update(["akane", "review", "the", "delivery", "plan"])

    await FlexibleAgentRuntime.cmd_hchat(runtime, update, context)

    assert messages == ["💬 Drafting Hchat message to <b>akane</b>..."]
    assert len(enqueued) == 1
    assert enqueued[0]["source"] == "bridge:hchat-draft"
    assert enqueued[0]["deliver_to_telegram"] is True
    assert '"target": "akane"' in enqueued[0]["prompt"]
    assert "tools/hchat_send.py" not in enqueued[0]["prompt"]
    assert "Do not run shell commands." in enqueued[0]["prompt"]


@pytest.mark.asyncio
async def test_hchat_draft_success_prepares_delivery_report(tmp_path):
    runtime, _sent, _voices = _make_background_runtime(tmp_path)
    listener_payloads = []
    runtime.register_request_listener = FlexibleAgentRuntime.register_request_listener.__get__(runtime, FlexibleAgentRuntime)
    runtime.register_request_listener("req-001", lambda payload: listener_payloads.append(payload))
    sender_calls = []

    def fake_sender(to_agent, from_agent, text, **kwargs):
        sender_calls.append((to_agent, from_agent, text, kwargs))
        return True

    runtime._hchat_draft_sender = fake_sender
    item = _queued_request_from("bridge:hchat-draft")
    core_raw = '{"target": "akane", "message": "Please review the plan.", "user_report": "I sent Akane the plan."}'

    result = await FlexibleAgentRuntime._prepare_hchat_draft_success(
        runtime,
        item,
        core_raw=core_raw,
        completion_path="foreground",
    )

    assert result.visible_text == "I sent Akane the plan."
    assert sender_calls == [("akane", "zelda", "Please review the plan.", {})]
    assert listener_payloads[0]["success"] is True
    assert listener_payloads[0]["text"] == "I sent Akane the plan."
    assert listener_payloads[0]["hchat_draft_parsed"]["target"] == "akane"
    assert listener_payloads[0]["hchat_payload_final"] == "Please review the plan."
    assert listener_payloads[0]["hchat_delivery_status"] == "delivered"


@pytest.mark.asyncio
async def test_hchat_draft_parse_error_does_not_send(tmp_path):
    runtime, _sent, _voices = _make_background_runtime(tmp_path)
    listener_payloads = []
    runtime.register_request_listener = FlexibleAgentRuntime.register_request_listener.__get__(runtime, FlexibleAgentRuntime)
    runtime.register_request_listener("req-001", lambda payload: listener_payloads.append(payload))
    sender_calls = []
    runtime._hchat_draft_sender = lambda *args, **kwargs: sender_calls.append((args, kwargs)) or True
    item = _queued_request_from("bridge:hchat-draft")

    result = await FlexibleAgentRuntime._prepare_hchat_draft_success(
        runtime,
        item,
        core_raw='{"message": "missing target"}',
        completion_path="foreground",
    )

    assert result.visible_text == '[hchat] Draft parse error: missing required field "target". Message not sent.'
    assert sender_calls == []
    assert listener_payloads[0]["success"] is False
    assert listener_payloads[0]["error"] == result.visible_text


class _StatusMemoryStore:
    def get_stats(self):
        return {"turns": 0, "memories": 0}


class _StatusBackendManager:
    def __init__(self, mode: str, state: dict):
        self.agent_mode = mode
        self.current_backend = None
        self._state = state

    def get_state_snapshot(self):
        return self._state


def _make_status_runtime(mode: str, state: dict) -> FlexibleAgentRuntime:
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "xishi"
    runtime.skill_manager = None
    runtime.workspace_dir = Path("/tmp/xishi")
    runtime._job_counts = lambda: (0, 0)
    runtime.current_request_meta = None
    runtime.last_error_summary = None
    runtime.last_error_at = None
    runtime.telegram_connected = True
    runtime._get_whatsapp_connected = lambda: False
    runtime.backend_manager = _StatusBackendManager(mode, state)
    runtime.config = SimpleNamespace(active_backend="codex-cli", allowed_backends=[])
    runtime.get_current_model = lambda: "gpt-5.5"
    runtime.is_generating = False
    runtime.queue = Queue()
    runtime._process_info = lambda: "main"
    runtime._pending_session_primer = False
    runtime.last_success_at = None
    runtime.last_activity_at = None
    runtime._format_age = lambda value: "never"
    runtime._get_current_effort = lambda: None
    runtime.transcript_log_path = Path("core_transcript.jsonl")
    runtime.session_started_at = datetime(2026, 5, 4, 21, 47)
    runtime.last_prompt = None
    runtime.last_response = None
    runtime._pending_auto_recall_context = None
    runtime.memory_store = _StatusMemoryStore()
    runtime.recent_context_path = Path("/tmp/xishi/recent_context.md")
    runtime.handoff_path = Path("/tmp/xishi/handoff.md")
    runtime._verbose = False
    runtime._think = False
    runtime.last_backend_switch_at = None
    runtime.session_id_dt = "test-session"
    return runtime


def test_status_text_shows_audit_model_configuration():
    runtime = _make_status_runtime(
        "audit",
        {
            "core": {"backend": "claude-cli", "model": "claude-sonnet-4-6"},
            "audit": {
                "backend": "claude-cli",
                "model": "claude-opus-4-7",
                "delivery": "always",
                "severity_threshold": "low",
                "timeout_s": 60,
            },
        },
    )

    text = runtime._build_status_text(detailed=False)

    assert "🔀 Mode: audit" in text
    assert "⚙️ Active backend: codex-cli • gpt-5.5" in text
    assert "🧪 Audit" in text
    assert "• Core: claude-cli / claude-sonnet-4-6" in text
    assert "• Auditor: claude-cli / claude-opus-4-7" in text
    assert "• Delivery: always" in text
    assert "• Threshold: low" in text
    assert "• Timeout: 60s" in text


def test_status_text_shows_wrapper_model_configuration():
    runtime = _make_status_runtime(
        "wrapper",
        {
            "core": {"backend": "codex-cli", "model": "gpt-5.5"},
            "wrapper": {"backend": "claude-cli", "model": "claude-haiku-4-5", "context_window": 3},
            "wrapper_slots": {"1": "Warm.", "2": "Concise."},
        },
    )

    text = runtime._build_status_text(detailed=False)

    assert "🔀 Mode: wrapper" in text
    assert "⚙️ Active backend: codex-cli • gpt-5.5" in text
    assert "🎭 Wrapper" in text
    assert "• Core: codex-cli / gpt-5.5" in text
    assert "• Wrapper: claude-cli / claude-haiku-4-5" in text
    assert "• Context window: 3" in text
    assert "• Slots: 3 configured" in text


def test_status_full_includes_audit_criteria_and_wrapper_slots():
    audit_runtime = _make_status_runtime(
        "audit",
        {
            "audit_criteria": {"1": "Catch tool-risk drift."},
        },
    )
    wrapper_runtime = _make_status_runtime(
        "wrapper",
        {
            "wrapper_slots": {"1": "Be concise."},
        },
    )

    audit_detailed = audit_runtime._build_status_text(detailed=True)
    assert "🧪 Audit Criteria:\n• 1: Catch tool-risk drift." in audit_detailed
    assert f"• 9: {DEFAULT_AUDIT_CRITERION_SLOT_TEXT}" in audit_detailed
    detailed = wrapper_runtime._build_status_text(detailed=True)
    assert "🎭 Wrapper Slots:\n• 1: Be concise." in detailed
    assert f"• 9: {DEFAULT_WRAPPER_STYLE_SLOT_TEXT}" in detailed


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

    def append_transcript(self, role, text, source="text"):
        self.transcript.append((role, text, source))

    def refresh_recent_context(self):
        self.refresh_count += 1


class FakeProjectChatLogger:
    def __init__(self):
        self.exchanges = []

    def log_exchange(self, prompt, response, source):
        self.exchanges.append((prompt, response, source))


def _make_background_runtime(tmp_path: Path, wrapper_response: BackendResponse | None = None):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.is_shutting_down = False
    runtime.name = "zelda"
    runtime.config = SimpleNamespace(active_backend="codex-cli", extra={})
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
    runtime.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    runtime.error_logger = SimpleNamespace(error=lambda *a, **k: None, exception=lambda *a, **k: None)
    runtime._request_listeners = {}
    runtime._pending_request_results = {}
    runtime._suppressed_transfer_results = []
    runtime._post_turn_observers = []
    runtime._pre_turn_context_providers = []

    async def fake_wrapper_response(**kwargs):
        return wrapper_response or BackendResponse(text="wrapped visible", duration_ms=1.0)

    runtime.backend_manager = SimpleNamespace(
        agent_mode="wrapper",
        get_state_snapshot=lambda: {
            "wrapper": {"backend": "claude-cli", "model": "claude-haiku-4-5", "context_window": 1},
            "wrapper_slots": {"1": "Be warm."},
        },
        generate_ephemeral_response=fake_wrapper_response,
    )
    runtime._mark_success = lambda: None
    runtime._mark_error = lambda error: None
    runtime._record_habit_outcome = lambda *a, **k: None
    runtime._should_buffer_during_transfer = lambda request_id: False
    runtime._record_suppressed_transfer_result = lambda item, **kwargs: runtime._suppressed_transfer_results.append(kwargs)
    runtime.get_current_model = lambda: "gpt-5.5"
    runtime._get_system_prompt_text = lambda: "system"
    runtime.typing_loop = FlexibleAgentRuntime.typing_loop.__get__(runtime, FlexibleAgentRuntime)
    runtime._wrapper_enabled = FlexibleAgentRuntime._wrapper_enabled.__get__(runtime, FlexibleAgentRuntime)
    runtime._wrapper_timeout_s = FlexibleAgentRuntime._wrapper_timeout_s.__get__(runtime, FlexibleAgentRuntime)
    runtime._wrapper_visible_context = FlexibleAgentRuntime._wrapper_visible_context.__get__(runtime, FlexibleAgentRuntime)
    runtime._wrapper_audit_fields = FlexibleAgentRuntime._wrapper_audit_fields.__get__(runtime, FlexibleAgentRuntime)
    runtime._core_memory_assistant_text = FlexibleAgentRuntime._core_memory_assistant_text.__get__(
        runtime, FlexibleAgentRuntime
    )
    runtime._format_wrapper_verbose_trace = FlexibleAgentRuntime._format_wrapper_verbose_trace.__get__(runtime, FlexibleAgentRuntime)
    runtime._send_wrapper_verbose_trace = FlexibleAgentRuntime._send_wrapper_verbose_trace.__get__(runtime, FlexibleAgentRuntime)
    runtime._append_core_transcript = FlexibleAgentRuntime._append_core_transcript.__get__(runtime, FlexibleAgentRuntime)
    runtime._send_wrapper_polishing_placeholder = FlexibleAgentRuntime._send_wrapper_polishing_placeholder.__get__(runtime, FlexibleAgentRuntime)
    runtime._delete_wrapper_polishing_placeholder = FlexibleAgentRuntime._delete_wrapper_polishing_placeholder.__get__(runtime, FlexibleAgentRuntime)
    runtime._apply_wrapper_to_visible_text = FlexibleAgentRuntime._apply_wrapper_to_visible_text.__get__(runtime, FlexibleAgentRuntime)
    runtime._notify_request_listeners = FlexibleAgentRuntime._notify_request_listeners.__get__(runtime, FlexibleAgentRuntime)
    sent = []
    voices = []

    async def send_long_message(chat_id, text, request_id=None, purpose=None):
        sent.append({"chat_id": chat_id, "text": text, "request_id": request_id, "purpose": purpose})
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

    async def send_message(self, chat_id, text, parse_mode=None):
        self.messages.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})
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


def _make_foreground_runtime(tmp_path: Path, wrapper_response: BackendResponse | None = None):
    runtime, sent, voices = _make_background_runtime(tmp_path, wrapper_response=wrapper_response)
    runtime.queue = asyncio.Queue()
    runtime.app = SimpleNamespace(bot=FakeBot())
    runtime.telegram_logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    runtime.context_assembler = FakeContextAssembler()
    runtime.backend_manager.current_backend = SimpleNamespace(_session_id=None)
    runtime.backend_manager.generate_response = lambda *a, **k: _completed_task(
        BackendResponse(text="core raw foreground", duration_ms=1.0)
    )
    runtime._remote_backend_block_reason = lambda source: None
    runtime._mark_activity = lambda: None
    runtime._log_maintenance = lambda *a, **k: None
    runtime._consume_session_primer = lambda item: item.prompt
    runtime._build_habit_sections = lambda item, effective_prompt: ([], [])
    runtime._workzone_prompt_section = lambda: []
    runtime.get_typing_placeholder = lambda: ("typing", None)
    runtime.typing_loop = lambda chat_id, stop_event: stop_event.wait()
    runtime._strip_transfer_accept_prefix = lambda item, text: text
    runtime._verbose = False
    runtime._think = False
    runtime._cos_enabled = False
    runtime.is_generating = False
    runtime.current_request_meta = None
    hchat_replies = []

    async def hchat_route_reply(item, text):
        hchat_replies.append({"request_id": item.request_id, "text": text, "source": item.source})

    runtime._hchat_route_reply = hchat_route_reply
    runtime.process_queue = FlexibleAgentRuntime.process_queue.__get__(runtime, FlexibleAgentRuntime)
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


def _queued_request_from(source: str) -> QueuedRequest:
    item = _queued_request()
    item.source = source
    return item


async def _completed_task(response):
    return response


def _make_audit_followup_runtime(tmp_path: Path, *, item_silent: bool = False, delivery: str = "always"):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.config = SimpleNamespace(active_backend="codex-cli")
    runtime.workspace_dir = tmp_path
    runtime.audit_transcript_log_path = tmp_path / "audit_transcript.jsonl"
    runtime._background_tasks = set()
    runtime.error_logger = SimpleNamespace(warning=lambda *a, **k: None)
    runtime.get_current_model = lambda: "gpt-5.5"
    runtime._audit_visible_context = lambda context_window: []
    runtime._should_buffer_during_transfer = lambda request_id: False
    sent = []
    audit_calls = []

    async def send_long_message(chat_id, text, request_id=None, purpose=None):
        sent.append({"chat_id": chat_id, "text": text, "request_id": request_id, "purpose": purpose})
        return 0.0, 1

    async def generate_ephemeral_response(**kwargs):
        audit_calls.append(kwargs)
        await asyncio.sleep(0)
        return BackendResponse(
            text=json.dumps(
                {
                    "status": "warn",
                    "max_severity": "medium",
                    "triggered_sensors": ["destructive_action"],
                    "summary": "shell risk",
                    "findings": [
                        {
                            "severity": "medium",
                            "category": "destructive_action",
                            "evidence": "shell_exec event",
                            "recommendation": "confirm before running",
                        }
                    ],
                }
            ),
            duration_ms=1.0,
        )

    runtime.backend_manager = SimpleNamespace(
        agent_mode="audit",
        get_state_snapshot=lambda: {
            "audit": {
                "backend": "claude-cli",
                "model": "claude-sonnet-4-6",
                "delivery": delivery,
                "severity_threshold": "low",
                "timeout_s": 5,
            },
            "audit_criteria": {"1": "Flag shell commands."},
        },
        generate_ephemeral_response=generate_ephemeral_response,
    )
    runtime.send_long_message = send_long_message
    item = QueuedRequest(
        request_id="audit-req-001",
        chat_id=123,
        prompt="please run a shell command",
        source="text",
        summary="shell",
        created_at=datetime.now().isoformat(),
        silent=item_silent,
    )
    item.deliver_to_telegram = True
    response = BackendResponse(text="core response", duration_ms=1.0)
    collector = AuditTelemetryCollector()
    return runtime, item, response, collector, sent, audit_calls


def test_write_audit_evidence_sanitizes_path_and_writes_full_record(tmp_path):
    runtime, item, response, collector, sent, audit_calls = _make_audit_followup_runtime(tmp_path)
    item.request_id = "req/with spaces:001"

    path = FlexibleAgentRuntime._write_audit_evidence(
        runtime,
        item,
        core_raw="core raw",
        visible_text="visible",
        telemetry={"action_event_count": 2},
        completion_path="test",
        audit_criteria={"1": "Flag risky actions."},
        visible_context=[{"role": "user", "text": "hi", "source": "text"}],
    )

    assert path.endswith("req_with_spaces_001.json")
    evidence = json.loads(Path(path).read_text(encoding="utf-8"))
    assert evidence["user_request"] == item.prompt
    assert evidence["core_raw"] == "core raw"
    assert evidence["telemetry"]["action_event_count"] == 2
    assert evidence["audit_criteria"]["1"] == "Flag risky actions."
    assert "core_transcript" in evidence["related_logs"]


def test_write_audit_evidence_failure_returns_empty_string(tmp_path):
    runtime, item, response, collector, sent, audit_calls = _make_audit_followup_runtime(tmp_path)
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("block", encoding="utf-8")
    runtime.workspace_dir = blocker

    path = FlexibleAgentRuntime._write_audit_evidence(
        runtime,
        item,
        core_raw="core raw",
        visible_text="visible",
        telemetry={},
        completion_path="test",
        audit_criteria=None,
        visible_context=[],
    )

    assert path == ""


@pytest.mark.asyncio
async def test_wrapper_only_commands_reject_flex_mode(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    runtime, messages = _make_runtime(manager)
    update, context = _update(["backend=codex-cli"])

    await FlexibleAgentRuntime.cmd_core(runtime, update, context)

    assert messages
    assert "only applies in **wrapper** or **audit** mode" in messages[-1]
    assert not (tmp_path / "agent" / "state.json").exists()


@pytest.mark.asyncio
async def test_audit_followup_is_scheduled_after_core_delivery_and_tracked(tmp_path):
    runtime, item, response, collector, sent, audit_calls = _make_audit_followup_runtime(tmp_path)
    await collector.record(StreamEvent(kind=KIND_SHELL_EXEC, summary="Running command", detail="rm -rf /tmp/demo"))

    sent.append({"purpose": "response", "text": response.text})
    FlexibleAgentRuntime._schedule_audit_followup(
        runtime,
        item,
        core_raw=response.text,
        visible_text=response.text,
        response=response,
        audit_collector=collector,
        completion_path="test",
    )

    assert len(runtime._background_tasks) == 1
    await asyncio.gather(*list(runtime._background_tasks))
    await asyncio.sleep(0)

    assert audit_calls
    evidence_path = tmp_path / "audit_evidence" / "audit-req-001.json"
    assert evidence_path.exists()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["core_raw"] == "core response"
    assert evidence["telemetry"]["action_event_count"] == 1
    assert str(evidence_path) in audit_calls[0]["prompt"]
    assert "Primary current-turn evidence file" in audit_calls[0]["prompt"]
    assert [entry["purpose"] for entry in sent] == ["response", "audit-report"]
    assert "shell_exec event" in sent[-1]["text"]
    audit_log = tmp_path / "audit_transcript.jsonl"
    assert audit_log.exists()
    entry = json.loads(audit_log.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["audit_result"]["max_severity"] == "medium"
    assert entry["telemetry"]["action_event_count"] == 1
    assert runtime._background_tasks == set()


@pytest.mark.asyncio
async def test_silent_audit_writes_transcript_without_user_notification(tmp_path):
    runtime, item, response, collector, sent, audit_calls = _make_audit_followup_runtime(tmp_path, item_silent=True)
    await collector.record(StreamEvent(kind=KIND_SHELL_EXEC, summary="Running command"))

    FlexibleAgentRuntime._schedule_audit_followup(
        runtime,
        item,
        core_raw=response.text,
        visible_text=response.text,
        response=response,
        audit_collector=collector,
        completion_path="test",
    )

    await asyncio.gather(*list(runtime._background_tasks))
    await asyncio.sleep(0)

    assert audit_calls
    assert sent == []
    assert (tmp_path / "audit_transcript.jsonl").exists()


@pytest.mark.asyncio
async def test_cmd_mode_wrapper_persists_mode(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    runtime, messages = _make_runtime(manager)
    update, context = _update(["wrapper"])

    await FlexibleAgentRuntime.cmd_mode(runtime, update, context)

    state = _read_state(tmp_path / "agent")
    assert state["agent_mode"] == "wrapper"
    assert state["active_backend"] == "codex-cli"
    assert state["active_model"] == "gpt-5.5"
    assert "Switched to **wrapper** mode" in messages[-1]


@pytest.mark.asyncio
async def test_cmd_mode_wrapper_does_not_activate_when_core_switch_fails(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    runtime, messages = _make_runtime(manager)

    async def fail_switch(chat_id, target_engine, target_model=None, with_context=False):
        return False, "Backend not allowed"

    runtime._switch_backend_mode = fail_switch
    update, context = _update(["wrapper"])

    await FlexibleAgentRuntime.cmd_mode(runtime, update, context)

    assert manager.agent_mode == "flex"
    assert not (tmp_path / "agent" / "state.json").exists()
    assert "Wrapper mode was not activated" in messages[-1]
    assert "Backend not allowed" in messages[-1]


@pytest.mark.asyncio
async def test_backend_and_model_commands_guide_wrapper_mode(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    manager.agent_mode = "wrapper"
    manager.current_backend = SimpleNamespace(config=SimpleNamespace(model="gpt-5.5"))
    runtime, messages = _make_runtime(manager)

    update, context = _update([])
    await FlexibleAgentRuntime.cmd_backend(runtime, update, context)
    assert "/core" in messages[-1]
    assert "/wrap" in messages[-1]

    await FlexibleAgentRuntime.cmd_model(runtime, update, context)
    assert "/core" in messages[-1]
    assert "/wrap" in messages[-1]


@pytest.mark.asyncio
async def test_cmd_core_updates_wrapper_core_state(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    manager.agent_mode = "wrapper"
    runtime, messages = _make_runtime(manager)
    update, context = _update(["backend=codex-cli", "model=gpt-5.5"])

    await FlexibleAgentRuntime.cmd_core(runtime, update, context)

    state = _read_state(tmp_path / "agent")
    assert state["agent_mode"] == "wrapper"
    assert state["core"] == {"backend": "codex-cli", "model": "gpt-5.5"}
    assert state["active_backend"] == "codex-cli"
    assert state["active_model"] == "gpt-5.5"
    assert "Wrapper core updated" in messages[-1]


@pytest.mark.asyncio
async def test_cmd_wrap_updates_wrapper_translator_state(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    manager.agent_mode = "wrapper"
    runtime, messages = _make_runtime(manager)
    update, context = _update(["backend=claude-cli", "model=claude-haiku-4-5", "context=5"])

    await FlexibleAgentRuntime.cmd_wrap(runtime, update, context)

    state = _read_state(tmp_path / "agent")
    assert state["wrapper"] == {
        "backend": "claude-cli",
        "model": "claude-haiku-4-5",
        "context_window": 5,
        "fallback": "passthrough",
    }
    assert "Wrapper translator updated" in messages[-1]


@pytest.mark.asyncio
async def test_wrapper_config_status_commands_include_clickable_buttons(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    manager.agent_mode = "wrapper"
    runtime, messages = _make_runtime(manager)
    update, context = _update([])

    await FlexibleAgentRuntime.cmd_core(runtime, update, context)
    assert "Tap a button" in messages[-1]
    assert runtime._reply_payloads[-1]["reply_markup"] is not None
    assert "wcfg:core:codex-cli:gpt-5.5" in str(runtime._reply_payloads[-1]["reply_markup"])

    await FlexibleAgentRuntime.cmd_wrap(runtime, update, context)
    assert "Tap a provider/model button" in messages[-1]
    assert "Buttons are grouped by provider" in messages[-1]
    assert "Each model button changes both wrapper backend and model" in messages[-1]
    assert "Context buttons only change" in messages[-1]
    assert runtime._reply_payloads[-1]["reply_markup"] is not None
    wrap_markup = str(runtime._reply_payloads[-1]["reply_markup"])
    assert "wcfg:wrapid:claude_haiku" in wrap_markup
    assert "wcfg:wrapid:gemini_flash" in wrap_markup
    assert "wcfg:wrapid:deepseek_chat" in wrap_markup
    assert "wcfg:wrapid:or_deepseek" in wrap_markup
    assert "wcfg:wrapctx:3" in wrap_markup

    await FlexibleAgentRuntime.cmd_wrapper(runtime, update, context)
    assert "Tap buttons below" in messages[-1]
    assert runtime._reply_payloads[-1]["reply_markup"] is not None
    assert "wcfg:menu:core" in str(runtime._reply_payloads[-1]["reply_markup"])


@pytest.mark.asyncio
async def test_wrapper_config_buttons_update_core_model(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    manager.agent_mode = "wrapper"
    runtime, _messages = _make_runtime(manager)
    edits = []
    answers = []

    query = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        data="wcfg:core:codex-cli:gpt-5.4",
        message=SimpleNamespace(chat_id=123),
    )

    async def edit_message_text(text, **kwargs):
        edits.append({"text": text, **kwargs})

    async def answer(text=None, **kwargs):
        answers.append({"text": text, **kwargs})

    query.edit_message_text = edit_message_text
    query.answer = answer
    update = SimpleNamespace(callback_query=query)

    await FlexibleAgentRuntime.callback_wrapper_config(runtime, update, SimpleNamespace())

    state = _read_state(tmp_path / "agent")
    assert state["core"] == {"backend": "codex-cli", "model": "gpt-5.4"}
    assert state["active_backend"] == "codex-cli"
    assert state["active_model"] == "gpt-5.4"
    assert "Wrapper core updated" in edits[-1]["text"]
    assert edits[-1]["reply_markup"] is not None
    assert answers[-1]["text"] is None


@pytest.mark.asyncio
async def test_audit_core_status_uses_audit_keyboard(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    manager.agent_mode = "audit"
    runtime, messages = _make_runtime(manager)
    update, context = _update([])

    await FlexibleAgentRuntime.cmd_core(runtime, update, context)

    assert "Audit core model" in messages[-1]
    assert "Wrapper core model" not in messages[-1]
    assert "This is the model that does the actual work" in messages[-1]
    markup = str(runtime._reply_payloads[-1]["reply_markup"])
    assert "Claude Opus 4.7" in markup
    assert "Claude Opus 4.6" in markup
    assert "acfg:coreid:codex_gpt55" in markup
    assert "acfg:coreid:claude_sonnet" in markup
    assert "acfg:menu:auditmodel" in markup
    assert "wcfg:" not in markup


@pytest.mark.asyncio
async def test_audit_config_buttons_update_core_model(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    manager.agent_mode = "audit"
    runtime, _messages = _make_runtime(manager)
    edits = []
    answers = []

    query = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        data="acfg:coreid:claude_sonnet",
        message=SimpleNamespace(chat_id=123),
    )

    async def edit_message_text(text, **kwargs):
        edits.append({"text": text, **kwargs})

    async def answer(text=None, **kwargs):
        answers.append({"text": text, **kwargs})

    query.edit_message_text = edit_message_text
    query.answer = answer
    update = SimpleNamespace(callback_query=query)

    await FlexibleAgentRuntime.callback_audit_config(runtime, update, SimpleNamespace())

    state = _read_state(tmp_path / "agent")
    assert state["core"] == {"backend": "claude-cli", "model": "claude-sonnet-4-6"}
    assert state["active_backend"] == "claude-cli"
    assert state["active_model"] == "claude-sonnet-4-6"
    assert "Audit core updated" in edits[-1]["text"]
    assert edits[-1]["reply_markup"] is not None
    assert answers[-1]["text"] is None


@pytest.mark.asyncio
async def test_audit_model_menu_and_buttons_update_auditor_model(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    manager.agent_mode = "audit"
    runtime, messages = _make_runtime(manager)
    update, context = _update(["model"])

    await FlexibleAgentRuntime.cmd_audit(runtime, update, context)

    assert "Audit model:" in messages[-1]
    assert "This model reviews the core model" in messages[-1]
    markup = str(runtime._reply_payloads[-1]["reply_markup"])
    assert "Claude Opus 4.7" in markup
    assert "Claude Opus 4.6" in markup
    assert "acfg:auditid:claude_opus" in markup
    assert "acfg:auditid:claude_sonnet" in markup
    assert "acfg:menu:core" in markup

    edits = []
    answers = []
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        data="acfg:auditid:claude_opus",
        message=SimpleNamespace(chat_id=123),
    )

    async def edit_message_text(text, **kwargs):
        edits.append({"text": text, **kwargs})

    async def answer(text=None, **kwargs):
        answers.append({"text": text, **kwargs})

    query.edit_message_text = edit_message_text
    query.answer = answer
    await FlexibleAgentRuntime.callback_audit_config(runtime, SimpleNamespace(callback_query=query), SimpleNamespace())

    state = _read_state(tmp_path / "agent")
    assert state["audit"]["backend"] == "claude-cli"
    assert state["audit"]["model"] == "claude-opus-4-7"
    assert "Audit model updated" in edits[-1]["text"]
    assert edits[-1]["reply_markup"] is not None
    assert answers[-1]["text"] is None


def test_audit_model_choice_labels_include_versions():
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime._allowed_wrapper_engine = lambda _backend: True
    runtime._get_available_models_for = lambda _backend: []

    core_labels = {choice[1] for choice in FlexibleAgentRuntime._audit_core_model_choices(runtime)}
    auditor_labels = {choice[1] for choice in FlexibleAgentRuntime._audit_auditor_model_choices(runtime)}

    for labels in (core_labels, auditor_labels):
        assert "Claude Opus 4.7" in labels
        assert "Claude Opus 4.6" in labels
        assert "Claude Sonnet 4.6" in labels
        assert "OR Sonnet 4.6" in labels
        assert "OR Opus 4.6" in labels


@pytest.mark.asyncio
async def test_audit_status_menu_exposes_delivery_and_threshold_buttons(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    manager.agent_mode = "audit"
    runtime, messages = _make_runtime(manager)
    update, context = _update([])

    await FlexibleAgentRuntime.cmd_audit(runtime, update, context)

    assert "Default testing posture" in messages[-1]
    assert "unauthorized external/API calls" in messages[-1]
    assert "verified or safely tested outcomes" in messages[-1]
    markup = str(runtime._reply_payloads[-1]["reply_markup"])
    assert "acfg:delivery:always" in markup
    assert "acfg:delivery:issues_only" in markup
    assert "acfg:threshold:low" in markup
    assert "acfg:threshold:critical" in markup


@pytest.mark.asyncio
async def test_audit_config_buttons_update_delivery_and_threshold(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    manager.agent_mode = "audit"
    runtime, _messages = _make_runtime(manager)
    edits = []
    answers = []

    async def edit_message_text(text, **kwargs):
        edits.append({"text": text, **kwargs})

    async def answer(text=None, **kwargs):
        answers.append({"text": text, **kwargs})

    for data in ("acfg:delivery:issues_only", "acfg:threshold:critical"):
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            data=data,
            message=SimpleNamespace(chat_id=123),
        )
        query.edit_message_text = edit_message_text
        query.answer = answer
        await FlexibleAgentRuntime.callback_audit_config(runtime, SimpleNamespace(callback_query=query), SimpleNamespace())

    state = _read_state(tmp_path / "agent")
    assert state["audit"]["delivery"] == "issues_only"
    assert state["audit"]["severity_threshold"] == "critical"
    assert edits
    assert answers[-1]["text"] is None


@pytest.mark.asyncio
async def test_cmd_mode_audit_preserves_wrapper_and_unrelated_state(tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    (workspace / "state.json").write_text(
        json.dumps(
            {
                "active_backend": "codex-cli",
                "agent_mode": "flex",
                "wrapper": {"backend": "claude-cli", "model": "claude-haiku-4-5"},
                "wrapper_slots": {"1": "Keep tone warm."},
                "unrelated": {"keep": True},
            }
        ),
        encoding="utf-8",
    )
    manager = _make_manager(workspace)
    runtime, messages = _make_runtime(manager)
    update, context = _update(["audit"])

    await FlexibleAgentRuntime.cmd_mode(runtime, update, context)

    state = _read_state(workspace)
    assert state["agent_mode"] == "audit"
    assert state["wrapper"] == {"backend": "claude-cli", "model": "claude-haiku-4-5"}
    assert state["wrapper_slots"] == {"1": "Keep tone warm."}
    assert state["unrelated"] == {"keep": True}
    assert "Switched to **audit** mode" in messages[-1]


@pytest.mark.asyncio
async def test_cmd_new_queues_session_reset_source_for_wrapper(tmp_path):
    manager = _make_manager(tmp_path)
    manager.agent_mode = "wrapper"
    runtime, messages = _make_runtime(manager)
    handled = []
    queued = []

    async def handle_new_session():
        handled.append(True)

    manager.current_backend = SimpleNamespace(
        capabilities=SimpleNamespace(supports_sessions=True),
        handle_new_session=handle_new_session,
    )
    runtime._clear_transfer_state = lambda: None
    runtime._pending_auto_recall_context = "old"
    runtime.context_assembler = SimpleNamespace(memory_store=SimpleNamespace(clear_turns=lambda: None))

    async def enqueue_request(chat_id, prompt, source, summary, **kwargs):
        queued.append(
            {
                "chat_id": chat_id,
                "prompt": prompt,
                "source": source,
                "summary": summary,
                **kwargs,
            }
        )

    runtime.enqueue_request = enqueue_request
    update, context = _update([])

    await FlexibleAgentRuntime.cmd_new(runtime, update, context)

    assert handled == [True]
    assert messages[-1] == "Starting a fresh session..."
    assert queued[0]["source"] == SESSION_RESET_SOURCE
    assert queued[0]["skip_memory_injection"] is True


@pytest.mark.asyncio
async def test_wrapper_config_buttons_update_wrapper_model_across_backends(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    manager.config.allowed_backends.extend(
        [
            {"engine": "gemini-cli", "model": "gemini-2.5-flash"},
            {"engine": "deepseek-api", "model": "deepseek-chat"},
            {"engine": "openrouter-api", "model": "deepseek/deepseek-v3.2-exp"},
        ]
    )
    manager.agent_mode = "wrapper"
    runtime, _messages = _make_runtime(manager)
    edits = []
    answers = []

    query = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        data="wcfg:wrapid:or_deepseek:3",
        message=SimpleNamespace(chat_id=123),
    )

    async def edit_message_text(text, **kwargs):
        edits.append({"text": text, **kwargs})

    async def answer(text=None, **kwargs):
        answers.append({"text": text, **kwargs})

    query.edit_message_text = edit_message_text
    query.answer = answer
    update = SimpleNamespace(callback_query=query)

    await FlexibleAgentRuntime.callback_wrapper_config(runtime, update, SimpleNamespace())

    state = _read_state(tmp_path / "agent")
    assert state["wrapper"] == {
        "backend": "openrouter-api",
        "model": "deepseek/deepseek-v3.2-exp",
        "context_window": 3,
        "fallback": "passthrough",
    }
    assert "Wrapper translator updated" in edits[-1]["text"]
    assert "openrouter-api" in edits[-1]["text"]
    assert edits[-1]["reply_markup"] is not None
    assert answers[-1]["text"] is None


@pytest.mark.asyncio
async def test_cmd_wrapper_set_list_and_clear_slots(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    manager.agent_mode = "wrapper"
    runtime, messages = _make_runtime(manager)

    update, context = _update(["set", "1", "Be", "warm"])
    await FlexibleAgentRuntime.cmd_wrapper(runtime, update, context)

    state = _read_state(tmp_path / "agent")
    assert state["wrapper_slots"] == {"1": "Be warm"}
    assert "updated" in messages[-1]

    update, context = _update(["list"])
    await FlexibleAgentRuntime.cmd_wrapper(runtime, update, context)
    assert "Be warm" in messages[-1]
    assert DEFAULT_WRAPPER_STYLE_SLOT_TEXT in messages[-1]

    update, context = _update(["clear", "1"])
    await FlexibleAgentRuntime.cmd_wrapper(runtime, update, context)

    state = _read_state(tmp_path / "agent")
    assert state["wrapper_slots"] == {}

    update, context = _update(["clear", "9"])
    await FlexibleAgentRuntime.cmd_wrapper(runtime, update, context)

    state = _read_state(tmp_path / "agent")
    assert state["wrapper_slots"] == {"9": ""}
    update, context = _update(["list"])
    await FlexibleAgentRuntime.cmd_wrapper(runtime, update, context)
    assert DEFAULT_WRAPPER_STYLE_SLOT_TEXT not in messages[-1]


@pytest.mark.asyncio
async def test_reset_preserves_wrapper_config_and_sys_prompts(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    manager.agent_mode = "wrapper"
    manager.config.active_backend = "codex-cli"
    manager._active_model_override = "gpt-5.5"
    manager.update_wrapper_blocks(
        core={"backend": "codex-cli", "model": "gpt-5.5"},
        wrapper={"backend": "claude-cli", "model": "claude-haiku-4-5", "context_window": 3},
        wrapper_slots={"1": "Keep the Xishi persona."},
    )
    workspace = manager.config.workspace_dir
    (workspace / "agent.md").write_text("identity", encoding="utf-8")
    (workspace / "sys_prompts.json").write_text('{"slots":[]}', encoding="utf-8")
    (workspace / "transcript.jsonl").write_text("old transcript", encoding="utf-8")
    (workspace / "memory").mkdir(exist_ok=True)
    (workspace / "memory" / "old.txt").write_text("old memory", encoding="utf-8")

    runtime, messages = _make_runtime(manager)
    runtime.name = "test-flex"
    runtime.workspace_dir = workspace
    runtime.global_config = manager.global_config
    runtime.sys_prompt_manager = None
    runtime.logger = SimpleNamespace(warning=lambda *a, **k: None)
    runtime._get_active_skill_sections = lambda: []
    runtime._get_agent_class = lambda: "persona"
    runtime._pending_auto_recall_context = "old"
    runtime._pending_session_primer = "old"
    runtime._clear_transfer_state = lambda: None
    update, context = _update(["CONFIRM"])

    await FlexibleAgentRuntime.cmd_reset(runtime, update, context)

    state = _read_state(workspace)
    assert state["agent_mode"] == "wrapper"
    assert state["active_backend"] == "codex-cli"
    assert state["active_model"] == "gpt-5.5"
    assert state["core"] == {"backend": "codex-cli", "model": "gpt-5.5"}
    assert state["wrapper"] == {"backend": "claude-cli", "model": "claude-haiku-4-5", "context_window": 3}
    assert state["wrapper_slots"] == {"1": "Keep the Xishi persona."}
    assert (workspace / "agent.md").exists()
    assert (workspace / "sys_prompts.json").exists()
    assert not (workspace / "transcript.jsonl").exists()
    assert not (workspace / "memory" / "old.txt").exists()
    assert "wrapper config are intact" in messages[-1]


@pytest.mark.asyncio
async def test_foreground_completion_uses_wrapper_output_for_visible_surfaces(tmp_path):
    runtime, sent, voices, hchat_replies = _make_foreground_runtime(tmp_path)
    listener_payloads = []
    runtime.register_request_listener = FlexibleAgentRuntime.register_request_listener.__get__(runtime, FlexibleAgentRuntime)
    runtime.register_request_listener("req-001", lambda payload: listener_payloads.append(payload))
    item = _queued_request()
    await runtime.queue.put(item)

    task = asyncio.create_task(runtime.process_queue())
    try:
        for _ in range(50):
            if sent and voices and listener_payloads and hchat_replies:
                break
            await asyncio.sleep(0.01)
        assert sent[0]["text"] == "wrapped visible"
        assert voices[0]["text"] == "wrapped visible"
        assert listener_payloads[0]["text"] == "wrapped visible"
        assert listener_payloads[0]["visible_text"] == "wrapped visible"
        assert listener_payloads[0]["core_raw"] == "core raw foreground"
        assert listener_payloads[0]["wrapper_used"] is True
        assert runtime.last_response["text"] == "wrapped visible"
        assert ("assistant", "codex-cli", "core raw foreground") in runtime.memory_store.turns
        assert ("hello", "core raw foreground", "text") in runtime.memory_store.exchanges
        assert ("assistant", "wrapped visible", "text") in runtime.handoff_builder.transcript
        assert runtime.project_chat_logger.exchanges[0] == ("hello", "wrapped visible", "text")
        assert hchat_replies[0]["text"] == "wrapped visible"
        core_entry = json.loads((tmp_path / "core_transcript.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert core_entry["text"] == "core raw foreground"
        assert core_entry["visible_text"] == "wrapped visible"
        assert core_entry["completion_path"] == "foreground"
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_foreground_wrapper_verbose_shows_core_and_final_outputs(tmp_path):
    runtime, sent, voices, hchat_replies = _make_foreground_runtime(tmp_path)
    runtime._verbose = True
    item = _queued_request()
    await runtime.queue.put(item)

    task = asyncio.create_task(runtime.process_queue())
    try:
        for _ in range(50):
            if len(sent) >= 2 and voices and hchat_replies:
                break
            await asyncio.sleep(0.01)
        assert sent[0]["purpose"] == "wrapper-verbose"
        assert "Core output" in sent[0]["text"]
        assert "core raw foreground" in sent[0]["text"]
        assert "Wrapper final output" in sent[0]["text"]
        assert "wrapped visible" in sent[0]["text"]
        assert sent[-1]["purpose"] == "response"
        assert sent[-1]["text"] == "wrapped visible"
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_wrapper_polishing_placeholder_bridges_after_core_output(tmp_path):
    runtime, sent, voices, hchat_replies = _make_foreground_runtime(tmp_path)
    runtime.telegram_connected = True
    item = _queued_request()
    await runtime.queue.put(item)

    task = asyncio.create_task(runtime.process_queue())
    try:
        for _ in range(50):
            if sent and voices and hchat_replies and len(runtime.app.bot.messages) >= 2:
                break
            await asyncio.sleep(0.01)
        assert runtime.app.bot.messages[0]["text"] == "typing"
        assert runtime.app.bot.messages[1]["text"] == "✨ Polishing the final voice..."
        assert runtime.app.bot.deleted[-1]["message_id"] == 2
        assert sent[-1]["text"] == "wrapped visible"
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_background_completion_uses_wrapper_output_for_visible_surfaces(tmp_path):
    runtime, sent, voices = _make_background_runtime(tmp_path)
    listener_payloads = []
    runtime.register_request_listener = FlexibleAgentRuntime.register_request_listener.__get__(runtime, FlexibleAgentRuntime)
    runtime.register_request_listener("req-001", lambda payload: listener_payloads.append(payload))
    item = _queued_request()
    task = asyncio.create_task(_completed_task(BackendResponse(text="core raw", duration_ms=1.0)))
    await task

    await FlexibleAgentRuntime._on_background_complete(runtime, task, item)

    assert listener_payloads[0]["text"] == "wrapped visible"
    assert listener_payloads[0]["visible_text"] == "wrapped visible"
    assert listener_payloads[0]["core_raw"] == "core raw"
    assert listener_payloads[0]["wrapper_used"] is True
    assert runtime.last_response["text"] == "wrapped visible"
    assert ("assistant", "codex-cli", "core raw") in runtime.memory_store.turns
    assert ("hello", "core raw", "text") in runtime.memory_store.exchanges
    assert ("assistant", "wrapped visible", "text") in runtime.handoff_builder.transcript
    assert runtime.project_chat_logger.exchanges[0] == ("hello", "wrapped visible", "text")
    assert sent[0]["text"] == "wrapped visible"
    assert voices[0]["text"] == "wrapped visible"
    core_entry = json.loads((tmp_path / "core_transcript.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert core_entry["text"] == "core raw"
    assert core_entry["visible_text"] == "wrapped visible"
    assert core_entry["completion_path"] == "background"
    assert core_entry["wrapper_used"] is True


@pytest.mark.asyncio
async def test_background_transfer_suppression_buffers_wrapper_output(tmp_path):
    runtime, sent, voices = _make_background_runtime(tmp_path)
    runtime._should_buffer_during_transfer = lambda request_id: True
    item = _queued_request()
    task = asyncio.create_task(_completed_task(BackendResponse(text="core raw", duration_ms=1.0)))
    await task

    await FlexibleAgentRuntime._on_background_complete(runtime, task, item)

    assert runtime._suppressed_transfer_results == [{"success": True, "text": "wrapped visible"}]
    assert sent == []
    assert voices == []


@pytest.mark.asyncio
async def test_background_bypass_source_does_not_wrap(tmp_path):
    runtime, sent, voices = _make_background_runtime(tmp_path)
    listener_payloads = []
    runtime.register_request_listener = FlexibleAgentRuntime.register_request_listener.__get__(runtime, FlexibleAgentRuntime)
    runtime.register_request_listener("req-001", lambda payload: listener_payloads.append(payload))
    item = _queued_request_from("scheduler")
    task = asyncio.create_task(_completed_task(BackendResponse(text="core raw", duration_ms=1.0)))
    await task

    await FlexibleAgentRuntime._on_background_complete(runtime, task, item)

    assert listener_payloads[0]["text"] == "core raw"
    assert listener_payloads[0]["visible_text"] == "core raw"
    assert listener_payloads[0]["wrapper_used"] is False
    assert sent[0]["text"] == "core raw"
    assert voices[0]["text"] == "core raw"


@pytest.mark.asyncio
async def test_background_voice_source_wraps(tmp_path):
    runtime, sent, voices = _make_background_runtime(tmp_path)
    item = _queued_request_from("voice")
    task = asyncio.create_task(_completed_task(BackendResponse(text="core raw voice", duration_ms=1.0)))
    await task

    await FlexibleAgentRuntime._on_background_complete(runtime, task, item)

    assert sent[0]["text"] == "wrapped visible"
    assert voices[0]["text"] == "wrapped visible"


@pytest.mark.asyncio
async def test_background_hchat_source_bypasses_wrapper(tmp_path):
    runtime, sent, voices = _make_background_runtime(tmp_path)
    item = _queued_request_from("bridge:hchat")
    task = asyncio.create_task(_completed_task(BackendResponse(text="core raw hchat", duration_ms=1.0)))
    await task

    await FlexibleAgentRuntime._on_background_complete(runtime, task, item)

    assert sent[0]["text"] == "core raw hchat"
    assert voices[0]["text"] == "core raw hchat"


@pytest.mark.asyncio
async def test_background_wrapper_failure_falls_back_to_core_raw(tmp_path):
    runtime, sent, voices = _make_background_runtime(
        tmp_path,
        wrapper_response=BackendResponse(text="", duration_ms=1.0, error="wrapper failed", is_success=False),
    )
    listener_payloads = []
    runtime.register_request_listener = FlexibleAgentRuntime.register_request_listener.__get__(runtime, FlexibleAgentRuntime)
    runtime.register_request_listener("req-001", lambda payload: listener_payloads.append(payload))
    item = _queued_request()
    task = asyncio.create_task(_completed_task(BackendResponse(text="core raw fallback", duration_ms=1.0)))
    await task

    await FlexibleAgentRuntime._on_background_complete(runtime, task, item)

    assert listener_payloads[0]["text"] == "core raw fallback"
    assert listener_payloads[0]["wrapper_failed"] is True
    assert sent[0]["text"] == "core raw fallback"
    assert voices[0]["text"] == "core raw fallback"


def test_core_transcript_helper_records_foreground_path(tmp_path):
    runtime, _sent, _voices = _make_background_runtime(tmp_path)
    item = _queued_request()

    wrapper_result = SimpleNamespace(
        wrapper_used=True,
        wrapper_failed=False,
        fallback_reason=None,
    )
    runtime._append_core_transcript(
        item,
        core_raw="core raw foreground",
        visible_text="wrapped foreground",
        completion_path="foreground",
        wrapper_result=wrapper_result,
    )

    entry = json.loads((tmp_path / "core_transcript.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert entry["role"] == "assistant_core"
    assert entry["text"] == "core raw foreground"
    assert entry["visible_text"] == "wrapped foreground"
    assert entry["completion_path"] == "foreground"
    assert entry["wrapper_used"] is True
