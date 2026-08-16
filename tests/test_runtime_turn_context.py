from __future__ import annotations

import json
from types import SimpleNamespace

from orchestrator import runtime_delivery_order, runtime_turn_context
from orchestrator.runtime_common import QueuedRequest


def _runtime(tmp_path):
    backend = SimpleNamespace(
        effort="high",
        model="deepseek/deepseek-v4-pro",
        _claw_model=lambda: "deepseek/deepseek-v4-pro",
        _permission_mode=lambda: "workspace-write",
    )
    return SimpleNamespace(
        workspace_dir=tmp_path,
        config=SimpleNamespace(active_backend="her"),
        backend_manager=SimpleNamespace(current_backend=backend),
        get_current_model=lambda: backend._claw_model(),
    )


def _item(request_id: str, prompt: str) -> QueuedRequest:
    return QueuedRequest(
        request_id=request_id,
        chat_id=42,
        prompt=prompt,
        source="telegram",
        summary=prompt,
        created_at="2026-08-16T21:00:00",
    )


def _section_payload(runtime, item) -> dict:
    sections = runtime_turn_context.context_section(runtime, item)
    assert [title for title, _body in sections] == ["HASHI TURN CONTEXT"]
    return json.loads(sections[0][1])


def test_turn_context_injects_latest_delivered_dialogue_and_model_transition(tmp_path):
    runtime = _runtime(tmp_path)
    first = _item(
        "req-0001",
        "请选择：A. 完整重跑 Wiki pipeline；B. 只做 dry-run",
    )
    runtime_turn_context.capture_at_enqueue(runtime, first)
    runtime_turn_context.record_delivered_turn(
        runtime,
        first,
        "A. 完整重跑 Wiki pipeline\nB. 只做 dry-run",
    )

    runtime.backend_manager.current_backend.model = "deepseek/deepseek-v4-flash"
    runtime.backend_manager.current_backend._claw_model = lambda: (
        "deepseek/deepseek-v4-flash"
    )
    runtime.backend_manager.current_backend.effort = "xhigh"
    second = _item("req-0002", "A")
    runtime_turn_context.capture_at_enqueue(runtime, second)

    payload = _section_payload(runtime, second)
    assert payload["format"] == "hashi-turn-context-v1"
    assert payload["current"]["model"] == "deepseek/deepseek-v4-flash"
    assert payload["current"]["effort"] == "xhigh"
    assert payload["reply_target"] == {
        "kind": "latest_delivered_final",
        "request_id": "req-0001",
    }
    assert payload["previous_turn"]["user_text"].startswith("请选择")
    assert payload["previous_turn"]["assistant_text"].startswith("A. 完整重跑")
    assert payload["transition"] == {
        "effort_changed": True,
        "model_changed": True,
        "previous_effort": "high",
        "previous_model": "deepseek/deepseek-v4-pro",
    }


def test_turn_context_snapshot_is_frozen_before_later_delivery(tmp_path):
    runtime = _runtime(tmp_path)
    first = _item("req-0001", "first")
    runtime_turn_context.capture_at_enqueue(runtime, first)
    runtime_turn_context.record_delivered_turn(runtime, first, "first final")

    reply = _item("req-0002", "A")
    runtime_turn_context.capture_at_enqueue(runtime, reply)

    later = _item("req-cron", "later scheduler task")
    later.source = "scheduler:daily"
    runtime_turn_context.capture_at_enqueue(runtime, later)
    runtime_turn_context.record_delivered_turn(runtime, later, "later cron final")

    payload = _section_payload(runtime, reply)
    assert payload["reply_target"]["request_id"] == "req-0001"
    assert payload["previous_turn"]["assistant_text"] == "first final"
    assert "later cron final" not in json.dumps(payload, ensure_ascii=False)


def test_cold_runtime_marks_previous_turn_unavailable_for_her_session_fallback(
    tmp_path,
):
    runtime = _runtime(tmp_path)
    first_after_reboot = _item("req-0001", "A")

    payload = _section_payload(runtime, first_after_reboot)

    assert payload["previous_turn_status"] == "unavailable"
    assert payload["previous_turn"] is None


def test_pending_earlier_direct_turn_freezes_absence_of_a_visible_final(tmp_path):
    runtime = _runtime(tmp_path)
    earlier = _item("req-0001", "first request still running")
    reply = _item("req-0002", "A")
    runtime_delivery_order.register_turn(runtime, earlier)
    runtime_delivery_order.register_turn(runtime, reply)

    payload = _section_payload(runtime, reply)

    assert payload["previous_turn_status"] == "captured_no_prior_final"
    assert payload["previous_turn"] is None
