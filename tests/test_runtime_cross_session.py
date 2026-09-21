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


def test_scheduler_exchange_is_persisted_as_read_only_history(tmp_path):
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
    assert receipt["active"] is False
    assert receipt["pending_interaction"] is None
    state_path = runtime_cross_session.receipt_state_path(runtime)
    assert state_path is not None
    assert json.loads(state_path.read_text(encoding="utf-8"))["version"] == 2
    assert runtime_cross_session.context_section(runtime, item) == []

    user_item = _item(request_id="req-user", source="text", prompt="What happened?")
    sections = runtime_cross_session.context_section(runtime, user_item)

    assert sections[0][0] == "CROSS-SESSION TURN RECEIPTS"
    assert "Comment one" in sections[0][1]
    assert "read-only context" in sections[0][1]
    assert "USER:\nRun the evening engagement task" in sections[0][1]
    assert "pending_interaction" not in sections[0][1]

    timeline = runtime_cross_session.timeline_entries(runtime, user_item)
    assert len(timeline) == 1
    assert timeline[0]["receipt_id"] == receipt["receipt_id"]
    assert timeline[0]["completed_at"] == receipt["updated_at"]
    assert timeline[0]["user_text"] == item.prompt
    assert timeline[0]["assistant_text"] == visible


def test_primary_pending_turn_stays_in_canonical_session_not_receipt_state(tmp_path):
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

    assert receipt is None
    assert runtime_cross_session.load_receipts(runtime) == []

    reply = _item(
        request_id="req-primary-answer",
        source="text",
        prompt="Use the existing mapping, but preserve all current workbook formatting.",
        summary="Mapping answer",
    )
    _begin(runtime, reply)
    effective_prompt = runtime_cross_session.prepare_reply_binding(
        runtime, reply, reply.prompt
    )

    assert effective_prompt == reply.prompt
    assert "cross_session_receipt" not in runtime.current_request_meta
    assert not hasattr(reply, "_cross_session_receipt")


def test_context_uses_the_most_recent_receipts_in_chronological_order(tmp_path):
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
    assert "Completed scheduled task 0." not in section
    assert "Completed scheduled task 1." in section
    assert section.count("## Exchange ") == runtime_cross_session.MAX_CONTEXT_RECEIPTS


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


def test_continue_remains_verbatim_with_scheduler_exchange_in_history(tmp_path):
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
    assert prompt == "continue."
    assert "cross_session_receipt" not in metadata
    assert not hasattr(continuation, "_cross_session_receipt")
    assert "Task incomplete" in runtime_cross_session.context_section(
        runtime, continuation
    )[0][1]


def test_choice_reply_is_verbatim_and_receipts_remain_chronological(tmp_path):
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

    assert prompt == "comment A, C"
    assert not hasattr(reply, "_cross_session_receipt")
    timeline = runtime_cross_session.timeline_entries(runtime, reply)
    assert [entry["assistant_text"] for entry in timeline] == [old_text, new_text]


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


def test_reply_target_is_never_captured_and_later_history_stays_ordered(tmp_path):
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

    assert binding is None
    assert prompt == "A"
    timeline = runtime_cross_session.timeline_entries(runtime, reply)
    assert [entry["assistant_text"] for entry in timeline] == [
        first_text,
        second_text,
    ]


def test_primary_choice_is_not_copied_into_cross_session_receipts(tmp_path):
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

    assert prompt == "A"
    assert "cross_session_receipt" not in runtime.current_request_meta
    receipts = runtime_cross_session.load_receipts(runtime)
    assert len(receipts) == 1
    assert receipts[0]["request_id"] == scheduler_item.request_id


def test_primary_question_does_not_mutate_scheduler_task_status(tmp_path):
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
    assert len(receipts) == 1
    assert receipts[0]["active"] is True
    assert receipts[0]["pending_interaction"]["kind"] == "continuation"
    assert "resolved_by" not in receipts[0]


def test_scheduler_receipts_do_not_semantically_supersede_each_other(tmp_path):
    runtime = _runtime(tmp_path)
    old_item = _item(request_id="req-old")
    old_text = "Reply with a letter:\nA — Old action\nB — Old alternative"
    runtime_cross_session.record_turn_result(
        runtime,
        old_item,
        assistant_text=old_text,
        response=_response(
            old_text,
            pending_interaction={"kind": "choice", "labels": ["A", "B"]},
        ),
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

    assert [receipt["active"] for receipt in receipts] == [True, False]
    assert "resolved_by" not in receipts[0]


def test_natural_reply_does_not_resolve_or_overwrite_receipt(tmp_path):
    runtime = _runtime(tmp_path)
    scheduler_item = _item()
    visible = "Reply with a letter:\nA — Do it"
    runtime_cross_session.record_turn_result(
        runtime,
        scheduler_item,
        assistant_text=visible,
        response=_response(
            visible,
            pending_interaction={"kind": "choice", "labels": ["A"]},
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
    assert receipt["active"] is True
    assert "resolved_by" not in receipt
    assert receipt["assistant_text"] == visible
    timeline = runtime_cross_session.timeline_entries(
        runtime,
        _item(request_id="req-after", source="text", prompt="What happened?"),
    )
    assert timeline[0]["user_text"] == scheduler_item.prompt
    assert timeline[0]["assistant_text"] == visible


def test_failed_natural_reply_does_not_mutate_receipt(tmp_path):
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
    assert "last_attempt" not in receipt


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


def test_receipts_never_cross_hashi_session_or_context_generation(tmp_path):
    runtime = _runtime(tmp_path)
    scheduled = _item(
        session_id="session-a",
        context_generation=1,
    )
    visible = "Reply with a letter:\nA — Continue Session A"
    runtime_cross_session.record_turn_result(
        runtime,
        scheduled,
        assistant_text=visible,
        response=_response(visible),
        delivered=True,
        completion_path="foreground",
    )

    other_session = _item(
        request_id="req-session-b",
        source="text",
        prompt="A",
        session_id="session-b",
        context_generation=1,
    )
    assert runtime_cross_session.context_section(runtime, other_session) == []
    assert runtime_cross_session.timeline_entries(runtime, other_session) == []
    assert runtime_cross_session.capture_reply_target(runtime, other_session) is None

    fresh_generation = _item(
        request_id="req-session-a-fresh",
        source="text",
        prompt="A",
        session_id="session-a",
        context_generation=2,
    )
    assert runtime_cross_session.context_section(runtime, fresh_generation) == []
    assert runtime_cross_session.capture_reply_target(runtime, fresh_generation) is None

    same_generation = _item(
        request_id="req-session-a",
        source="text",
        prompt="A",
        session_id="session-a",
        context_generation=1,
    )
    assert "Continue Session A" in runtime_cross_session.context_section(
        runtime, same_generation
    )[0][1]
    assert runtime_cross_session.capture_reply_target(runtime, same_generation) is None


def test_natural_choice_reply_remains_verbatim_and_unbound(tmp_path):
    runtime = _runtime(tmp_path)
    scheduled = _item(session_id="session-a", context_generation=1)
    visible = "Choose 1, 2, or 3."
    runtime_cross_session.record_turn_result(
        runtime,
        scheduled,
        assistant_text=visible,
        response=_response(
            visible,
            pending_interaction={"kind": "choice", "labels": ["1", "2", "3"]},
        ),
        delivered=True,
        completion_path="foreground",
    )
    reply = _item(
        request_id="req-natural-choice",
        source="text",
        prompt="3",
        summary="3",
        session_id="session-a",
        context_generation=1,
    )
    _begin(runtime, reply)

    effective = runtime_cross_session.prepare_reply_binding(runtime, reply, reply.prompt)

    assert effective == "3"
    assert "cross_session_receipt" not in runtime.current_request_meta
    assert not hasattr(reply, "_cross_session_receipt")
    assert runtime_cross_session.timeline_entries(runtime, reply)[-1][
        "assistant_text"
    ] == visible


def test_primary_prose_and_windows_path_do_not_create_choice_receipt(tmp_path):
    runtime = _runtime(tmp_path)
    item = _item(
        request_id="req-primary-report",
        source="text",
        prompt="Report the result plainly",
        summary="Plain report",
        session_id="session-a",
        context_generation=1,
    )
    visible = "Reply summary:\nC:\\Users\\thene\\projects\\HASHI4"

    receipt = runtime_cross_session.record_turn_result(
        runtime,
        item,
        assistant_text=visible,
        response=_response(visible),
        delivered=True,
        completion_path="foreground",
    )

    assert receipt is None
    assert runtime_cross_session.load_receipts(runtime) == []
