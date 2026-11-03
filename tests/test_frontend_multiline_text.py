"""Reachable natural text retains paragraphs while routing identities stay strict."""
from __future__ import annotations

import html

import pytest

from orchestrator.frontend_contracts import normalize_content_component, normalize_frontend_operation
from orchestrator.frontend_compatibility import normalize_compatibility_command
from orchestrator.frontend_projection import render_event_to_html, render_event_to_plain_text, poll_frontend_feed
from tests.test_frontend_command_admission import _Runtime, _update, _execute
from tests.test_frontend_standard_interface import _setup_runtime
from tests.test_session_api import _server, _Request

BODY = "first paragraph\nsecond\tparagraph <unsafe>"


@pytest.mark.parametrize("command,args", [("steer", [BODY]), ("focus", [BODY]), ("ticket", [BODY]),
    ("hchat", ["akane@HASHI2", BODY]), ("wa_send", ["+61123456789", BODY]), ("loop", [BODY])])
def test_command_owner_natural_text_is_one_bounded_argument(command, args):
    result = normalize_frontend_operation({"kind": "command", "name": command, "arguments": args})
    assert result["arguments"] == args and result["name"] == command


def test_quoted_multiline_command_survives_the_real_compatibility_parser():
    result = normalize_compatibility_command('/steer "first\nsecond"', source_channel="telegram.message")
    assert result["operation"]["arguments"] == ["first\nsecond"]


@pytest.mark.asyncio
async def test_telegram_multiline_command_admission_is_durable_and_replays_once(tmp_path):
    runtime, update = _Runtime(tmp_path), _update(9100)
    calls = []
    async def handler():
        calls.append("executed")
        return {"ok": True}
    kwargs = {"command_name": "steer", "arguments": [BODY],
        "request_payload": {"update_id": "9100", "command": "steer", "arguments": [BODY]}}
    first = await _execute(runtime, update, handler, **kwargs)
    replay = await _execute(runtime, update, handler, **kwargs)
    assert first["ok"] and replay["ok"] and replay["replayed"]
    assert calls == ["executed"]


@pytest.mark.asyncio
async def test_multiline_cancel_reason_reaches_only_the_selected_run(tmp_path):
    server, runtime = _server(tmp_path)
    session = server.session_store.resolve_primary_session(owner_id=server._v1_owner_id(_Request()), agent_id="lily")
    accepted = server.session_store.accept_run(session_id=session["session_id"], owner_id=session["owner_id"],
        agent_id="lily", request_id="req-multiline-cancel", text="work", source="workbench", idempotency_key="multiline-cancel")
    server.session_store.mark_request_running(accepted.request_id, worker_id="worker")
    calls = []
    async def cancel(**scope):
        calls.append(scope)
        return {"ok": True, "status": "cancellation_requested", "terminal": False, "request_id": accepted.request_id}
    runtime.cancel_session_run = cancel
    response = await server.handle_v1_session_run_cancel(_Request({"reason": BODY},
        match_info={"session_id": session["session_id"], "run_id": accepted.run_id}))
    assert response.status == 202 and calls[0]["reason"] == BODY
    assert calls[0]["run_id"] == accepted.run_id
    assert server.session_store.get_run(accepted.run_id)["state"] == "running"


@pytest.mark.parametrize("block", [
    {"type": "hint", "text": BODY},
    {"type": "key_value", "items": [{"key": "reason", "value": BODY}]},
    {"type": "table", "headers": ["Result"], "rows": [[BODY]]},
])
def test_multiline_display_components_reach_both_renderers(block):
    normalized = normalize_content_component(block)
    event = {"content_blocks": [normalized]}
    assert BODY in render_event_to_plain_text(event)
    assert html.escape(BODY) in render_event_to_html(event)
    assert "<unsafe>" not in render_event_to_html(event)


def test_canonical_durable_presentation_keeps_multiline_values_after_feed_reload(tmp_path):
    runtime, store, owner, session = _setup_runtime(tmp_path)
    store.append_presentation_message(session_id=session["session_id"], owner_id=owner, agent_id="test-agent",
        role="assistant", text="Result", source="status", idempotency_key="multiline-presentation",
        presentation_channel="status", message_context={"frontend_presentation": {
            "semantic_kind": "status", "presentation_channel": "status", "interface_kind": "state",
            "content_blocks": [{"type": "key_value", "items": [{"key": "reason", "value": BODY}]}]}})
    for _ in range(2):
        event = poll_frontend_feed(store, session["session_id"], owner_id=owner)["durable_events"][-1]
        assert BODY in render_event_to_plain_text(event)
        assert html.escape(BODY) in render_event_to_html(event)


@pytest.mark.parametrize("control", ["\x00", "\x07", "\x1b", "\x7f"])
def test_free_text_rejects_non_text_controls(control):
    with pytest.raises(ValueError):
        normalize_frontend_operation({"kind": "control", "action": "steer", "text": "a" + control + "b"})
    with pytest.raises(ValueError):
        normalize_content_component({"type": "hint", "text": "a" + control + "b"})


@pytest.mark.parametrize("command,args", [
    ("status", ["a\nb"]), ("hchat", ["aka\nne", BODY]), ("wa_send", ["+61\n123", BODY]),
    ("loop", ["stop", "id\nother"]), ("loop", ["stop\nid"]),
    ("loop", ["stop-prefix\nid"]), ("ste\ner", [BODY]),
])
def test_non_text_arguments_and_routing_names_remain_single_line(command, args):
    with pytest.raises(ValueError):
        normalize_frontend_operation({"kind": "command", "name": command, "arguments": args})


def test_new_multiline_fields_keep_their_original_size_and_label_limits():
    for operation in [
        {"kind": "control", "action": "steer", "text": "x" * 65537},
        {"kind": "command", "name": "steer", "arguments": ["x" * 4097]},
        {"kind": "command", "name": "steer", "arguments": ["x"] * 65},
    ]:
        with pytest.raises(ValueError):
            normalize_frontend_operation(operation)
    with pytest.raises(ValueError):
        normalize_content_component({"type": "hint", "text": "x" * 1025})
    with pytest.raises(ValueError):
        normalize_content_component({"type": "key_value", "items": [{"key": "a\nb", "value": BODY}]})
    with pytest.raises(ValueError):
        normalize_content_component({"type": "table", "headers": ["a\nb"], "rows": [[BODY]]})
