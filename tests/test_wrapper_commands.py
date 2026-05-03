from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from adapters.base import BackendResponse
from orchestrator.agent_runtime import QueuedRequest
from orchestrator.config import FlexibleAgentConfig, GlobalConfig
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.flexible_backend_manager import FlexibleBackendManager


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
    runtime._is_authorized_user = lambda user_id: user_id == 1
    messages: list[str] = []

    async def _reply_text(update, text, **kwargs):
        messages.append(text)

    runtime._reply_text = _reply_text
    return runtime, messages


def _update(args: list[str] | None = None):
    return (
        SimpleNamespace(effective_user=SimpleNamespace(id=1)),
        SimpleNamespace(args=args or []),
    )


def _read_state(workspace: Path) -> dict:
    return json.loads((workspace / "state.json").read_text(encoding="utf-8"))


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
    runtime.last_response = None
    runtime.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    runtime.error_logger = SimpleNamespace(error=lambda *a, **k: None)
    runtime._request_listeners = {}
    runtime._pending_request_results = {}
    runtime._suppressed_transfer_results = []

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
    runtime._append_core_transcript = FlexibleAgentRuntime._append_core_transcript.__get__(runtime, FlexibleAgentRuntime)
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


@pytest.mark.asyncio
async def test_wrapper_only_commands_reject_flex_mode(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    runtime, messages = _make_runtime(manager)
    update, context = _update(["backend=codex-cli"])

    await FlexibleAgentRuntime.cmd_core(runtime, update, context)

    assert messages
    assert "only applies in **wrapper** mode" in messages[-1]
    assert not (tmp_path / "agent" / "state.json").exists()


@pytest.mark.asyncio
async def test_cmd_mode_wrapper_persists_mode(tmp_path):
    manager = _make_manager(tmp_path / "agent")
    runtime, messages = _make_runtime(manager)
    update, context = _update(["wrapper"])

    await FlexibleAgentRuntime.cmd_mode(runtime, update, context)

    state = _read_state(tmp_path / "agent")
    assert state["agent_mode"] == "wrapper"
    assert "Switched to **wrapper** mode" in messages[-1]


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

    update, context = _update(["clear", "1"])
    await FlexibleAgentRuntime.cmd_wrapper(runtime, update, context)

    state = _read_state(tmp_path / "agent")
    assert state["wrapper_slots"] == {}


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
    assert ("assistant", "codex-cli", "wrapped visible") in runtime.memory_store.turns
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
