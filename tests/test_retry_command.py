from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import runtime_control, runtime_retry
from orchestrator.handoff_builder import HandoffBuilder
from orchestrator.multimodal_contract import canonical_request_content
from orchestrator.runtime_command_binding import BOT_COMMAND_BINDINGS, COMMAND_BINDINGS


class _MemoryStore:
    def __init__(self) -> None:
        self.clear_count = 0

    def clear_turns(self) -> None:
        self.clear_count += 1


class _Handoff:
    def __init__(self, *, prompt: str = "HANDOFF CONTEXT", exchanges: int = 3) -> None:
        self.prompt = prompt
        self.exchanges = exchanges
        self.refreshed = 0
        self.built = 0

    def refresh_recent_context(self) -> None:
        self.refreshed += 1

    def build_handoff(self) -> None:
        self.built += 1

    def build_session_restore_prompt(self, **_kwargs):
        if not self.prompt:
            return "", 0, 0
        return self.prompt, self.exchanges, 420


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, message, *args, **_kwargs) -> None:
        self.messages.append(str(message) % args if args else str(message))

    def exception(self, message, *args, **_kwargs) -> None:
        self.messages.append(str(message) % args if args else str(message))


def _update(chat_id: int = 42):
    message = SimpleNamespace(text="/retry")
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=message,
        message=message,
    )


def _runtime(
    tmp_path: Path,
    *,
    engine: str = "grok-cli",
    current_prompt: str | None = "Finish the security cleanup",
    handoff_prompt: str = "HANDOFF CONTEXT",
):
    replies: list[str] = []
    enqueued: list[tuple[tuple, dict]] = []
    shutdown = AsyncMock()
    handle_new_session = AsyncMock()
    store = _MemoryStore()
    assembler = SimpleNamespace(
        memory_store=store,
        turns_injection_enabled=True,
        saved_memory_injection_enabled=True,
    )
    backend = SimpleNamespace(
        shutdown=shutdown,
        handle_new_session=handle_new_session,
        capabilities=SimpleNamespace(supports_sessions=engine.endswith("-cli")),
        current_proc=None,
    )
    queue: asyncio.Queue = asyncio.Queue()

    async def reply(_update, text, **_kwargs):
        replies.append(text)

    async def enqueue(*args, **kwargs):
        enqueued.append((args, kwargs))
        source = args[2]
        return "req-handoff" if source == runtime_retry.RETRY_HANDOFF_SOURCE else "req-retry"

    meta = None
    last_prompt = None
    if current_prompt is not None:
        meta = {
            "request_id": "req-stuck",
            "chat_id": 42,
            "prompt": current_prompt,
            "source": "text",
            "summary": "Security cleanup",
        }
        last_prompt = SimpleNamespace(
            request_id="req-stuck",
            chat_id=42,
            prompt=current_prompt,
            source="text",
            summary="Security cleanup",
        )

    runtime = SimpleNamespace(
        name="zhaojun",
        workspace_dir=tmp_path,
        config=SimpleNamespace(active_backend=engine, engine=engine, workspace_dir=tmp_path),
        backend_manager=SimpleNamespace(
            current_backend=backend,
            agent_mode="flex",
        ),
        queue=queue,
        current_request_meta=meta,
        last_prompt=last_prompt,
        last_response=None,
        is_generating=current_prompt is not None,
        context_assembler=assembler,
        handoff_builder=_Handoff(prompt=handoff_prompt),
        logger=_Logger(),
        _is_authorized_user=lambda _uid: True,
        _reply_text=reply,
        _clear_transfer_state=lambda: setattr(runtime, "transfer_cleared", True),
        _arm_session_primer=lambda text: setattr(runtime, "primer", text),
        _notify_right_brain_interrupted=lambda *args, **kwargs: setattr(
            runtime, "interrupted", (args, kwargs)
        ),
        enqueue_startup_bootstrap=AsyncMock(),
        enqueue_request=enqueue,
        send_long_message=AsyncMock(),
    )
    return runtime, replies, enqueued, store, backend


def test_retry_and_resend_are_registered_with_distinct_meanings():
    command_map = {binding.name: binding for binding in COMMAND_BINDINGS}
    menu_map = {binding.name: binding for binding in BOT_COMMAND_BINDINGS}

    assert command_map["retry"].method_name == "cmd_retry"
    assert command_map["resend"].method_name == "cmd_resend"
    assert "context" in menu_map["retry"].description.lower()
    assert "output" in menu_map["resend"].description.lower()


def test_retry_state_survives_restart_and_keeps_bridge_output(tmp_path):
    runtime = SimpleNamespace(workspace_dir=tmp_path, logger=_Logger(), last_response=None)
    item = SimpleNamespace(
        request_id="req-bridge",
        chat_id=99,
        prompt="Review the bridge delivery incident",
        source="bridge:hchat",
        summary="Bridge review",
        silent=False,
    )

    runtime_retry.remember_retryable_prompt(runtime, item)
    bridge_output = "  Bridge review completed\nwith details  "
    runtime_retry.remember_output(runtime, item, bridge_output)

    restarted = SimpleNamespace(
        workspace_dir=tmp_path,
        logger=_Logger(),
        current_request_meta=None,
        last_prompt=None,
        last_response=None,
        transcript_log_path=tmp_path / "missing-transcript.jsonl",
        core_transcript_log_path=tmp_path / "missing-core.jsonl",
    )
    prompt = runtime_retry.capture_retryable_prompt(restarted, fallback_chat_id=1)
    output = runtime_retry.capture_resend_output(restarted)

    assert prompt is not None
    assert prompt.prompt == "Review the bridge delivery incident"
    assert prompt.source == "bridge:hchat"
    assert output is not None
    assert output.text == bridge_output
    assert output.source == "bridge:hchat"

    state = json.loads(runtime_retry.retry_state_path(runtime).read_text(encoding="utf-8"))
    assert state["version"] == runtime_retry.RETRY_STATE_VERSION
    assert state["last_prompt"]["request_id"] == "req-bridge"
    assert state["last_output"]["request_id"] == "req-bridge"


def _retry_media_content(tmp_path):
    return canonical_request_content(
        [
            {"type": "text", "item_index": 1, "text": "Inspect it."},
            {
                "type": "media",
                "item_index": 2,
                "attachment_id": "attachment-retry",
                "modality": "image",
                "kind": "photo",
                "mime_type": "image/jpeg",
                "filename": "retry.jpg",
                "caption": "",
                "local_ref": str(tmp_path / "retry.jpg"),
                "size_bytes": 0,
                "sha256": "0" * 64,
                "transport": {"message_id": 99},
            },
        ]
    )


def test_retry_snapshot_persists_attachment_identity_without_inline_bytes(tmp_path):
    runtime = SimpleNamespace(workspace_dir=tmp_path, logger=_Logger())
    content = _retry_media_content(tmp_path)
    item = SimpleNamespace(
        request_id="req-media",
        chat_id=42,
        prompt="Inspect it.",
        source="photo",
        summary="Photo",
        silent=False,
        request_content=content,
    )

    runtime_retry.remember_retryable_prompt(runtime, item)
    restarted = SimpleNamespace(
        workspace_dir=tmp_path,
        logger=_Logger(),
        current_request_meta=None,
        last_prompt=None,
        transcript_log_path=tmp_path / "missing.jsonl",
    )
    snapshot = runtime_retry.capture_retryable_prompt(restarted)

    assert snapshot is not None
    assert snapshot.request_content == content
    assert snapshot.request_content is not content
    persisted = runtime_retry.retry_state_path(runtime).read_text(encoding="utf-8")
    assert "attachment-retry" in persisted
    assert "base64," not in persisted


def test_internal_retry_handoff_never_replaces_user_retry_or_resend_state(tmp_path):
    runtime = SimpleNamespace(workspace_dir=tmp_path, logger=_Logger(), last_response=None)
    user_item = SimpleNamespace(
        request_id="req-user",
        chat_id=42,
        prompt="Original request",
        source="text",
        summary="Original",
        silent=False,
    )
    handoff_item = SimpleNamespace(
        request_id="req-handoff",
        chat_id=42,
        prompt="Internal handoff",
        source=runtime_retry.RETRY_HANDOFF_SOURCE,
        summary="Internal",
        silent=False,
    )

    runtime_retry.remember_retryable_prompt(runtime, user_item)
    runtime_retry.remember_output(runtime, user_item, "Previous visible output")
    assert runtime_retry.remember_retryable_prompt(runtime, handoff_item) is None
    assert runtime_retry.remember_output(runtime, handoff_item, "Internal acknowledgement") is None

    assert runtime_retry.capture_retryable_prompt(runtime).prompt == "Original request"
    assert runtime_retry.capture_resend_output(runtime).text == "Previous visible output"


def test_interrupted_task_survives_restart_and_last_prompt_overwrite(tmp_path):
    runtime = SimpleNamespace(workspace_dir=tmp_path, logger=_Logger())
    original = {
        "request_id": "req-research",
        "chat_id": 42,
        "prompt": "Research common illnesses in middle-aged men and write a detailed report",
        "source": "text",
        "summary": "Health research",
    }

    saved = runtime_retry.remember_interrupted_task(
        runtime,
        original,
        backend="her-v2",
    )
    runtime_retry.remember_retryable_prompt(
        runtime,
        SimpleNamespace(
            request_id="req-continue",
            chat_id=42,
            prompt="You can continue now",
            source="text",
            summary="Continue",
            silent=False,
        ),
    )

    restarted = SimpleNamespace(workspace_dir=tmp_path, logger=_Logger())
    recovered = runtime_retry.capture_interrupted_task(restarted)

    assert saved is not None
    assert recovered is not None
    assert recovered.prompt == original["prompt"]
    assert recovered.request_id == "req-research"
    assert recovered.backend == "her-v2"
    state = json.loads(runtime_retry.retry_state_path(runtime).read_text(encoding="utf-8"))
    assert state["last_prompt"]["prompt"] == "You can continue now"
    assert state["unfinished_task"]["prompt"] == original["prompt"]


@pytest.mark.parametrize(
    "prompt",
    [
        "You can continue now",
        "please resume",
        "pick up where you left off",
        "继续",
        "可以继续了",
        "从刚才停下的地方继续",
    ],
)
def test_explicit_continuation_phrases_are_recognized(prompt):
    assert runtime_retry.is_explicit_continuation(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "Continue using PostgreSQL for the new service",
        "Write a new report",
        "/retry",
        "The work can continue after approval",
    ],
)
def test_unrelated_prompts_do_not_resume_interrupted_task(prompt):
    assert runtime_retry.is_explicit_continuation(prompt) is False


def test_repeated_stop_of_continuation_keeps_original_task_and_success_clears_it(tmp_path):
    runtime = SimpleNamespace(workspace_dir=tmp_path, logger=_Logger(), current_request_meta={})
    original = runtime_retry.remember_interrupted_task(
        runtime,
        {
            "request_id": "req-original",
            "chat_id": 42,
            "prompt": "Complete the original implementation",
            "source": "text",
            "summary": "Implementation",
        },
        backend="her-v2",
    )
    item = SimpleNamespace(
        request_id="req-continue",
        chat_id=42,
        prompt="You can continue now",
        source="text",
        summary="Continue",
        silent=False,
    )
    prepared = runtime_retry.prepare_interrupted_task_continuation(
        runtime,
        item,
        item.prompt,
        backend="her-v2",
    )
    runtime.current_request_meta = {
        "request_id": item.request_id,
        "prompt": item.prompt,
        "source": item.source,
        "summary": item.summary,
        "resumed_interrupted_task": item._resumed_interrupted_task,
    }

    saved_again = runtime_retry.remember_interrupted_task(
        runtime,
        runtime.current_request_meta,
        backend="her-v2",
    )

    assert original is not None
    assert "Complete the original implementation" in prepared
    assert saved_again is not None
    assert saved_again.request_id == "req-original"
    assert saved_again.prompt == "Complete the original implementation"
    assert runtime_retry.clear_completed_interrupted_task(runtime, item) is True
    assert runtime_retry.capture_interrupted_task(runtime) is None
    state = json.loads(runtime_retry.retry_state_path(runtime).read_text(encoding="utf-8"))
    assert "unfinished_task" not in state


def test_fresh_boundary_prevents_old_interrupted_task_from_rebinding(tmp_path):
    from orchestrator.fresh_context import start_boundary

    runtime = SimpleNamespace(
        workspace_dir=tmp_path,
        logger=_Logger(),
        current_request_meta={},
    )
    saved = runtime_retry.remember_interrupted_task(
        runtime,
        {
            "request_id": "req-before-fresh",
            "chat_id": 42,
            "prompt": "Old unfinished task",
            "source": "text",
            "summary": "Old task",
        },
        backend="her-v2",
    )
    assert saved is not None
    start_boundary(runtime, now_epoch=saved.interrupted_at + 0.001)
    item = SimpleNamespace(
        request_id="req-after-fresh",
        chat_id=42,
        prompt="continue",
        source="text",
        summary="Continue",
        silent=False,
    )

    prepared = runtime_retry.prepare_interrupted_task_continuation(
        runtime,
        item,
        item.prompt,
        backend="her-v2",
    )

    assert prepared == "continue"
    assert not hasattr(item, "_resumed_interrupted_task")
    assert runtime_retry.capture_interrupted_task(runtime) == saved


@pytest.mark.asyncio
async def test_resend_replays_exact_output_in_current_chat_without_model_work(tmp_path):
    runtime, replies, enqueued, _store, _backend = _runtime(
        tmp_path,
        current_prompt=None,
        handoff_prompt="",
    )
    runtime.last_response = {
        "chat_id": 7,
        "text": "Exact previous output",
        "source": "bridge:hchat",
        "request_id": "req-old",
    }

    await runtime_control.cmd_resend(runtime, _update(chat_id=42), SimpleNamespace(args=[]))

    assert enqueued == []
    runtime.send_long_message.assert_awaited_once_with(
        chat_id=42,
        text="Exact previous output",
        request_id="req-old",
        purpose="resend-output",
    )
    assert replies == []


@pytest.mark.asyncio
async def test_retry_stops_resets_handoffs_then_requeues_original_prompt(tmp_path):
    runtime, replies, enqueued, store, backend = _runtime(tmp_path)
    await runtime.queue.put(object())
    await runtime.queue.put(object())

    await runtime_control.cmd_retry(runtime, _update(), SimpleNamespace(args=[]))

    backend.shutdown.assert_awaited_once()
    backend.handle_new_session.assert_awaited_once()
    assert store.clear_count == 1
    assert runtime.queue.empty()
    assert runtime.transfer_cleared is True
    assert runtime._user_interrupt["reason"] == "user_retry"
    assert runtime._user_interrupt["request_id"] == "req-stuck"
    assert runtime.interrupted[1]["reason"] == "user_retry"
    assert runtime.handoff_builder.refreshed == 1
    assert runtime.handoff_builder.built == 1
    runtime.enqueue_startup_bootstrap.assert_awaited_once_with(42)

    assert [call[0][2] for call in enqueued] == [
        runtime_retry.RETRY_HANDOFF_SOURCE,
        "retry",
    ]
    handoff_args, handoff_kwargs = enqueued[0]
    assert handoff_args[1] == "HANDOFF CONTEXT"
    assert handoff_kwargs["deliver_to_telegram"] is False
    assert handoff_kwargs["skip_memory_injection"] is True
    assert handoff_kwargs["is_retry"] is True

    retry_args, retry_kwargs = enqueued[1]
    assert retry_args[0] == 42
    assert retry_args[1] == "Finish the security cleanup"
    assert retry_kwargs["is_retry"] is True
    assert replies and "Clean context: /new semantics." in replies[-1]
    assert "cleared 2 waiting request(s)" in replies[-1]
    assert "req-retry" in replies[-1]


@pytest.mark.asyncio
async def test_retry_requeues_the_same_canonical_attachment_content(tmp_path):
    runtime, _replies, enqueued, _store, _backend = _runtime(
        tmp_path, handoff_prompt=""
    )
    content = _retry_media_content(tmp_path)
    runtime.current_request_meta["request_content"] = content

    await runtime_control.cmd_retry(runtime, _update(), SimpleNamespace(args=[]))

    assert len(enqueued) == 1
    _args, kwargs = enqueued[0]
    assert kwargs["request_content"] == content


@pytest.mark.asyncio
async def test_retry_uses_fresh_semantics_for_api_backend(tmp_path):
    runtime, replies, enqueued, store, backend = _runtime(
        tmp_path,
        engine="openrouter-api",
        handoff_prompt="",
    )
    backend.capabilities.supports_sessions = False

    await runtime_control.cmd_retry(runtime, _update(), SimpleNamespace(args=[]))

    backend.shutdown.assert_awaited_once()
    backend.handle_new_session.assert_not_awaited()
    assert store.clear_count == 1
    assert runtime.context_assembler.turns_injection_enabled is True
    assert runtime.context_assembler.saved_memory_injection_enabled is False
    assert [call[0][2] for call in enqueued] == ["retry"]
    assert "Clean context: /fresh semantics." in replies[-1]


@pytest.mark.asyncio
async def test_retry_reruns_prompt_even_when_handoff_queueing_fails(tmp_path):
    runtime, replies, enqueued, _store, _backend = _runtime(tmp_path)

    async def enqueue(*args, **kwargs):
        if args[2] == runtime_retry.RETRY_HANDOFF_SOURCE:
            raise RuntimeError("handoff queue unavailable")
        enqueued.append((args, kwargs))
        return "req-retry"

    runtime.enqueue_request = enqueue

    await runtime_control.cmd_retry(runtime, _update(), SimpleNamespace(args=[]))

    assert [call[0][2] for call in enqueued] == ["retry"]
    assert "handoff restore could not be queued" in replies[-1]
    assert "req-retry" in replies[-1]
    assert any(
        "Could not queue handoff context" in message
        for message in runtime.logger.messages
    )


@pytest.mark.asyncio
async def test_retry_does_not_arm_interrupt_for_waiting_queue_only(tmp_path):
    runtime, _replies, _enqueued, _store, _backend = _runtime(
        tmp_path,
        current_prompt=None,
        handoff_prompt="",
    )
    runtime.last_prompt = SimpleNamespace(
        request_id="req-previous",
        chat_id=42,
        prompt="Retry the previous completed request",
        source="text",
        summary="Previous request",
    )
    await runtime.queue.put(object())

    await runtime_control.cmd_retry(runtime, _update(), SimpleNamespace(args=[]))

    assert getattr(runtime, "_user_interrupt", None) is None
    assert runtime.queue.qsize() == 0


@pytest.mark.asyncio
async def test_retry_without_prompt_changes_nothing(tmp_path):
    runtime, replies, enqueued, store, backend = _runtime(
        tmp_path,
        current_prompt=None,
        handoff_prompt="",
    )

    await runtime_control.cmd_retry(runtime, _update(), SimpleNamespace(args=[]))

    backend.shutdown.assert_not_awaited()
    backend.handle_new_session.assert_not_awaited()
    assert store.clear_count == 0
    assert enqueued == []
    assert "Nothing to retry" in replies[-1]
    assert "No session state was changed" in replies[-1]


@pytest.mark.asyncio
async def test_retry_response_argument_moves_user_to_resend_without_side_effects(tmp_path):
    runtime, replies, enqueued, store, backend = _runtime(tmp_path)

    await runtime_control.cmd_retry(
        runtime,
        _update(),
        SimpleNamespace(args=["response"]),
    )

    backend.shutdown.assert_not_awaited()
    backend.handle_new_session.assert_not_awaited()
    assert store.clear_count == 0
    assert enqueued == []
    assert "/resend" in replies[-1]


def test_retry_handoff_control_turns_are_excluded_from_future_handoffs(tmp_path):
    builder = HandoffBuilder(tmp_path)
    builder.append_transcript("user", "Normal user request", "text")
    builder.append_transcript("assistant", "Normal answer", "text")
    builder.append_transcript(
        "user",
        "INTERNAL RETRY HANDOFF PAYLOAD",
        runtime_retry.RETRY_HANDOFF_SOURCE,
    )
    builder.append_transcript(
        "assistant",
        "INTERNAL RETRY HANDOFF ACK",
        runtime_retry.RETRY_HANDOFF_SOURCE,
    )

    context, exchange_count, _word_count = builder.build_recent_context_block()

    assert exchange_count == 1
    assert "Normal user request" in context
    assert "Normal answer" in context
    assert "INTERNAL RETRY HANDOFF" not in context
