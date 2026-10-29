"""Real command/capture/SQLite/FC recovery; no transport or lifecycle effects."""

from __future__ import annotations

import logging
import asyncio
from types import MethodType, SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from orchestrator import command_interaction_bridge as bridge, runtime_session
from orchestrator import telegram_notifications
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.frontend_projection import project_frontend_event
from orchestrator.chat_transcript_projection import build_chat_projection
from orchestrator.session_store import SessionNotFound, SessionStore


@pytest.fixture
def command_runtime(tmp_path, monkeypatch):
    workspace = tmp_path / "workspaces" / "fixture"
    workspace.mkdir(parents=True)
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="COMMAND_TEST")
    primary = store.ensure_default_session(owner_id="user:7", agent_id="fixture")
    selected = store.create_session(
        owner_id="user:7", agent_id="fixture", title="Selected"
    )
    runtime = NS(
        name="fixture",
        workspace_dir=workspace,
        session_store=store,
        global_config=NS(
            authorized_id=7,
            bridge_home=tmp_path,
            project_root=tmp_path,
            instance_id="COMMAND_TEST",
            ui_language="en",
        ),
        config=NS(extra={}, active_backend="codex-cli"),
        paths=NS(code_root=tmp_path, bridge_home=tmp_path),
        logger=logging.getLogger("command-test"),
        telegram_logger=logging.getLogger("command-test"),
        _is_authorized_user=lambda actor: actor == 7,
        _is_command_allowed=lambda name: True,
        _send_text=AsyncMock(side_effect=AssertionError("transport escaped capture")),
        app=NS(bot=None),
    )
    for name in (
        "_reply_text",
        "send_long_message",
        "cmd_current",
        "cmd_sessions",
        "cmd_version",
        "cmd_timeout",
    ):
        setattr(runtime, name, MethodType(getattr(FlexibleAgentRuntime, name), runtime))
    modes = {"mode": "on"}
    monkeypatch.setattr(
        telegram_notifications, "notification_mode", lambda _: modes["mode"]
    )

    def set_mode(_, value):
        modes["mode"] = value
        return value

    monkeypatch.setattr(telegram_notifications, "set_notification_mode", set_mode)
    metadata = {
        "actor_id": 7,
        "instance_id": "COMMAND_TEST",
        "owner_id": "user:7",
        "session_id": selected["session_id"],
        "context_generation": 1,
        "session_surface": "workbench",
        "session_channel_key": "default",
        "connection_binding": "bindingabcdefghijkl",
        "_durable_command_invocation": True,
    }
    return runtime, metadata, primary


def operation(command, **changes):
    return {
        "version": 1,
        "op": "open",
        "command": command,
        "ui_locale": "en",
        "client_id": "clientabcdefghijkl",
        "request_id": "requestabcdefghijkl",
        **changes,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "/queue help",
        "/delay help",
        "/version",
        "/current",
        "/sessions",
        "/notify",
        "/timeout",
        "/wiki",
    ],
)
async def test_command_input_and_result_survive_reopen_in_selected_session(
    command_runtime, command
):
    runtime, metadata, primary = command_runtime
    result = await bridge.dispatch_command_interaction(
        runtime, operation(command), metadata
    )
    assert result["ok"], result
    assert result["messages"]
    reopened = SessionStore(runtime.session_store.db_path, instance_id="COMMAND_TEST")
    messages = reopened.messages(metadata["session_id"], owner_id="user:7")
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["text"] == command
    assert all(not m["history_eligible"] for m in messages)
    assert messages[1]["text"] == result["messages"][-1]["text"]
    projection = build_chat_projection(
        reopened,
        session=reopened.get_session(metadata["session_id"], owner_id="user:7"),
        owner_id="user:7",
        include_command_ui=True,
    )
    assert [m["role"] for m in projection["messages"]] == ["user", "assistant"]
    assert (
        projection["messages"][-1]["message_ref"]
        == result["messages"][-1]["message_ref"]
    )
    assert not reopened.messages(primary["session_id"], owner_id="user:7")
    replay = await bridge.dispatch_command_interaction(
        runtime, operation(command), metadata
    )
    assert replay["replayed"]
    assert len(reopened.messages(metadata["session_id"], owner_id="user:7")) == 2


@pytest.mark.asyncio
async def test_notify_edit_and_close_publish_one_revision_of_text_and_actions(
    command_runtime,
):
    runtime, metadata, _ = command_runtime
    opened = await bridge.dispatch_command_interaction(
        runtime, operation("/notify"), metadata
    )
    assert opened["ok"], opened
    card = opened["messages"][-1]
    menu = runtime._command_interaction_store.menus[card["command_ui"]["menu_id"]]
    action = next(
        key for key, (data, _) in menu.actions.items() if data == "notify:quiet"
    )
    payload = operation(
        "",
        op="act",
        request_id="actionabcdefghijkl",
        menu_id=menu.id,
        revision=1,
        button_id=action,
    )
    edited = await bridge.dispatch_command_interaction(runtime, payload, metadata)
    assert edited["ok"], edited
    rows = runtime.session_store.messages(metadata["session_id"], owner_id="user:7")
    row = next(m for m in rows if m["role"] == "assistant")
    assert row["message_id"] == menu.presentation_message_id
    assert "QUIET" in row["text"]
    canonical = row["message_context"]["frontend_presentation"]["content_blocks"]
    direct = edited["messages"][-1]["presentation"]["content_blocks"]
    assert canonical == direct
    for event in runtime.session_store.events(
        metadata["session_id"], owner_id="user:7"
    ):
        if event["kind"] == "frontend.message.updated":
            projected = project_frontend_event(
                event, message_map={row["message_id"]: row}
            )
            assert projected["content_blocks"] == direct
    assert {b["action_id"] for b in canonical if b["type"] == "action"} == set(
        menu.actions
    )
    assert all(
        "<b>" not in b.get("text", "") for b in canonical if b.get("format") == "plain"
    )
    stale = await bridge.dispatch_command_interaction(
        runtime, {**payload, "request_id": "staleabcdefghijkl"}, metadata
    )
    assert not stale["ok"]
    closed = await bridge.dispatch_command_interaction(
        runtime,
        operation(
            "", op="close", request_id="closeabcdefghijkl", menu_id=menu.id, revision=2
        ),
        metadata,
    )
    assert closed["ok"]
    row = next(
        m
        for m in runtime.session_store.messages(
            metadata["session_id"], owner_id="user:7"
        )
        if m["role"] == "assistant"
    )
    assert row["text"] and row["message_context"]["command_ui"]["closed"]
    assert not any(
        b["type"] == "action"
        for b in row["message_context"]["frontend_presentation"]["content_blocks"]
    )


def test_explicit_publication_rejects_foreign_session_without_default_fallback(
    command_runtime,
):
    runtime, metadata, primary = command_runtime
    foreign = runtime.session_store.create_session(
        owner_id="user:8", agent_id="fixture"
    )
    with pytest.raises(SessionNotFound):
        runtime_session.publish_frontend_message(
            runtime,
            role="assistant",
            text="Private",
            source="test",
            publication_id="explicit",
            surface="workbench",
            channel_key="default",
            explicit_session_id=foreign["session_id"],
        )
    assert not runtime.session_store.messages(primary["session_id"], owner_id="user:7")


@pytest.mark.asyncio
async def test_native_telegram_notify_edit_updates_the_same_canonical_message(
    command_runtime, monkeypatch
):
    from orchestrator.commands.notify import notify_command, notify_callback

    runtime, metadata, primary = command_runtime
    monkeypatch.setattr(
        FlexibleAgentRuntime, "_telegram_channel_allowed", AsyncMock(return_value=True)
    )
    sent = NS(message_id=100)
    message = NS(message_id=10, reply_text=AsyncMock(return_value=sent))
    update = NS(
        message=message,
        effective_message=message,
        effective_chat=NS(id=7),
        effective_user=NS(id=7),
        update_id=10,
        callback_query=None,
    )
    await notify_command(runtime, update, NS(args=[]))
    before = runtime.session_store.messages(primary["session_id"], owner_id="user:7")[
        -1
    ]
    query = NS(
        id="native-notify-quiet",
        data="notify:quiet",
        from_user=NS(id=7),
        message=NS(message_id=100, chat_id=7, chat=NS(id=7)),
        edit_message_text=AsyncMock(return_value=sent),
        answer=AsyncMock(),
    )
    update = NS(
        message=None,
        effective_message=query.message,
        effective_chat=NS(id=7),
        effective_user=NS(id=7),
        update_id=11,
        callback_query=query,
    )

    async def handler(update, context):
        return await notify_callback(runtime, update, context)

    wrapped = FlexibleAgentRuntime._wrap_callback(runtime, "notify", handler)
    await wrapped(update, NS(args=[]))
    row = runtime.session_store.messages(primary["session_id"], owner_id="user:7")[-1]
    assert row["message_id"] == before["message_id"]
    assert "QUIET" in row["text"]
    assert row["text"] == query.edit_message_text.call_args.args[0]
    assert all(
        "<b>" not in b.get("text", "")
        for b in row["message_context"]["frontend_presentation"]["content_blocks"]
    )
    await wrapped(update, NS(args=[]))
    assert query.edit_message_text.await_count == 1


@pytest.mark.asyncio
async def test_reboot_notice_keeps_frozen_selected_session(command_runtime):
    from orchestrator.admin_local_testing import _FakeUpdate, _CaptureStore
    from orchestrator.runtime_reboot import origin_from_update
    from orchestrator.reboot_manager import RebootManager

    runtime, metadata, primary = command_runtime
    update = _FakeUpdate(7, 7, _CaptureStore([]), "/reboot", session_metadata=metadata)
    origin = origin_from_update(runtime, update)
    kernel = NS(
        paths=runtime.paths,
        global_cfg=runtime.global_config,
        session_store=runtime.session_store,
    )
    manager = RebootManager(kernel, console_handler=None)
    record = manager.receipts.create(
        source=runtime.name,
        targets=[runtime.name],
        display_names={runtime.name: "Fixture"},
        mode="min",
        origin=origin,
    )
    result = await manager._deliver(record)
    assert result["sent"]
    rows = runtime.session_store.messages(metadata["session_id"], owner_id="user:7")
    assert len(rows) == 1 and rows[0]["role"] == "assistant"
    assert not runtime.session_store.messages(primary["session_id"], owner_id="user:7")
    assert (
        manager.receipts.get(record["id"])["origin"]["session_id"]
        == metadata["session_id"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation_name", ["clone", "move"])
async def test_transfer_completion_survives_manager_recreation_without_telegram(
    command_runtime, monkeypatch, operation_name
):
    from orchestrator.agent_move import manager as moves
    from orchestrator.admin_local_testing import _FakeUpdate, _CaptureStore
    from orchestrator.runtime_reboot import origin_from_update

    runtime, metadata, primary = command_runtime
    origin = origin_from_update(
        runtime, _FakeUpdate(7, 7, _CaptureStore([]), "", session_metadata=metadata)
    )
    origin.pop("chat_id", None)  # A backend-only client has no Telegram address.
    monkeypatch.setattr(
        moves,
        "get_outbound_move",
        lambda *_: {"operation": operation_name, "agent_id": "fixture"},
    )
    kernel = NS(
        paths=runtime.paths,
        global_cfg=runtime.global_config,
        session_store=runtime.session_store,
    )
    manager = moves.AgentMoveManager(kernel)
    manager.submit(
        "transfer-fc",
        {},
        requested_by="fixture",
        origin=origin,
        locale="zh-CN",
        _schedule=False,
    )
    manager._update(
        "transfer-fc",
        status="completed",
        result={
            "operation": operation_name,
            "agent_id": "fixture",
            "target_agent_id": "copy",
            "target_instance": "PEER",
        },
    )
    replacement = moves.AgentMoveManager(kernel)
    await replacement._deliver_result(
        "transfer-fc", replacement._require_record("transfer-fc")
    )
    rows = runtime.session_store.messages(metadata["session_id"], owner_id="user:7")
    assert len(rows) == 1 and "PEER" in rows[0]["text"]
    await replacement._deliver_result(
        "transfer-fc", replacement._require_record("transfer-fc")
    )
    assert (
        len(runtime.session_store.messages(metadata["session_id"], owner_id="user:7"))
        == 1
    )
    assert not runtime.session_store.messages(primary["session_id"], owner_id="user:7")


@pytest.mark.asyncio
async def test_restart_peer_rejection_after_capture_returns_to_original_session(
    command_runtime, monkeypatch
):
    from orchestrator.commands import restart_lifeline

    runtime, metadata, primary = command_runtime
    monkeypatch.setattr(
        restart_lifeline,
        "peer_restart_provider",
        lambda *a, **k: {"target_instance": "PEER"},
    )
    monkeypatch.setattr(
        restart_lifeline,
        "restart_via_provider",
        lambda *a, **k: (1, {"error": "isolated rejection"}),
    )
    tasks = []
    create_task = asyncio.create_task

    def own_task(coro, **kwargs):
        task = create_task(coro, **kwargs)
        tasks.append(task)
        return task

    monkeypatch.setattr(restart_lifeline.asyncio, "create_task", own_task)
    opened = await bridge.dispatch_command_interaction(
        runtime, operation("/restart PEER"), metadata
    )
    assert opened["ok"], opened
    await asyncio.gather(*tasks)
    rows = runtime.session_store.messages(metadata["session_id"], owner_id="user:7")
    assert any("isolated rejection" in row["text"] for row in rows)
    assert not runtime.session_store.messages(primary["session_id"], owner_id="user:7")
    runtime._send_text.assert_not_awaited()


def test_command_input_is_redacted_and_atomic_with_reservation(
    command_runtime, monkeypatch
):
    from orchestrator.frontend_command_admission import (
        reserve_frontend_command_invocation,
    )

    runtime, metadata, _ = command_runtime
    payload = operation("/status token=sensitive-fixture")
    invocation = bridge.build_frontend_command_invocation(
        payload,
        metadata,
        actor=7,
        command_name="status",
        arguments=["token=sensitive-fixture"],
    )
    reserve = dict(
        session_id=metadata["session_id"],
        owner_id="user:7",
        client_id=payload["client_id"],
        request_id=payload["request_id"],
        context_generation=1,
        payload=payload,
        invocation=invocation,
        record_input=True,
    )
    original = runtime.session_store.append_presentation_message

    def fail(**kwargs):
        raise OSError("isolated input persistence failure")

    monkeypatch.setattr(runtime.session_store, "append_presentation_message", fail)
    with pytest.raises(OSError):
        reserve_frontend_command_invocation(runtime, **reserve)
    monkeypatch.setattr(runtime.session_store, "append_presentation_message", original)
    ticket = reserve_frontend_command_invocation(runtime, **reserve)
    assert (
        ticket.state == "reserved"
    ), "the failed display transaction must not leave a pending business reservation"
    rows = runtime.session_store.messages(metadata["session_id"], owner_id="user:7")
    assert [m["text"] for m in rows] == ["/status token=[redacted]"]
    assert "sensitive-fixture" not in str(rows)


def test_control_reservation_does_not_invent_user_input(command_runtime):
    from orchestrator.frontend_command_admission import (
        reserve_frontend_command_in_store,
    )

    runtime, metadata, _ = command_runtime
    payload = operation("/transfer-redirect", record_input=True)
    invocation = bridge.build_frontend_command_invocation(
        payload, metadata, actor=7, command_name="transfer-redirect", arguments=[]
    )
    invocation["record_input"] = True
    reserve = dict(
        session_id=metadata["session_id"],
        owner_id="user:7",
        client_id=payload["client_id"],
        request_id=payload["request_id"],
        context_generation=1,
        payload=payload,
        invocation=invocation,
    )
    ticket = reserve_frontend_command_in_store(runtime.session_store, **reserve)
    assert ticket.state == "reserved"
    assert not runtime.session_store.messages(metadata["session_id"], owner_id="user:7")
    replay = reserve_frontend_command_in_store(runtime.session_store, **reserve)
    assert replay.state == "pending" and replay.replayed
    assert not runtime.session_store.messages(metadata["session_id"], owner_id="user:7")


def test_legacy_menu_update_rebuilds_typed_actions_and_text_not_raw_html(
    command_runtime,
):
    from orchestrator.command_interactions import Binding, Capture, MenuStore

    runtime, metadata, _ = command_runtime
    menus = MenuStore()
    binding = Binding(
        "COMMAND_TEST",
        "fixture",
        "7",
        metadata["session_id"],
        1,
        "clientabcdefghijkl",
        "bindingabcdefghijkl",
    )
    menu = menus.create(binding, "notify")
    capture = Capture(menus, binding, "notify", lambda data: ("callback",))
    capture._set(
        menu,
        "<b>ON</b>",
        "HTML",
        {"inline_keyboard": [[{"text": "Off", "callback_data": "notify:off"}]]},
    )
    published = runtime_session.publish_frontend_message(
        runtime,
        role="assistant",
        text=menu.text,
        source="test",
        publication_id="legacy-menu",
        surface="workbench",
        channel_key="default",
        explicit_session_id=metadata["session_id"],
        content_format="telegram-html",
        message_context={
            "command_ui": menus.render(menu),
            "frontend_presentation": menus.message(menu)["presentation"],
        },
    )
    menu.revision += 1
    capture._set(
        menu,
        "<b>OFF</b>",
        "HTML",
        {"inline_keyboard": [[{"text": "On", "callback_data": "notify:on"}]]},
    )
    updated = runtime.session_store.update_presentation_message(
        session_id=metadata["session_id"],
        owner_id="user:7",
        agent_id="fixture",
        message_id=published["message_id"],
        text=menu.text,
        message_context={"command_ui": menus.render(menu)},
    )
    assert (
        updated["message_context"]["frontend_presentation"]["content_blocks"]
        == menus.message(menu)["presentation"]["content_blocks"]
    )
