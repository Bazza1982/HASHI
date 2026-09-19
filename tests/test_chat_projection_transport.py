"""Real command entry, SessionStore and JSONL boundaries for chat reads."""
from __future__ import annotations

import base64
import json
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from orchestrator.admin_local_testing import execute_local_command, try_execute_slash_command_text
from orchestrator.session_store import SessionStore


PREFIX = "__hashi_chat_projection_v1__:"


def _wire(**fields):
    value = {"version": 1, "op": "recent", **fields}
    encoded = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return PREFIX + encoded


def _runtime(tmp_path):
    return NS(
        name="agent",
        workspace_dir=tmp_path,
        global_config=NS(authorized_id=7, instance_id="fixture", deployment_profile="personal"),
        session_store=SessionStore(tmp_path / "sessions.sqlite", instance_id="fixture"),
        _is_authorized_user=lambda actor: actor == 7,
        enqueue_request=AsyncMock(side_effect=AssertionError("projection enqueued a model request")),
        _send_text=AsyncMock(side_effect=AssertionError("projection sent a chat message")),
    )


def _session(runtime):
    return runtime.session_store.resolve_session(
        owner_id="user:7", agent_id="agent", surface="workbench", channel_key="default"
    )


def _path(runtime, session):
    return runtime.session_store.session_workspace(
        session["session_id"], session["context_generation"]
    ) / "transcript.jsonl"


def _line(text, **fields):
    return (json.dumps({"role": "assistant", "text": text, **fields}, ensure_ascii=False) + "\n").encode()


def _database_snapshot(tmp_path):
    with sqlite3.connect(tmp_path / "sessions.sqlite") as db:
        return list(db.iterdump())


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", [try_execute_slash_command_text, execute_local_command])
async def test_command_reads_real_session_history_without_audit_chat_or_model_side_effects(tmp_path, caplog, entry):
    runtime = _runtime(tmp_path)
    session = _session(runtime)
    accepted = runtime.session_store.accept_run(
        session_id=session["session_id"], owner_id="user:7", agent_id="agent",
        request_id="request-current", text="private request body", source="api",
        idempotency_key="request-current",
    )
    path = _path(runtime, session)
    duplicate = _line("重复内容")
    path.write_bytes(duplicate + duplicate)
    before = _database_snapshot(tmp_path)

    result = await entry(runtime, _wire(), source_channel="workbench_api",
                         chat_id=999, session_metadata={"owner_id": "user:999", "session_id": "forged"})

    assert result is not None, "reserved projection fell through to ordinary text"
    assert result["ok"] is True
    assert result["chat_projection_version"] == 1
    payload = result["projection"]
    assert payload["session_id"] == session["session_id"]
    assert payload["context_generation"] == 1
    assert payload["offset"] == len(duplicate) * 2
    assert len({row["message_ref"] for row in payload["messages"]}) == 3
    assert payload["messages"][0]["text"] == "private request body"
    assert payload["messages"][0]["canonical"] is True
    assert [row["source_sequence"] for row in payload["messages"][1:]] == [
        0,
        len(duplicate),
    ]
    assert [run["request_id"] for run in payload["requests"]] == [accepted.request_id]
    assert "text" not in payload["requests"][0]
    assert payload["request_discovery_complete"] is True
    assert payload["activity_replay_durable"] is False

    polled = await entry(
        runtime,
        _wire(
            op="poll",
            offset=len(duplicate),
            message_cursor=payload["message_cursor"],
        ),
        source_channel="workbench_api",
    )
    assert polled["projection"]["messages"] == payload["messages"][2:]
    assert _database_snapshot(tmp_path) == before
    assert path.read_bytes() == duplicate + duplicate
    assert not (tmp_path / "slash_command_audit.jsonl").exists()
    assert "重复内容" not in caplog.text and "private request body" not in caplog.text
    runtime.enqueue_request.assert_not_awaited()
    runtime._send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_projection_uses_current_binding_and_generation_for_messages_and_run_discovery(tmp_path):
    runtime = _runtime(tmp_path)
    store = runtime.session_store
    original = _session(runtime)
    _path(runtime, original).write_bytes(_line("default history"))
    selected = store.create_session(owner_id="user:7", agent_id="agent")
    _path(runtime, selected).write_bytes(_line("old context"))
    store.accept_run(session_id=selected["session_id"], owner_id="user:7", agent_id="agent",
                     request_id="old-context-run", text="old request", source="api", idempotency_key="old")
    store.mark_request_running("old-context-run", worker_id="fixture")
    store.finish_request("old-context-run", success=True, assistant_text="old answer", assistant_source="fixture")
    fresh = store.start_fresh_generation(selected["session_id"])
    store.bind_channel(owner_id="user:7", agent_id="agent", surface="workbench", channel_key="default",
                       session_id=fresh["session_id"])
    for target, owner, agent, request_id in [
        (original, "user:7", "agent", "default-run"),
        (fresh, "user:7", "agent", "selected-run"),
        (store.create_session(owner_id="user:999", agent_id="agent"), "user:999", "agent", "foreign-owner-run"),
        (store.create_session(owner_id="user:7", agent_id="other"), "user:7", "other", "other-agent-run"),
    ]:
        store.accept_run(session_id=target["session_id"], owner_id=owner, agent_id=agent,
                         request_id=request_id, text=request_id, source="api", idempotency_key=request_id)
    _path(runtime, fresh).write_bytes(
        _line("current", session_id=fresh["session_id"], context_generation=2)
        + _line("foreign", session_id=original["session_id"])
        + _line("stale", context_generation=1)
    )

    result = await try_execute_slash_command_text(runtime, _wire(), source_channel="workbench_api")

    assert result is not None, "reserved projection fell through to ordinary text"
    payload = result["projection"]
    assert payload["session_id"] == fresh["session_id"]
    assert payload["context_generation"] == 2
    assert [row["text"] for row in payload["messages"]] == [
        "selected-run",
        "current",
    ]
    assert [run["request_id"] for run in payload["requests"]] == ["selected-run"]


@pytest.mark.asyncio
async def test_projection_poll_preserves_half_record_then_acknowledges_completion_once(tmp_path):
    runtime = _runtime(tmp_path)
    session = _session(runtime)
    path = _path(runtime, session)
    first, pending = _line("first"), _line("下一条")
    path.write_bytes(first + pending[:20])

    result = await try_execute_slash_command_text(runtime, _wire(op="poll", offset=len(first)),
                                                 source_channel="workbench_api")
    assert result is not None, "reserved projection fell through to ordinary text"
    assert result["projection"]["messages"] == []
    assert result["projection"]["offset"] == len(first)
    with path.open("ab") as stream:
        stream.write(pending[20:])
    completed = await try_execute_slash_command_text(runtime, _wire(op="poll", offset=len(first)),
                                                    source_channel="workbench_api")
    assert [row["text"] for row in completed["projection"]["messages"]] == ["下一条"]
    end = completed["projection"]["offset"]
    empty = await try_execute_slash_command_text(runtime, _wire(op="poll", offset=end), source_channel="workbench_api")
    assert empty["projection"]["messages"] == []
    reset = await try_execute_slash_command_text(runtime, _wire(op="poll", offset=end + 10), source_channel="workbench_api")
    assert reset["projection"]["cursor_reset"] is True
    assert reset["projection"]["history_complete"] is False


@pytest.mark.asyncio
async def test_projection_v2_restores_authoritative_command_ui_while_v1_stays_compatible(tmp_path):
    runtime = _runtime(tmp_path)
    session = _session(runtime)
    menu_id = "menuabcdefghijklmnop"
    command_ui = {
        "version": 1,
        "menu_id": menu_id,
        "revision": 1,
        "expires_at": 4_102_444_800_000,
        "closed": False,
        "rows": [[{
            "text": "Continue",
            "button_id": "buttonabcdefghijkl",
            "disabled": False,
        }]],
    }
    runtime.session_store.append_presentation_message(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="agent",
        role="assistant",
        text="Choose",
        source="telegram.reply",
        idempotency_key="workbench:default:assistant:command-menu",
        content_format="telegram-html",
        presentation_channel="command",
        message_context={"command_ui": command_ui},
    )

    v2 = await try_execute_slash_command_text(
        runtime,
        _wire(version=2, capabilities={"command_ui": True}),
        source_channel="workbench_api",
    )
    v1 = await try_execute_slash_command_text(
        runtime, _wire(), source_channel="workbench_api"
    )

    assert v2["ok"] is True
    assert v2["chat_projection_version"] == 2
    projected = v2["projection"]["messages"][0]
    assert projected["message_ref"] == f"command-ui:{menu_id}"
    assert projected["command_ui"] == command_ui
    assert v2["projection"]["command_uis"] == [{
        "message_ref": f"command-ui:{menu_id}",
        "command_ui": command_ui,
    }]
    assert v1["chat_projection_version"] == 1
    assert "command_ui" not in v1["projection"]["messages"][0]
    assert "command_uis" not in v1["projection"]


@pytest.mark.asyncio
async def test_projection_v2_reads_the_latest_persisted_menu_revision(tmp_path):
    runtime = _runtime(tmp_path)
    store = runtime.session_store
    session = _session(runtime)
    menu_id = "menuabcdefghijklmnop"
    first_ui = {
        "version": 1,
        "menu_id": menu_id,
        "revision": 1,
        "expires_at": 4_102_444_800_000,
        "closed": False,
        "rows": [],
    }
    recorded = store.append_presentation_message(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="agent",
        role="assistant",
        text="Page one",
        source="telegram.reply",
        idempotency_key="workbench:default:assistant:command-menu-update",
        presentation_channel="command",
        message_context={"command_ui": first_ui},
    )
    latest_ui = {**first_ui, "revision": 2}

    store.update_presentation_message(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="agent",
        message_id=recorded["message_id"],
        text="Page two",
        message_context={"command_ui": latest_ui},
    )
    result = await try_execute_slash_command_text(
        runtime,
        _wire(version=2, capabilities={"command_ui": True}),
        source_channel="workbench_api",
    )
    polled = await try_execute_slash_command_text(
        runtime,
        _wire(
            version=2,
            capabilities={"command_ui": True},
            op="poll",
            offset=0,
            message_cursor=recorded["ordinal"],
        ),
        source_channel="workbench_api",
    )

    assert result["projection"]["messages"][0]["text"] == "Page two"
    assert result["projection"]["messages"][0]["command_ui"]["revision"] == 2
    assert polled["projection"]["messages"][0]["text"] == "Page two"
    assert polled["projection"]["messages"][0]["command_ui"]["revision"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [
    {"version": True}, {"version": 2}, {"op": "send"}, {"op": []},
    {"version": 2, "capabilities": {"command_ui": 1}},
    {"version": 2, "capabilities": {"command_ui": True, "extra": True}},
    {"limit": True}, {"limit": "2"}, {"limit": 1.5}, {"limit": 0}, {"limit": 201},
    {"op": "poll"}, {"op": "poll", "offset": -1}, {"op": "poll", "offset": True},
    {"op": "poll", "offset": "0"}, {"op": "poll", "offset": 9007199254740992},
    {"op": "poll", "offset": 0, "message_cursor": -1},
    {"op": "poll", "offset": 0, "message_cursor": True},
    {"op": "poll", "offset": 0, "message_cursor": 9007199254740992},
    {"message_cursor": 0},
    {"offset": 0}, {"op": "poll", "offset": 0, "limit": 2},
    {"owner_id": "user:999"}, {"session_id": "foreign"}, {"path": "/private"},
])
async def test_invalid_transport_is_terminal_and_does_not_create_session_or_audit(tmp_path, fields):
    runtime = _runtime(tmp_path)
    before = _database_snapshot(tmp_path)
    result = await try_execute_slash_command_text(runtime, _wire(**fields), source_channel="workbench_api")
    assert result == {"ok": False, "chat_projection_version": 1,
                      "error_code": "chat_projection_request_invalid", "http_status": 400}
    assert _database_snapshot(tmp_path) == before
    assert not (tmp_path / "slash_command_audit.jsonl").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("source,actor,allowed,profile,status,error", [
    ("telegram", 7, True, "personal", 403, "forbidden"),
    ("api_chat", 7, True, "personal", 403, "forbidden"),
    ("workbench_api", None, True, "personal", 403, "forbidden"),
    ("workbench_api", 0, True, "personal", 403, "forbidden"),
    ("workbench_api", -1, True, "personal", 403, "forbidden"),
    ("workbench_api", True, True, "personal", 403, "forbidden"),
    ("workbench_api", "7", True, "personal", 403, "forbidden"),
    ("workbench_api", 7, False, "personal", 403, "forbidden"),
    ("workbench_api", 7, True, "enterprise", 501, "governed_not_supported"),
])
async def test_authority_rejection_happens_before_session_resolution(tmp_path, source, actor, allowed, profile, status, error):
    runtime = _runtime(tmp_path)
    runtime.global_config.authorized_id = actor
    runtime.global_config.deployment_profile = profile
    runtime._is_authorized_user = lambda actor: allowed
    before = _database_snapshot(tmp_path)
    result = await try_execute_slash_command_text(runtime, _wire(), source_channel=source)
    assert result == {"ok": False, "chat_projection_version": 1,
                      "error_code": "chat_projection_" + error, "http_status": status}
    assert _database_snapshot(tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", [execute_local_command, try_execute_slash_command_text])
async def test_existing_shared_proxy_entry_routes_real_projection_to_worker(tmp_path, entry):
    worker = _runtime(tmp_path)
    _path(worker, _session(worker)).write_bytes(_line("worker generation"))

    class ExistingProxy:
        is_function_worker_proxy = True

        async def execute_slash_command(self, text, **kwargs):
            return await try_execute_slash_command_text(worker, text, **kwargs)

    result = await entry(ExistingProxy(), _wire(), source_channel="workbench_api")
    assert result["ok"] is True
    assert result["projection"]["messages"][0]["text"] == "worker generation"


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded", ["", "!bad!", "☃", "a" * 1024, "W10", "eA"])
async def test_malformed_reserved_wire_returns_typed_terminal_error_without_body_logging(tmp_path, caplog, encoded):
    runtime = _runtime(tmp_path)
    result = await execute_local_command(runtime, PREFIX + encoded, source_channel="workbench_api")
    assert result == {"ok": False, "chat_projection_version": 1,
                      "error_code": "chat_projection_request_invalid", "http_status": 400}
    assert runtime.session_store.list_sessions(owner_id="user:7") == []
    assert not (tmp_path / "slash_command_audit.jsonl").exists()
    assert PREFIX not in caplog.text


@pytest.mark.asyncio
async def test_real_transcript_read_failure_is_terminal_and_does_not_leak_file_path(tmp_path, caplog):
    runtime = _runtime(tmp_path)
    path = _path(runtime, _session(runtime))
    path.mkdir()
    result = await try_execute_slash_command_text(runtime, _wire(), source_channel="workbench_api")
    assert result == {"ok": False, "chat_projection_version": 1,
                      "error_code": "chat_projection_read_unavailable", "http_status": 503}
    assert str(tmp_path) not in caplog.text
    assert not (tmp_path / "slash_command_audit.jsonl").exists()
    runtime.enqueue_request.assert_not_awaited()
    runtime._send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_text_retains_noop_behavior(tmp_path):
    assert await try_execute_slash_command_text(_runtime(tmp_path), "ordinary text") is None
