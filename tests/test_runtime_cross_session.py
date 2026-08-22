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
        capabilities=SimpleNamespace(supports_sessions=True),
    )
    return SimpleNamespace(
        name="momo",
        workspace_dir=tmp_path,
        config=SimpleNamespace(active_backend="her-v2", workspace_dir=tmp_path),
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
    completion: str = "completed",
    stop_reason: str = "end_turn",
    recommendation: str = "",
    pending_interaction: dict[str, str] | None = None,
) -> BackendResponse:
    response = BackendResponse(
        text=text,
        duration_ms=1,
        stop_reason=stop_reason,
        stream_metadata={
            "completion_status": completion,
            "completion_stop_reason": stop_reason,
            "recommended_action": recommendation,
        },
    )
    if pending_interaction is not None:
        response.stream_metadata["pending_interaction"] = dict(pending_interaction)
    return response


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
        response=_response(visible),
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
    assert json.loads(state_path.read_text(encoding="utf-8"))["version"] == 2
    assert runtime_cross_session.context_section(runtime, item) == []

    user_item = _item(request_id="req-user", source="text", prompt="What happened?")
    sections = runtime_cross_session.context_section(runtime, user_item)

    assert sections[0][0] == "CROSS-SESSION TURN RECEIPTS"
    assert "Comment one" in sections[0][1]
    assert "read-only context" in sections[0][1]


def test_primary_pending_turn_persists_checkpoint_and_binds_a_full_flex_reply(tmp_path):
    runtime = _runtime(tmp_path, mode="flex")
    item = _item(
        request_id="req-primary-pending",
        source="text",
        prompt="Inspect the workbook and ask before writing",
        summary="Workbook clarification",
    )
    response = _response(
        "I inspected the workbook. Which mapping should I use?",
        completion="incomplete",
        stop_reason="requires_user_input",
    )
    response.stream_metadata.update(
        {
            "pending_interaction": {
                "interaction_id": "ask-1",
                "kind": "choice",
                "question": "Which mapping should I use?",
                "options": ["Use the existing mapping", "Create a new mapping"],
                "labels": ["A", "B"],
            },
            "task_checkpoint": {
                "active_goal": "Populate the workbook after clarification",
                "completed": ["Inspected source files"],
                "remaining_work": ["Write the selected mapping"],
                "next_action": "Await the user's answer",
            },
            "planning_status": "failed",
            "planning_error": (
                "task frame planned_tools contains non-canonical tool prose "
                "`write_file 或 hashi_file_write`"
            ),
            "execution_ledger": {
                "version": 1,
                "total_entries": 1,
                "entries": [
                    {
                        "tool_use_id": "read-1",
                        "tool": "read_file",
                        "status": "succeeded",
                        "verification": "verified",
                    }
                ],
            },
        }
    )

    receipt = runtime_cross_session.record_turn_result(
        runtime,
        item,
        assistant_text=response.text,
        response=response,
        delivered=True,
        completion_path="foreground",
    )

    assert receipt is not None
    assert receipt["task_status"] == "awaiting_user"
    assert receipt["task_checkpoint"]["completed"] == ["Inspected source files"]
    assert receipt["planning_status"] == "failed"
    assert "write_file 或 hashi_file_write" in receipt["planning_error"]
    assert receipt["execution_ledger"]["entries"][0]["tool_use_id"] == "read-1"
    assert receipt["delivery_receipt"]["confirmed"] is True

    reply = _item(
        request_id="req-primary-answer",
        source="text",
        prompt="Use the existing mapping, but preserve all current workbook formatting.",
        summary="Mapping answer",
    )
    _begin(runtime, reply)
    bound_prompt = runtime_cross_session.prepare_reply_binding(runtime, reply, reply.prompt)

    assert "Populate the workbook after clarification" in bound_prompt
    assert "Planning status: failed" in bound_prompt
    assert "write_file 或 hashi_file_write" in bound_prompt
    assert "read-1" in bound_prompt
    assert "preserve all current workbook formatting" in bound_prompt
    assert reply._cross_session_receipt["reply_kind"] == "answer"


def test_active_receipt_is_not_trimmed_out_by_recent_completed_receipts(tmp_path):
    runtime = _runtime(tmp_path)
    for index in range(runtime_cross_session.MAX_CONTEXT_RECEIPTS):
        item = _item(request_id=f"req-completed-{index}")
        text = f"Completed scheduled task {index}."
        runtime_cross_session.record_turn_result(
            runtime,
            item,
            assistant_text=text,
            response=_response(text),
            delivered=True,
            completion_path="foreground",
        )
    active_item = _item(request_id="req-active")
    active_text = "Reply with a letter:\nA — Keep this active choice"
    runtime_cross_session.record_turn_result(
        runtime,
        active_item,
        assistant_text=active_text,
        response=_response(active_text),
        delivered=True,
        completion_path="foreground",
    )

    user_item = _item(request_id="req-user", source="text", prompt="status")
    section = runtime_cross_session.context_section(runtime, user_item)[0][1]

    assert "Keep this active choice" in section
    assert section.count("## Receipt ") == runtime_cross_session.MAX_CONTEXT_RECEIPTS


def test_incomplete_status_and_legacy_recommendation_do_not_invent_a_pending_reply(
    tmp_path,
):
    runtime = _runtime(tmp_path)
    item = _item()
    visible = "The selected model reported unfinished work and suggested continuing."

    receipt = runtime_cross_session.record_turn_result(
        runtime,
        item,
        assistant_text=visible,
        response=_response(
            visible,
            completion="incomplete",
            stop_reason="max_iterations",
            recommendation="continue",
        ),
        delivered=True,
        completion_path="foreground",
    )

    assert receipt is not None
    assert receipt["status"] == "incomplete"
    assert receipt["pending_interaction"] is None
    assert receipt["active"] is False


def test_continue_binds_cross_session_receipt_without_backend_resume(tmp_path):
    runtime = _runtime(tmp_path)
    scheduler_item = _item()
    visible = "Task incomplete. CONTINUE from the saved session."
    runtime_cross_session.record_turn_result(
        runtime,
        scheduler_item,
        assistant_text=visible,
        response=_response(
            visible,
            completion="incomplete",
            stop_reason="max_iterations",
            recommendation="continue",
            pending_interaction={"kind": "continuation", "token": "CONTINUE"},
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
    assert "resume_session_id" not in metadata
    assert continuation._cross_session_receipt["reply_kind"] == "continuation"


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
        response=_response(old_text),
        delivered=True,
        completion_path="foreground",
    )
    runtime_cross_session.record_turn_result(
        runtime,
        new_item,
        assistant_text=new_text,
        response=_response(new_text),
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
    assert reply._cross_session_receipt["reply_kind"] == "choice"
    receipts = runtime_cross_session.load_receipts(runtime)
    assert [receipt["active"] for receipt in receipts] == [False, True]


def test_reply_target_is_frozen_at_enqueue_before_later_scheduler_delivery(tmp_path):
    runtime = _runtime(tmp_path)
    reply = _item(
        request_id="req-reply",
        source="text",
        prompt="continue",
        summary="Continue",
    )

    assert runtime_cross_session.capture_reply_target(runtime, reply) is None

    scheduler_item = _item(request_id="req-later-scheduler")
    visible = "Task incomplete. CONTINUE from the saved session."
    runtime_cross_session.record_turn_result(
        runtime,
        scheduler_item,
        assistant_text=visible,
        response=_response(
            visible,
            completion="incomplete",
            stop_reason="max_iterations",
            recommendation="continue",
            pending_interaction={"kind": "continuation", "token": "CONTINUE"},
        ),
        delivered=True,
        completion_path="background",
    )
    _begin(runtime, reply)

    prompt = runtime_cross_session.prepare_reply_binding(runtime, reply, reply.prompt)

    assert prompt == "continue"
    assert "cross_session_receipt" not in runtime.current_request_meta


def test_reply_target_captured_at_enqueue_survives_later_scheduler_delivery(tmp_path):
    runtime = _runtime(tmp_path)
    first_item = _item(request_id="req-first-scheduler")
    first_text = "Reply with a letter:\nA — First visible action"
    runtime_cross_session.record_turn_result(
        runtime,
        first_item,
        assistant_text=first_text,
        response=_response(first_text),
        delivered=True,
        completion_path="background",
    )
    reply = _item(
        request_id="req-reply",
        source="text",
        prompt="A",
        summary="A",
    )

    binding = runtime_cross_session.capture_reply_target(runtime, reply)

    second_item = _item(request_id="req-second-scheduler")
    second_text = "Reply with a letter:\nA — Later action"
    runtime_cross_session.record_turn_result(
        runtime,
        second_item,
        assistant_text=second_text,
        response=_response(second_text),
        delivered=True,
        completion_path="background",
    )
    _begin(runtime, reply)
    prompt = runtime_cross_session.prepare_reply_binding(runtime, reply, reply.prompt)

    assert binding is not None
    assert binding["request_id"] == "req-first-scheduler"
    assert "First visible action" in prompt
    assert "Later action" not in prompt


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
        response=_response(scheduler_text),
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

    assert "Original task:\nGive me a different choice" in prompt
    assert "Current user reply:\nA" in prompt
    assert runtime.current_request_meta["cross_session_receipt"]["request_id"] == "req-primary"
    receipts = runtime_cross_session.load_receipts(runtime)
    assert receipts[0]["active"] is False
    assert receipts[1]["active"] is True
    assert receipts[1]["task_status"] == "awaiting_user"


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
            completion="incomplete",
            stop_reason="max_iterations",
            recommendation="continue",
            pending_interaction={"kind": "continuation", "token": "CONTINUE"},
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
        ),
        delivered=True,
        completion_path="foreground",
    )

    receipts = runtime_cross_session.load_receipts(runtime)
    assert receipts[0]["active"] is False
    assert receipts[0]["resolved_by"].startswith("superseded_by:")
    assert receipts[1]["active"] is True
    assert receipts[1]["pending_interaction"]["kind"] == "question"


def test_newer_scheduler_completion_closes_older_scheduler_choice(tmp_path):
    runtime = _runtime(tmp_path)
    old_item = _item(request_id="req-old")
    old_text = "Reply with a letter:\nA — Old action\nB — Old alternative"
    runtime_cross_session.record_turn_result(
        runtime,
        old_item,
        assistant_text=old_text,
        response=_response(old_text),
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
        response=_response(visible),
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
        response=_response("Action completed."),
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
            completion="incomplete",
            stop_reason="max_iterations",
            recommendation="continue",
            pending_interaction={"kind": "continuation", "token": "CONTINUE"},
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
