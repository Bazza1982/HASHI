from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from adapters.base import BackendResponse
from orchestrator import runtime_cross_session


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(message)

    def warning(self, message: str) -> None:
        self.messages.append(message)


def _runtime(tmp_path: Path, *, mode: str = "fixed") -> SimpleNamespace:
    backend = SimpleNamespace(
        _session_id="primary-session",
        _claw_model=lambda: "local/deepseek-v4-pro",
        capabilities=SimpleNamespace(supports_sessions=True),
    )
    return SimpleNamespace(
        name="momo",
        workspace_dir=tmp_path,
        config=SimpleNamespace(active_backend="her", workspace_dir=tmp_path),
        backend_manager=SimpleNamespace(agent_mode=mode, current_backend=backend),
        current_request_meta=None,
        _request_meta_by_id={},
        logger=_Logger(),
        get_current_model=lambda: "local/deepseek-v4-pro",
    )


def _item(**overrides) -> SimpleNamespace:
    payload = {
        "request_id": "req-scheduler",
        "chat_id": 123,
        "source": "scheduler",
        "summary": "Cron Task [evening]",
        "prompt": "Run the evening engagement task",
        "silent": False,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _response(
    text: str,
    *,
    session_id: str,
    completion: str = "completed",
    stop_reason: str = "end_turn",
    recommendation: str = "",
    session_scope: str = "isolated_per_run",
) -> BackendResponse:
    return BackendResponse(
        text=text,
        duration_ms=1,
        stop_reason=stop_reason,
        stream_metadata={
            "claw_completion_status": completion,
            "claw_stop_reason": stop_reason,
            "her_session_scope": session_scope,
            "her_session_id": session_id,
            "her_model": "local/deepseek-v4-pro",
            "recommended_action": recommendation,
        },
    )


def _begin(runtime: SimpleNamespace, item: SimpleNamespace) -> None:
    metadata = {
        "request_id": item.request_id,
        "chat_id": item.chat_id,
        "prompt": item.prompt,
        "source": item.source,
        "summary": item.summary,
        "session_scope": "persistent",
    }
    runtime.current_request_meta = metadata
    runtime._request_meta_by_id[item.request_id] = metadata


def test_scheduler_choice_receipt_is_persisted_and_injected(tmp_path):
    runtime = _runtime(tmp_path)
    item = _item()
    visible = "Reply with a letter:\nA — Comment one\nB — Comment two"

    receipt = runtime_cross_session.record_turn_result(
        runtime,
        item,
        assistant_text=visible,
        response=_response(visible, session_id="scheduler-session"),
        delivered=True,
        completion_path="foreground",
    )

    assert receipt is not None
    assert receipt["active"] is True
    assert receipt["pending_interaction"] == {
        "kind": "choice",
        "labels": ["A", "B"],
    }
    state_path = runtime_cross_session.receipt_state_path(runtime)
    assert state_path is not None
    assert json.loads(state_path.read_text(encoding="utf-8"))["version"] == 1
    assert runtime_cross_session.context_section(runtime, item) == []

    user_item = _item(request_id="req-user", source="text", prompt="What happened?")
    sections = runtime_cross_session.context_section(runtime, user_item)

    assert sections[0][0] == "CROSS-SESSION TURN RECEIPTS"
    assert "Comment one" in sections[0][1]
    assert "read-only context" in sections[0][1]


def test_ultra_structured_choice_receipt_does_not_depend_on_rendered_text(tmp_path):
    runtime = _runtime(tmp_path)
    item = _item(request_id="req-ultra-choice")
    response = _response(
        "哥哥，您想先做哪一项？",
        session_id="ultra-primary-session",
        completion="incomplete",
        stop_reason="requires_user_input",
    )
    response.stream_metadata["her_ultra"] = {
        "run_id": "run-choice",
        "status": "incomplete",
        "pending_interaction": {
            "interaction_id": "run-choice:interaction:1",
            "kind": "choice",
            "labels": ["A", "B", "C"],
        },
    }

    receipt = runtime_cross_session.record_turn_result(
        runtime,
        item,
        assistant_text=response.text,
        response=response,
        delivered=True,
        completion_path="foreground",
    )

    assert receipt is not None
    assert receipt["pending_interaction"] == {
        "kind": "choice",
        "labels": ["A", "B", "C"],
        "interaction_id": "run-choice:interaction:1",
    }
    reply = _item(request_id="req-ultra-reply", source="text", prompt="B")
    _begin(runtime, reply)
    bound_prompt = runtime_cross_session.prepare_reply_binding(
        runtime, reply, reply.prompt
    )
    assert "authoritative referent resolution" in bound_prompt
    assert reply._cross_session_receipt["reply_kind"] == "choice"
    assert runtime.current_request_meta["resume_session_id"] == (
        "ultra-primary-session"
    )


def test_active_receipt_is_not_trimmed_out_by_recent_completed_receipts(tmp_path):
    runtime = _runtime(tmp_path)
    for index in range(runtime_cross_session.MAX_CONTEXT_RECEIPTS):
        item = _item(request_id=f"req-completed-{index}")
        text = f"Completed scheduled task {index}."
        runtime_cross_session.record_turn_result(
            runtime,
            item,
            assistant_text=text,
            response=_response(text, session_id=f"completed-session-{index}"),
            delivered=True,
            completion_path="foreground",
        )
    active_item = _item(request_id="req-active")
    active_text = "Reply with a letter:\nA — Keep this active choice"
    runtime_cross_session.record_turn_result(
        runtime,
        active_item,
        assistant_text=active_text,
        response=_response(active_text, session_id="active-session"),
        delivered=True,
        completion_path="foreground",
    )

    user_item = _item(request_id="req-user", source="text", prompt="status")
    section = runtime_cross_session.context_section(runtime, user_item)[0][1]

    assert "Keep this active choice" in section
    assert section.count("## Receipt ") == runtime_cross_session.MAX_CONTEXT_RECEIPTS


def test_continue_binds_exact_isolated_session(tmp_path):
    runtime = _runtime(tmp_path)
    scheduler_item = _item()
    visible = "Task incomplete. CONTINUE from the saved session."
    runtime_cross_session.record_turn_result(
        runtime,
        scheduler_item,
        assistant_text=visible,
        response=_response(
            visible,
            session_id="scheduler-session",
            completion="incomplete",
            stop_reason="max_iterations",
            recommendation="continue",
        ),
        delivered=True,
        completion_path="foreground",
    )
    continuation = _item(
        request_id="req-continue",
        source="text",
        prompt="continue.",
        summary="Continue",
    )
    _begin(runtime, continuation)

    prompt = runtime_cross_session.prepare_reply_binding(
        runtime, continuation, continuation.prompt
    )

    metadata = runtime._request_meta_by_id[continuation.request_id]
    assert "cross-session reply binding" in prompt
    assert "Task incomplete" in prompt
    assert metadata["session_scope"] == "isolated_resume"
    assert metadata["resume_session_id"] == "scheduler-session"
    assert continuation._cross_session_receipt["reply_kind"] == "continuation"


def test_model_mismatch_keeps_bound_reply_out_of_primary_session(tmp_path):
    runtime = _runtime(tmp_path)
    scheduler_item = _item()
    visible = "Task incomplete. CONTINUE from the saved session."
    runtime_cross_session.record_turn_result(
        runtime,
        scheduler_item,
        assistant_text=visible,
        response=_response(
            visible,
            session_id="scheduler-session",
            completion="incomplete",
            stop_reason="max_iterations",
            recommendation="continue",
        ),
        delivered=True,
        completion_path="foreground",
    )
    runtime.backend_manager.current_backend._claw_model = lambda: "other/model"
    runtime.get_current_model = lambda: "other/model"
    continuation = _item(
        request_id="req-continue",
        source="text",
        prompt="continue",
        summary="Continue",
    )
    _begin(runtime, continuation)

    runtime_cross_session.prepare_reply_binding(
        runtime,
        continuation,
        continuation.prompt,
    )

    metadata = runtime.current_request_meta
    assert metadata["session_scope"] == "isolated_per_run"
    assert "resume_session_id" not in metadata


def test_choice_reply_uses_newest_delivered_choice_set(tmp_path):
    runtime = _runtime(tmp_path)
    old_item = _item(request_id="req-old", summary="Cron Task [old]")
    new_item = _item(request_id="req-new", summary="Cron Task [new]")
    old_text = "Reply with a letter:\nA — Old action\nB — Old alternative"
    new_text = (
        "Reply with a letter:\n"
        "A — New action\n"
        "B — New alternative\n"
        "C — New second action"
    )
    runtime_cross_session.record_turn_result(
        runtime,
        old_item,
        assistant_text=old_text,
        response=_response(old_text, session_id="old-session"),
        delivered=True,
        completion_path="foreground",
    )
    runtime_cross_session.record_turn_result(
        runtime,
        new_item,
        assistant_text=new_text,
        response=_response(new_text, session_id="new-session"),
        delivered=True,
        completion_path="foreground",
    )
    reply = _item(
        request_id="req-choice",
        source="text",
        prompt="comment A, C",
        summary="Comment A and C",
    )
    _begin(runtime, reply)

    prompt = runtime_cross_session.prepare_reply_binding(runtime, reply, reply.prompt)

    assert "New action" in prompt
    assert "Old action" not in prompt
    assert runtime.current_request_meta["resume_session_id"] == "new-session"
    assert reply._cross_session_receipt["reply_kind"] == "choice"
    receipts = runtime_cross_session.load_receipts(runtime)
    assert [receipt["active"] for receipt in receipts] == [False, True]


def test_newer_primary_choice_prevents_stale_scheduler_choice_binding(tmp_path):
    runtime = _runtime(tmp_path)
    scheduler_item = _item()
    scheduler_text = (
        "Reply with a letter:\nA — Scheduler action\nB — Scheduler alternative"
    )
    runtime_cross_session.record_turn_result(
        runtime,
        scheduler_item,
        assistant_text=scheduler_text,
        response=_response(scheduler_text, session_id="scheduler-session"),
        delivered=True,
        completion_path="foreground",
    )
    primary_item = _item(
        request_id="req-primary",
        source="text",
        prompt="Give me a different choice",
        summary="Primary choice",
    )
    primary_text = "Reply with a letter:\nA — Primary action\nB — Primary alternative"
    runtime_cross_session.record_turn_result(
        runtime,
        primary_item,
        assistant_text=primary_text,
        response=_response(
            primary_text,
            session_id="primary-session",
            session_scope="persistent",
        ),
        delivered=True,
        completion_path="foreground",
    )
    reply = _item(
        request_id="req-choice",
        source="text",
        prompt="A",
        summary="A",
    )
    _begin(runtime, reply)

    prompt = runtime_cross_session.prepare_reply_binding(runtime, reply, reply.prompt)

    assert prompt == "A"
    assert "cross_session_receipt" not in runtime.current_request_meta
    receipt = runtime_cross_session.load_receipts(runtime)[0]
    assert receipt["active"] is False
    assert receipt["resolved_by"] == "newer_primary_interaction:req-primary"


def test_newer_primary_question_with_trailing_emoji_closes_scheduler_prompt(tmp_path):
    runtime = _runtime(tmp_path)
    scheduler_item = _item()
    scheduler_text = "Task incomplete. CONTINUE from the saved session."
    runtime_cross_session.record_turn_result(
        runtime,
        scheduler_item,
        assistant_text=scheduler_text,
        response=_response(
            scheduler_text,
            session_id="scheduler-session",
            completion="incomplete",
            stop_reason="max_iterations",
            recommendation="continue",
        ),
        delivered=True,
        completion_path="foreground",
    )
    primary_item = _item(
        request_id="req-primary",
        source="text",
        prompt="Show me the current choices",
        summary="Primary question",
    )
    primary_text = "想先做哪件，哥哥？💌"

    runtime_cross_session.record_turn_result(
        runtime,
        primary_item,
        assistant_text=primary_text,
        response=_response(
            primary_text,
            session_id="primary-session",
            session_scope="persistent",
        ),
        delivered=True,
        completion_path="foreground",
    )

    receipt = runtime_cross_session.load_receipts(runtime)[0]
    assert receipt["active"] is False
    assert receipt["resolved_by"] == "newer_primary_interaction:req-primary"


def test_newer_scheduler_completion_closes_older_scheduler_choice(tmp_path):
    runtime = _runtime(tmp_path)
    old_item = _item(request_id="req-old")
    old_text = "Reply with a letter:\nA — Old action\nB — Old alternative"
    runtime_cross_session.record_turn_result(
        runtime,
        old_item,
        assistant_text=old_text,
        response=_response(old_text, session_id="old-session"),
        delivered=True,
        completion_path="foreground",
    )
    new_item = _item(request_id="req-new")
    runtime_cross_session.record_turn_result(
        runtime,
        new_item,
        assistant_text="The newer scheduled task completed.",
        response=_response(
            "The newer scheduled task completed.",
            session_id="new-session",
        ),
        delivered=True,
        completion_path="foreground",
    )

    receipts = runtime_cross_session.load_receipts(runtime)

    assert [receipt["active"] for receipt in receipts] == [False, False]
    assert receipts[0]["resolved_by"].startswith("superseded_by:")


def test_successful_bound_reply_resolves_receipt(tmp_path):
    runtime = _runtime(tmp_path)
    scheduler_item = _item()
    visible = "Reply with a letter:\nA — Do it"
    runtime_cross_session.record_turn_result(
        runtime,
        scheduler_item,
        assistant_text=visible,
        response=_response(visible, session_id="scheduler-session"),
        delivered=True,
        completion_path="foreground",
    )
    reply = _item(
        request_id="req-choice",
        source="text",
        prompt="A",
        summary="A",
    )
    _begin(runtime, reply)
    runtime_cross_session.prepare_reply_binding(runtime, reply, reply.prompt)

    runtime_cross_session.record_turn_result(
        runtime,
        reply,
        assistant_text="Action completed.",
        response=_response("Action completed.", session_id="scheduler-session"),
        delivered=True,
        completion_path="foreground",
    )

    receipt = runtime_cross_session.load_receipts(runtime)[0]
    assert receipt["active"] is False
    assert receipt["resolved_by"] == "req-choice"
    assert receipt["assistant_text"] == "Action completed."


def test_failed_bound_reply_keeps_original_checkpoint_retryable(tmp_path):
    runtime = _runtime(tmp_path)
    scheduler_item = _item()
    visible = "Task incomplete. CONTINUE from the saved session."
    runtime_cross_session.record_turn_result(
        runtime,
        scheduler_item,
        assistant_text=visible,
        response=_response(
            visible,
            session_id="scheduler-session",
            completion="incomplete",
            stop_reason="max_iterations",
            recommendation="continue",
        ),
        delivered=True,
        completion_path="foreground",
    )
    reply = _item(
        request_id="req-continue",
        source="text",
        prompt="continue",
        summary="Continue",
    )
    _begin(runtime, reply)
    runtime_cross_session.prepare_reply_binding(runtime, reply, reply.prompt)

    runtime_cross_session.record_turn_result(
        runtime,
        reply,
        error="temporary provider error",
        delivered=True,
        completion_path="foreground",
    )

    receipt = runtime_cross_session.load_receipts(runtime)[0]
    assert receipt["active"] is True
    assert receipt["session_id"] == "scheduler-session"
    assert receipt["assistant_text"] == visible
    assert receipt["last_attempt"]["status"] == "failed"
    assert receipt["last_attempt"]["delivered"] is True


def test_failed_scheduler_turn_is_context_only(tmp_path):
    runtime = _runtime(tmp_path)
    item = _item()

    receipt = runtime_cross_session.record_turn_result(
        runtime,
        item,
        error="browser bridge unavailable",
        delivered=True,
        completion_path="foreground",
    )

    assert receipt is not None
    assert receipt["status"] == "failed"
    assert receipt["active"] is False
    user_item = _item(request_id="req-user", source="text", prompt="status")
    assert (
        "browser bridge unavailable"
        in runtime_cross_session.context_section(runtime, user_item)[0][1]
    )
