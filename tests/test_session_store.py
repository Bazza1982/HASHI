from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace

import pytest

from orchestrator import runtime_session
from orchestrator.session_store import (
    IdempotencyConflict,
    SessionConflict,
    SessionNotFound,
    SessionStore,
    StaleFencingToken,
)


def _store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI1")


def test_runtime_without_bridge_root_keeps_session_db_in_its_workspace(tmp_path):
    runtime = type("Runtime", (), {})()
    runtime.name = "test-agent"
    runtime.workspace_dir = tmp_path / "workspace"
    runtime.global_config = type(
        "Config", (), {"authorized_id": 7, "instance_id": "TEST"}
    )()

    store = runtime_session.ensure_store(runtime)

    assert store.db_path == runtime.workspace_dir / "state" / "sessions.sqlite3"
    assert not (tmp_path / "state" / "sessions.sqlite3").exists()


def _complete(
    store: SessionStore,
    *,
    session_id: str,
    owner_id: str,
    request_id: str,
    key: str,
    text: str,
    answer: str,
    agent_id: str = "lily",
    source: str = "test",
):
    accepted = store.accept_run(
        session_id=session_id,
        owner_id=owner_id,
        agent_id=agent_id,
        request_id=request_id,
        text=text,
        source=source,
        idempotency_key=key,
    )
    assert store.mark_request_running(request_id, worker_id="test-worker") == 1
    run = store.finish_request(
        request_id,
        success=True,
        assistant_text=answer,
        assistant_source="test-backend",
    )
    assert run and run["state"] == "completed"
    return accepted


def test_conversation_continuity_capsule_imports_history_before_new_target_messages_idempotently(tmp_path):
    owner = "user:7"
    source = SessionStore(tmp_path / "source" / "state" / "sessions.sqlite3", instance_id="HASHI2")
    source_session = source.ensure_default_session(owner_id=owner, agent_id="lily")
    source.bind_channel(
        owner_id=owner,
        agent_id="lily",
        surface="workbench",
        channel_key="default",
        session_id=source_session["session_id"],
    )
    source.bind_channel(
        owner_id=owner,
        agent_id="lily",
        surface="telegram",
        channel_key="7",
        session_id=source_session["session_id"],
    )
    _complete(
        source,
        session_id=source_session["session_id"],
        owner_id=owner,
        request_id="source-old",
        key="source-old",
        text="old user message",
        answer="old assistant message",
        source="workbench",
    )

    capsule = source.export_conversation_continuity(
        owner_id=owner,
        agent_id="lily",
        source_instance="HASHI2",
        transfer_id="transfer-0001",
        history_mode="move",
    )

    target = SessionStore(tmp_path / "target" / "state" / "sessions.sqlite3", instance_id="HASHI3")
    target_session = target.ensure_default_session(owner_id=owner, agent_id="lily")
    _complete(
        target,
        session_id=target_session["session_id"],
        owner_id=owner,
        request_id="target-new",
        key="target-new",
        text="new target message",
        answer="new target answer",
        source="workbench",
    )

    first = target.import_conversation_continuity(
        capsule,
        owner_id=owner,
        agent_id="lily",
        transfer_id="transfer-0001",
        history_mode="move",
    )
    second = target.import_conversation_continuity(
        capsule,
        owner_id=owner,
        agent_id="lily",
        transfer_id="transfer-0001",
        history_mode="move",
    )

    resolved = target.resolve_session(
        owner_id=owner,
        agent_id="lily",
        surface="workbench",
        channel_key="default",
    )
    messages = target.messages(resolved["session_id"], owner_id=owner)
    assert [item["text"] for item in messages] == [
        "old user message",
        "old assistant message",
        "new target message",
        "new target answer",
    ]
    assert len({item["message_id"] for item in messages}) == 4
    provenance = messages[0]["message_context"]["conversation_continuity"]
    assert provenance == {
        "schema_version": 1,
        "source_instance": "HASHI2",
        "source_session_id": source_session["session_id"],
        "source_message_id": source.messages(source_session["session_id"])[0]["message_id"],
        "source_ordinal": 1,
        "source_created_at": source.messages(source_session["session_id"])[0]["created_at"],
        "origin_ref": capsule["sessions"][0]["messages"][0]["origin_ref"],
        "transfer_id": "transfer-0001",
        "history_mode": "move",
    }
    assert first["imported_messages"] == 2
    assert second["imported_messages"] == 0
    assert second["replayed"] is True


def test_read_only_history_dedup_does_not_create_an_empty_archive_for_a_new_transfer(tmp_path):
    owner = "user:7"
    source = SessionStore(
        tmp_path / "source-read-only.sqlite",
        instance_id="HASHI2",
    )
    source_session = source.ensure_default_session(owner_id=owner, agent_id="lily")
    _complete(
        source,
        session_id=source_session["session_id"],
        owner_id=owner,
        request_id="read-only-source",
        key="read-only-source",
        text="stable source message",
        answer="stable source answer",
    )
    first_capsule = source.export_conversation_continuity(
        owner_id=owner,
        agent_id="lily",
        source_instance="HASHI2",
        transfer_id="read-only-transfer-1",
        history_mode="inherit_read_only",
    )
    second_capsule = source.export_conversation_continuity(
        owner_id=owner,
        agent_id="lily",
        source_instance="HASHI2",
        transfer_id="read-only-transfer-2",
        history_mode="inherit_read_only",
    )
    target = SessionStore(
        tmp_path / "target-read-only.sqlite",
        instance_id="HASHI3",
    )

    first = target.import_conversation_continuity(
        first_capsule,
        owner_id=owner,
        agent_id="lily-clone",
        transfer_id="read-only-transfer-1",
        history_mode="inherit_read_only",
    )
    second = target.import_conversation_continuity(
        second_capsule,
        owner_id=owner,
        agent_id="lily-clone",
        transfer_id="read-only-transfer-2",
        history_mode="inherit_read_only",
    )

    sessions = target.list_sessions(
        owner_id=owner,
        agent_id="lily-clone",
        include_archived=True,
    )
    assert len(sessions) == 1
    assert second["target_session_ids"] == first["target_session_ids"]
    assert second["created_session_ids"] == []
    assert second["imported_messages"] == 0
    assert second["deduplicated_messages"] == 2

    first_rollback = target.rollback_conversation_continuity(
        "read-only-transfer-1"
    )
    surviving_session = target.get_session(
        first["target_session_ids"][0],
        owner_id=owner,
    )
    assert first_rollback["removed_messages"] == 0
    assert first_rollback["retained_shared_messages"] == 2
    assert [
        item["text"]
        for item in target.messages(surviving_session["session_id"], owner_id=owner)
    ] == ["stable source message", "stable source answer"]
    assert target.conversation_continuity_import_status(
        "read-only-transfer-2"
    )["imported_messages"] == 2

    second_rollback = target.rollback_conversation_continuity(
        "read-only-transfer-2"
    )
    assert second_rollback["removed_messages"] == 2
    assert target.list_sessions(
        owner_id=owner,
        agent_id="lily-clone",
        include_archived=True,
    ) == []


def test_conversation_continuity_preserves_independent_surface_sessions_and_owner_scope(tmp_path):
    owner = "user:7"
    source = SessionStore(
        tmp_path / "source-surfaces.sqlite",
        instance_id="HASHI2",
    )
    workbench = source.ensure_default_session(owner_id=owner, agent_id="lily")
    telegram = source.create_session(owner_id=owner, agent_id="lily")
    source.bind_channel(
        owner_id=owner,
        agent_id="lily",
        surface="workbench",
        channel_key="default",
        session_id=workbench["session_id"],
    )
    source.bind_channel(
        owner_id=owner,
        agent_id="lily",
        surface="telegram",
        channel_key="7",
        session_id=telegram["session_id"],
    )
    _complete(
        source,
        session_id=workbench["session_id"],
        owner_id=owner,
        request_id="surface-workbench",
        key="surface-workbench",
        text="workbench history",
        answer="workbench answer",
        source="workbench",
    )
    _complete(
        source,
        session_id=telegram["session_id"],
        owner_id=owner,
        request_id="surface-telegram",
        key="surface-telegram",
        text="telegram history",
        answer="telegram answer",
        source="telegram",
    )
    other_owner = source.ensure_default_session(owner_id="user:8", agent_id="lily")
    _complete(
        source,
        session_id=other_owner["session_id"],
        owner_id="user:8",
        request_id="surface-other-owner",
        key="surface-other-owner",
        text="must not transfer",
        answer="private answer",
    )

    capsule = source.export_conversation_continuity(
        owner_id=owner,
        agent_id="lily",
        source_instance="HASHI2",
        transfer_id="surface-transfer",
        history_mode="copy",
    )
    target = SessionStore(
        tmp_path / "target-surfaces.sqlite",
        instance_id="HASHI3",
    )
    existing_target = target.ensure_default_session(owner_id=owner, agent_id="lily")
    for surface, channel_key in (("workbench", "default"), ("telegram", "7")):
        target.bind_channel(
            owner_id=owner,
            agent_id="lily",
            surface=surface,
            channel_key=channel_key,
            session_id=existing_target["session_id"],
        )
    imported = target.import_conversation_continuity(
        capsule,
        owner_id=owner,
        agent_id="lily",
        transfer_id="surface-transfer",
        history_mode="copy",
    )

    workbench_target = target.resolve_session(
        owner_id=owner,
        agent_id="lily",
        surface="workbench",
        channel_key="default",
    )
    telegram_target = target.resolve_session(
        owner_id=owner,
        agent_id="lily",
        surface="telegram",
        channel_key="7",
    )
    assert workbench_target["session_id"] != telegram_target["session_id"]
    assert imported["source_session_map"] == {
        workbench["session_id"]: workbench_target["session_id"],
        telegram["session_id"]: telegram_target["session_id"],
    }
    assert [
        item["text"]
        for item in target.messages(workbench_target["session_id"], owner_id=owner)
    ] == ["workbench history", "workbench answer"]
    assert [
        item["text"]
        for item in target.messages(telegram_target["session_id"], owner_id=owner)
    ] == ["telegram history", "telegram answer"]
    assert all(
        item["text"] != "must not transfer"
        for session in target.list_sessions(owner_id=owner, agent_id="lily")
        for item in target.messages(session["session_id"], owner_id=owner)
    )


def test_conversation_continuity_import_is_atomic_on_mid_insert_failure(tmp_path):
    owner = "user:7"
    source = SessionStore(
        tmp_path / "source-atomic.sqlite",
        instance_id="HASHI2",
    )
    source_session = source.ensure_default_session(owner_id=owner, agent_id="lily")
    _complete(
        source,
        session_id=source_session["session_id"],
        owner_id=owner,
        request_id="atomic-source",
        key="atomic-source",
        text="atomic user",
        answer="atomic assistant",
    )
    capsule = source.export_conversation_continuity(
        owner_id=owner,
        agent_id="lily",
        source_instance="HASHI2",
        transfer_id="atomic-transfer",
        history_mode="copy",
    )
    target = SessionStore(
        tmp_path / "target-atomic.sqlite",
        instance_id="HASHI3",
    )
    with sqlite3.connect(target.db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_continuity_insert
            BEFORE INSERT ON messages
            WHEN NEW.source LIKE 'continuity:%' AND NEW.text = 'atomic assistant'
            BEGIN
                SELECT RAISE(ABORT, 'forced continuity failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced continuity failure"):
        target.import_conversation_continuity(
            capsule,
            owner_id=owner,
            agent_id="lily",
            transfer_id="atomic-transfer",
            history_mode="copy",
        )

    assert target.list_sessions(
        owner_id=owner,
        agent_id="lily",
        include_archived=True,
    ) == []
    with sqlite3.connect(target.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM conversation_continuity_imports"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM conversation_continuity_batches"
        ).fetchone()[0] == 0
        connection.execute("DROP TRIGGER fail_continuity_insert")

    imported = target.import_conversation_continuity(
        capsule,
        owner_id=owner,
        agent_id="lily",
        transfer_id="atomic-transfer",
        history_mode="copy",
    )
    assert imported["imported_messages"] == 2


def test_conversation_continuity_rejects_owner_change_and_rolls_back_only_imported_history(tmp_path):
    source = SessionStore(tmp_path / "source" / "state" / "sessions.sqlite3", instance_id="HASHI2")
    source_session = source.ensure_default_session(owner_id="user:7", agent_id="lily")
    _complete(
        source,
        session_id=source_session["session_id"],
        owner_id="user:7",
        request_id="old",
        key="old",
        text="old",
        answer="answer",
    )
    capsule = source.export_conversation_continuity(
        owner_id="user:7",
        agent_id="lily",
        source_instance="HASHI2",
        transfer_id="transfer-0002",
        history_mode="copy",
    )
    target = SessionStore(tmp_path / "target" / "state" / "sessions.sqlite3", instance_id="HASHI3")

    with pytest.raises(SessionConflict, match="owner"):
        target.import_conversation_continuity(
            capsule,
            owner_id="user:8",
            agent_id="lily",
            transfer_id="transfer-0002",
            history_mode="move",
        )

    imported = target.import_conversation_continuity(
        capsule,
        owner_id="user:7",
        agent_id="kasumi-clone",
        transfer_id="transfer-0002",
        history_mode="copy",
    )
    session = target.get_session(imported["target_session_ids"][0], owner_id="user:7")
    _complete(
        target,
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="kasumi-clone",
        request_id="after-import",
        key="after-import",
        text="target survives",
        answer="still here",
    )

    rolled_back = target.rollback_conversation_continuity("transfer-0002")
    remaining = target.messages(session["session_id"], owner_id="user:7")

    assert rolled_back["removed_messages"] == 2
    assert [item["text"] for item in remaining] == ["target survives", "still here"]
    assert source.messages(source_session["session_id"], owner_id="user:7")[0]["text"] == "old"


def test_conversation_rollback_restores_preexisting_channel_bindings(tmp_path):
    owner = "user:7"
    source = SessionStore(
        tmp_path / "source-bindings.sqlite", instance_id="HASHI2"
    )
    source_session = source.ensure_default_session(owner_id=owner, agent_id="lily")
    for surface, channel_key in (("workbench", "default"), ("telegram", "7")):
        source.bind_channel(
            owner_id=owner,
            agent_id="lily",
            surface=surface,
            channel_key=channel_key,
            session_id=source_session["session_id"],
        )
    _complete(
        source,
        session_id=source_session["session_id"],
        owner_id=owner,
        request_id="bound-source",
        key="bound-source",
        text="bound source",
        answer="bound answer",
    )
    capsule = source.export_conversation_continuity(
        owner_id=owner,
        agent_id="lily",
        source_instance="HASHI2",
        transfer_id="binding-rollback",
        history_mode="move",
    )

    target = SessionStore(
        tmp_path / "target-bindings.sqlite", instance_id="HASHI3"
    )
    workbench_session = target.ensure_default_session(
        owner_id=owner, agent_id="lily"
    )
    telegram_session = target.create_session(owner_id=owner, agent_id="lily")
    target.bind_channel(
        owner_id=owner,
        agent_id="lily",
        surface="workbench",
        channel_key="default",
        session_id=workbench_session["session_id"],
    )
    target.bind_channel(
        owner_id=owner,
        agent_id="lily",
        surface="telegram",
        channel_key="7",
        session_id=telegram_session["session_id"],
    )

    imported = target.import_conversation_continuity(
        capsule,
        owner_id=owner,
        agent_id="lily",
        transfer_id="binding-rollback",
        history_mode="move",
    )
    imported_target = imported["target_session_ids"][0]
    for surface, channel_key in (("workbench", "default"), ("telegram", "7")):
        assert target.resolve_session(
            owner_id=owner,
            agent_id="lily",
            surface=surface,
            channel_key=channel_key,
        )["session_id"] == imported_target

    target.rollback_conversation_continuity("binding-rollback")

    assert target.resolve_session(
        owner_id=owner,
        agent_id="lily",
        surface="workbench",
        channel_key="default",
    )["session_id"] == workbench_session["session_id"]
    assert target.resolve_session(
        owner_id=owner,
        agent_id="lily",
        surface="telegram",
        channel_key="7",
    )["session_id"] == telegram_session["session_id"]


def test_conversation_rollback_preserves_a_later_user_channel_rebind(tmp_path):
    owner = "user:7"
    source = SessionStore(
        tmp_path / "source-later-binding.sqlite",
        instance_id="HASHI2",
    )
    source_session = source.ensure_default_session(owner_id=owner, agent_id="lily")
    for surface, channel_key in (("workbench", "default"), ("telegram", "7")):
        source.bind_channel(
            owner_id=owner,
            agent_id="lily",
            surface=surface,
            channel_key=channel_key,
            session_id=source_session["session_id"],
        )
    _complete(
        source,
        session_id=source_session["session_id"],
        owner_id=owner,
        request_id="later-binding-source",
        key="later-binding-source",
        text="source history",
        answer="source answer",
    )
    capsule = source.export_conversation_continuity(
        owner_id=owner,
        agent_id="lily",
        source_instance="HASHI2",
        transfer_id="later-binding-rollback",
        history_mode="move",
    )

    target = SessionStore(
        tmp_path / "target-later-binding.sqlite",
        instance_id="HASHI3",
    )
    original_workbench = target.ensure_default_session(
        owner_id=owner,
        agent_id="lily",
    )
    original_telegram = target.create_session(owner_id=owner, agent_id="lily")
    target.bind_channel(
        owner_id=owner,
        agent_id="lily",
        surface="workbench",
        channel_key="default",
        session_id=original_workbench["session_id"],
    )
    target.bind_channel(
        owner_id=owner,
        agent_id="lily",
        surface="telegram",
        channel_key="7",
        session_id=original_telegram["session_id"],
    )
    imported = target.import_conversation_continuity(
        capsule,
        owner_id=owner,
        agent_id="lily",
        transfer_id="later-binding-rollback",
        history_mode="move",
    )
    imported_target = imported["target_session_ids"][0]
    later_session = target.create_session(owner_id=owner, agent_id="lily")
    changed_surface = (
        "workbench"
        if imported_target != original_workbench["session_id"]
        else "telegram"
    )
    changed_channel = "default" if changed_surface == "workbench" else "7"
    target.bind_channel(
        owner_id=owner,
        agent_id="lily",
        surface=changed_surface,
        channel_key=changed_channel,
        session_id=later_session["session_id"],
    )

    target.rollback_conversation_continuity("later-binding-rollback")

    assert target.resolve_session(
        owner_id=owner,
        agent_id="lily",
        surface=changed_surface,
        channel_key=changed_channel,
    )["session_id"] == later_session["session_id"]


def test_moved_source_conversation_retirement_is_archival_and_idempotent(tmp_path):
    store = SessionStore(tmp_path / "retire.sqlite", instance_id="HASHI2")
    session = store.resolve_session(
        owner_id="user:7",
        agent_id="lily",
        surface="workbench",
        channel_key="default",
    )
    _complete(
        store,
        session_id=session["session_id"],
        owner_id="user:7",
        request_id="retire",
        key="retire",
        text="recoverable",
        answer="still stored",
    )

    first = store.archive_agent_conversation_sessions(
        owner_id="user:7",
        agent_id="lily",
        transfer_id="retirement-1",
    )
    second = store.archive_agent_conversation_sessions(
        owner_id="user:7",
        agent_id="lily",
        transfer_id="retirement-1",
    )

    archived = store.get_session(session["session_id"], owner_id="user:7")
    assert archived["status"] == "archived"
    assert archived["is_default"] is False
    assert [
        item["text"]
        for item in store.messages(session["session_id"], owner_id="user:7")
    ] == ["recoverable", "still stored"]
    assert first["session_ids"] == [session["session_id"]]
    assert first["replayed"] is False
    assert second["replayed"] is True
    replacement = store.resolve_session(
        owner_id="user:7",
        agent_id="lily",
        surface="workbench",
        channel_key="default",
    )
    assert replacement["session_id"] != session["session_id"]


def test_assistant_delivery_receipts_are_route_scoped_and_success_only(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")

    first = _complete(
        store,
        session_id=session["session_id"],
        owner_id=owner,
        request_id="req-delivered",
        key="delivery-1",
        text="first prompt",
        answer="last delivered in chat one",
        source="text",
    )
    delivered = store.record_assistant_delivery(
        first.request_id,
        delivered=True,
        surface="telegram",
        channel_key="chat-1",
        transport="telegram",
        completion_path="foreground",
        disposition="transport_delivered",
    )
    duplicate = store.record_assistant_delivery(
        first.request_id,
        delivered=True,
        surface="telegram",
        channel_key="chat-1",
        transport="telegram",
        completion_path="foreground",
        disposition="transport_delivered",
    )

    failed = _complete(
        store,
        session_id=session["session_id"],
        owner_id=owner,
        request_id="req-failed-delivery",
        key="delivery-2",
        text="second prompt",
        answer="newer but not delivered",
        source="text",
    )
    store.record_assistant_delivery(
        failed.request_id,
        delivered=False,
        surface="telegram",
        channel_key="chat-1",
        transport="telegram",
        completion_path="foreground",
        disposition="transport_returned_no_receipt",
    )

    other_route = _complete(
        store,
        session_id=session["session_id"],
        owner_id=owner,
        request_id="req-other-chat",
        key="delivery-3",
        text="third prompt",
        answer="delivered in chat two",
        source="text",
    )
    store.record_assistant_delivery(
        other_route.request_id,
        delivered=True,
        assistant_text="transport presentation override",
        surface="telegram",
        channel_key="chat-2",
        transport="telegram",
        completion_path="background",
        disposition="transport_delivered",
    )

    assert delivered is not None
    assert duplicate is not None
    assert duplicate["event_id"] == delivered["event_id"]
    assert store.latest_delivered_assistant_text(
        session["session_id"], surface="telegram", channel_key="chat-1"
    ) == "last delivered in chat one"
    assert store.latest_delivered_assistant_text(
        session["session_id"], surface="telegram", channel_key="chat-2"
    ) == "transport presentation override"
    assert store.latest_delivered_assistant_text(
        session["session_id"], surface="workbench", channel_key="chat-1"
    ) is None
    assert store.has_assistant_delivery_outcome(
        session["session_id"], surface="telegram", channel_key="chat-1"
    ) is True
    assert store.has_assistant_delivery_outcome(
        session["session_id"], surface="telegram", channel_key="unseen-chat"
    ) is False


def test_delivery_queue_acknowledgement_is_not_a_delivered_receipt(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")
    accepted = _complete(
        store,
        session_id=session["session_id"],
        owner_id=owner,
        request_id="req-hchat-queued",
        key="hchat-queued",
        text="peer prompt",
        answer="peer answer",
        source="protocol:message",
    )

    event = store.record_assistant_delivery(
        accepted.request_id,
        delivered=False,
        outcome_state="queued",
        surface="hchat",
        channel_key="peer@HASHI2",
        transport="hchat",
        completion_path="foreground",
        disposition="cross_instance_enqueued",
    )

    assert event["status"] == "queued"
    assert event["detail"]["outcome_state"] == "queued"
    assert store.latest_delivered_assistant_text(
        session["session_id"], surface="hchat", channel_key="peer@HASHI2"
    ) is None
    assert store.has_assistant_delivery_outcome(
        session["session_id"], surface="hchat", channel_key="peer@HASHI2"
    ) is True

    delivered = store.record_assistant_delivery(
        accepted.request_id,
        delivered=True,
        surface="hchat",
        channel_key="peer@HASHI2",
        transport="hchat",
        completion_path="foreground",
        disposition="terminal_reply_observed",
    )
    assert delivered["status"] == "delivered"
    assert store.latest_delivered_assistant_text(
        session["session_id"], surface="hchat", channel_key="peer@HASHI2"
    ) == "peer answer"


def test_say_delivery_lookup_targets_telegram_when_command_arrives_via_workbench(
    tmp_path,
):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")
    accepted = _complete(
        store,
        session_id=session["session_id"],
        owner_id=owner,
        request_id="req-api-controlled-say",
        key="api-controlled-say",
        text="prompt",
        answer="telegram-delivered answer",
        source="text",
    )
    store.record_assistant_delivery(
        accepted.request_id,
        delivered=True,
        surface="telegram",
        channel_key="99",
        transport="telegram",
        completion_path="foreground",
    )
    runtime = SimpleNamespace(
        name="lily",
        workspace_dir=tmp_path,
        session_store=store,
        global_config=SimpleNamespace(authorized_id=7, instance_id="HASHI1"),
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=99),
        callback_query=None,
        _hashi_session_surface="workbench",
        _hashi_session_channel_key="default",
        _hashi_session_owner_id=None,
        _hashi_session_id=None,
    )

    text, tracking_started = runtime_session.telegram_delivery_state_for_update(
        runtime, update
    )

    assert text == "telegram-delivered answer"
    assert tracking_started is True


def test_default_session_is_permanent_and_channel_bindings_are_isolated(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    default_a = store.ensure_default_session(owner_id=owner, agent_id="lily")
    default_b = store.ensure_default_session(owner_id=owner, agent_id="lily")
    other = store.create_session(owner_id=owner, agent_id="lily", title="Project B")

    assert default_a["session_id"] == default_b["session_id"]
    assert default_a["is_default"] is True

    store.bind_channel(
        owner_id=owner,
        agent_id="lily",
        surface="telegram",
        channel_key="chat-1",
        session_id=other["session_id"],
    )
    assert (
        store.resolve_session(
            owner_id=owner,
            agent_id="lily",
            surface="telegram",
            channel_key="chat-1",
        )["session_id"]
        == other["session_id"]
    )
    assert (
        store.resolve_session(
            owner_id=owner,
            agent_id="lily",
            surface="workbench",
            channel_key="window-2",
        )["session_id"]
        == default_a["session_id"]
    )

    with pytest.raises(SessionConflict):
        store.archive_session(default_a["session_id"])


def test_primary_conversation_adopts_latest_legacy_binding_once(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    default = store.ensure_default_session(owner_id=owner, agent_id="lily")
    telegram = store.create_session(owner_id=owner, agent_id="lily", title="Telegram")
    store.bind_channel(
        owner_id=owner,
        agent_id="lily",
        surface="workbench",
        channel_key="default",
        session_id=default["session_id"],
    )
    store.bind_channel(
        owner_id=owner,
        agent_id="lily",
        surface="telegram",
        channel_key="7",
        session_id=telegram["session_id"],
    )
    store.accept_run(
        session_id=telegram["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="latest-telegram",
        text="latest Telegram turn",
        source="text",
        idempotency_key="latest-telegram",
    )

    selected = store.resolve_primary_session(owner_id=owner, agent_id="lily")
    assert selected["session_id"] == telegram["session_id"]
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM channel_bindings WHERE surface='conversation'"
        ).fetchone()[0] == 0

    established = store.resolve_primary_session(
        owner_id=owner,
        agent_id="lily",
        establish=True,
    )
    assert established["session_id"] == telegram["session_id"]
    store.accept_run(
        session_id=default["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="newer-default",
        text="newer legacy default turn",
        source="api",
        idempotency_key="newer-default",
    )
    assert store.resolve_primary_session(owner_id=owner, agent_id="lily")[
        "session_id"
    ] == telegram["session_id"]


def test_presentation_messages_are_visible_but_never_enter_model_history(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")

    first = store.append_presentation_message(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        role="assistant",
        text="restart completed",
        source="telegram.runtime_notice",
        idempotency_key="reboot:one:final",
        content_format="telegram-html",
    )
    replay = store.append_presentation_message(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        role="assistant",
        text="restart completed",
        source="telegram.runtime_notice",
        idempotency_key="reboot:one:final",
        content_format="telegram-html",
    )

    assert replay["message_id"] == first["message_id"]
    assert first["history_eligible"] is False
    assert store.recent_messages(session["session_id"], owner_id=owner) == []
    visible = store.recent_visible_messages(session["session_id"], owner_id=owner)
    assert [message["text"] for message in visible] == ["restart completed"]
    assert visible[0]["message_context"]["presentation_only"] is True


def test_delivered_frontend_message_uses_shared_primary_and_is_idempotent(tmp_path):
    store = _store(tmp_path)
    runtime = SimpleNamespace(
        name="lily",
        session_store=store,
        global_config=SimpleNamespace(authorized_id=7, instance_id="HASHI1"),
    )
    stale = store.ensure_default_session(owner_id="user:7", agent_id="lily")
    session = store.create_session(
        owner_id="user:7",
        agent_id="lily",
        title="Current shared conversation",
    )
    store.bind_primary_session(
        owner_id="user:7",
        agent_id="lily",
        session_id=session["session_id"],
    )

    first = runtime_session.record_frontend_message(
        runtime,
        role="assistant",
        text="handoff prepared",
        source="telegram.send",
        transport_message_id=42,
        surface="telegram",
        channel_key="7",
        explicit_session_id=stale["session_id"],
    )
    replay = runtime_session.record_frontend_message(
        runtime,
        role="assistant",
        text="handoff prepared",
        source="telegram.send",
        transport_message_id=42,
        surface="telegram",
        channel_key="7",
        explicit_session_id=stale["session_id"],
    )

    assert first is not None and replay is not None
    assert replay["message_id"] == first["message_id"]
    assert store.resolve_primary_session(owner_id="user:7", agent_id="lily")[
        "session_id"
    ] == session["session_id"]
    assert [
        message["text"]
        for message in store.recent_visible_messages(
            session["session_id"], owner_id="user:7"
        )
    ] == ["handoff prepared"]


def test_workzone_slots_are_session_scoped_revisioned_and_snapshot_visible(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    first = store.ensure_default_session(owner_id=owner, agent_id="lily")
    second = store.create_session(owner_id=owner, agent_id="lily", title="Second")
    main = tmp_path / "main"
    attached = tmp_path / "attached"
    main.mkdir()
    attached.mkdir()

    state = store.set_workzone_slot(
        first["session_id"],
        "main",
        path=str(main),
        enabled=True,
        expected_revision=0,
        source="test",
    )
    assert state["revision"] == 1
    assert state["slots"] == [
        {
            "slot_id": "main",
            "path": str(main),
            "enabled": True,
            "label": "",
            "created_at": state["slots"][0]["created_at"],
            "updated_at": state["slots"][0]["updated_at"],
        }
    ]
    assert store.get_session(first["session_id"])["workzone"] == str(main)

    state = store.set_workzone_slot(
        first["session_id"],
        "1",
        path=str(attached),
        enabled=True,
        label="shared",
        expected_revision=1,
        source="test",
    )
    assert state["revision"] == 2
    assert [slot["slot_id"] for slot in state["slots"]] == ["main", "1"]
    assert store.get_workzone_set(second["session_id"])["slots"] == []

    with pytest.raises(SessionConflict, match="stale"):
        store.set_workzone_slot(
            first["session_id"],
            "1",
            enabled=False,
            expected_revision=1,
            source="stale-test",
        )

    state = store.disable_all_workzones(
        first["session_id"], expected_revision=2, source="test"
    )
    assert state["revision"] == 3
    assert all(slot["enabled"] is False for slot in state["slots"])
    assert store.get_session(first["session_id"])["workzone"] is None
    assert store.snapshot(first["session_id"], owner_id=owner)["workzones"] == state


def test_existing_scalar_workzone_is_migrated_to_enabled_main_slot(tmp_path):
    store = _store(tmp_path)
    session = store.ensure_default_session(owner_id="user:7", agent_id="lily")
    legacy = tmp_path / "legacy"
    legacy.mkdir()

    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE sessions SET workzone = ? WHERE session_id = ?",
            (str(legacy), session["session_id"]),
        )
        connection.execute(
            "DELETE FROM session_workzones WHERE session_id = ?",
            (session["session_id"],),
        )

    reloaded = SessionStore(store.db_path, instance_id="HASHI1")
    state = reloaded.get_workzone_set(session["session_id"])

    assert state["revision"] == 0
    assert [(slot["slot_id"], slot["path"], slot["enabled"]) for slot in state["slots"]] == [
        ("main", str(legacy), True)
    ]


def test_workzone_slot_ids_are_limited_to_main_plus_one_through_nine(tmp_path):
    store = _store(tmp_path)
    session = store.ensure_default_session(owner_id="user:7", agent_id="lily")

    with pytest.raises(ValueError, match="main or 1..9"):
        store.set_workzone_slot(session["session_id"], "10", path=str(tmp_path))


def test_ordinary_recent_context_never_crosses_session_or_fresh_generation(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    first = store.ensure_default_session(owner_id=owner, agent_id="lily")
    second = store.create_session(owner_id=owner, agent_id="lily", title="Second")

    _complete(
        store,
        session_id=first["session_id"],
        owner_id=owner,
        request_id="req-first",
        key="key-first",
        text="FIRST SESSION SECRET",
        answer="first answer",
    )
    _complete(
        store,
        session_id=second["session_id"],
        owner_id=owner,
        request_id="req-second",
        key="key-second",
        text="SECOND SESSION SECRET",
        answer="second answer",
    )

    assert [
        row["user_text"] for row in store.recent_exchanges(first["session_id"])
    ] == ["FIRST SESSION SECRET"]
    assert [
        row["user_text"] for row in store.recent_exchanges(second["session_id"])
    ] == ["SECOND SESSION SECRET"]

    fresh = store.start_fresh_generation(first["session_id"])
    assert fresh["context_generation"] == 2
    assert store.recent_exchanges(first["session_id"]) == []
    assert "FIRST SESSION SECRET" in [
        message["text"] for message in store.messages(first["session_id"])
    ]


def test_bridge_recent_exchanges_cross_sessions_but_preserve_owner_agent_scope(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    first = store.create_session(owner_id=owner, agent_id="lily", title="First")
    second = store.create_session(owner_id=owner, agent_id="lily", title="Second")

    for index in range(6):
        _complete(
            store,
            session_id=first["session_id"],
            owner_id=owner,
            request_id=f"req-first-{index}",
            key=f"key-first-{index}",
            text=f"turn-{index}",
            answer=f"answer-{index}",
        )
    store.archive_session(first["session_id"])
    for index in range(6, 12):
        _complete(
            store,
            session_id=second["session_id"],
            owner_id=owner,
            request_id=f"req-second-{index}",
            key=f"key-second-{index}",
            text=f"turn-{index}",
            answer=f"answer-{index}",
        )

    ignored_agent = store.create_session(
        owner_id=owner, agent_id="other-agent", title="Other Agent"
    )
    _complete(
        store,
        session_id=ignored_agent["session_id"],
        owner_id=owner,
        request_id="req-other-agent",
        key="key-other-agent",
        text="OTHER AGENT SECRET",
        answer="other agent answer",
        agent_id="other-agent",
    )
    ignored_owner = store.create_session(
        owner_id="user:8", agent_id="lily", title="Other Owner"
    )
    _complete(
        store,
        session_id=ignored_owner["session_id"],
        owner_id="user:8",
        request_id="req-other-owner",
        key="key-other-owner",
        text="OTHER OWNER SECRET",
        answer="other owner answer",
    )
    _complete(
        store,
        session_id=second["session_id"],
        owner_id=owner,
        request_id="req-handoff-source",
        key="key-handoff-source",
        text="HANDOFF SHOULD NOT RECURSE",
        answer="handoff acknowledgement",
        source="handoff",
    )

    rows = store.recent_agent_exchanges(
        owner_id=owner,
        agent_id="lily",
        limit=10,
        excluded_sources={"handoff"},
    )

    assert [row["user_text"] for row in rows] == [
        f"turn-{index}" for index in range(2, 12)
    ]
    assert {row["session_id"] for row in rows} == {
        first["session_id"],
        second["session_id"],
    }


def test_last_user_activity_comes_from_all_agent_sessions(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    default = store.ensure_default_session(owner_id=owner, agent_id="lily")
    other = store.create_session(owner_id=owner, agent_id="lily", title="Other")

    assert store.last_user_message_at(agent_id="lily", owner_id=owner) is None
    store.accept_run(
        session_id=other["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-activity",
        text="active in another Session",
        source="test",
        idempotency_key="activity-key",
    )

    assert store.last_user_message_at(agent_id="lily", owner_id=owner)
    assert store.last_user_message_at(agent_id="other-agent", owner_id=owner) is None
    assert store.messages(default["session_id"]) == []


def test_accept_run_is_atomic_and_idempotency_is_digest_bound(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")
    first = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-one",
        text="same request",
        source="test",
        idempotency_key="stable-key",
    )
    replay = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-two-never-used",
        text="same request",
        source="test",
        idempotency_key="stable-key",
    )

    assert replay.replayed is True
    assert replay.run_id == first.run_id
    assert replay.request_id == "req-one"
    assert len(store.messages(session["session_id"])) == 1

    with pytest.raises(IdempotencyConflict):
        store.accept_run(
            session_id=session["session_id"],
            owner_id=owner,
            agent_id="lily",
            request_id="req-three",
            text="different request",
            source="test",
            idempotency_key="stable-key",
        )


def test_terminal_commit_writes_message_event_projection_and_outbox_together(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")
    accepted = _complete(
        store,
        session_id=session["session_id"],
        owner_id=owner,
        request_id="req-terminal",
        key="terminal-key",
        text="question",
        answer="answer",
    )

    snapshot = store.snapshot(session["session_id"], owner_id=owner)
    assert [message["role"] for message in snapshot["messages"]] == [
        "user",
        "assistant",
    ]
    assert snapshot["runs"][0]["state"] == "completed"
    assert snapshot["runs"][0]["run_id"] == accepted.run_id
    assert [event["kind"] for event in store.events(session["session_id"])] == [
        "session.created",
        "run.accepted",
        "run.started",
        "run.completed",
    ]
    with sqlite3.connect(store.db_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0]
            == 2
        )


def test_promotion_watermark_is_idempotent_and_archive_is_non_destructive(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.create_session(owner_id=owner, agent_id="lily", title="Temporary")
    _complete(
        store,
        session_id=session["session_id"],
        owner_id=owner,
        request_id="req-promote",
        key="promote-key",
        text="remember all of this",
        answer="retained answer",
    )
    candidate = store.promotion_candidates(agent_id="lily")[0]

    assert store.record_promoted(agent_id="lily", candidate=candidate) is True
    assert store.record_promoted(agent_id="lily", candidate=candidate) is False
    assert store.promotion_candidates(agent_id="lily") == []

    archived = store.archive_session(session["session_id"], deleted=True)
    assert archived["status"] == "deleted"
    assert [message["text"] for message in store.messages(session["session_id"])] == [
        "remember all of this",
        "retained answer",
    ]
    assert store.promotion_status(agent_id="lily")["promoted_count"] == 1


def test_cancel_fences_late_worker_and_is_idempotent(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-cancel",
        text="stop",
        source="test",
        idempotency_key="cancel-key",
    )
    token = store.mark_request_running(accepted.request_id, worker_id="worker")
    stopped = store.cancel_run(accepted.run_id, owner_id=owner)
    assert stopped["state"] == "stopped"
    assert stopped["fencing_token"] == token + 1
    assert store.cancel_run(accepted.run_id, owner_id=owner)["state"] == "stopped"
    assert (
        store.finish_request(
            accepted.request_id,
            success=True,
            assistant_text="late",
            fencing_token=token,
        )["state"]
        == "stopped"
    )


def test_attachment_owner_binding_and_approval_origin_fencing(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")
    body = b"abc"
    staged = store.stage_attachment(
        session_id=session["session_id"],
        owner_id=owner,
        filename="a.txt",
        media_type="text/plain",
        size_bytes=3,
        sha256=hashlib.sha256(body).hexdigest(),
    )
    store.upload_attachment_bytes(
        session_id=session["session_id"],
        owner_id=owner,
        attachment_id=staged["attachment_id"],
        payload=body,
    )
    assert (
        store.commit_attachment(
            session_id=session["session_id"],
            owner_id=owner,
            attachment_id=staged["attachment_id"],
        )["state"]
        == "committed"
    )
    with pytest.raises(SessionNotFound):
        store.commit_attachment(
            session_id=session["session_id"],
            owner_id="user:8",
            attachment_id=staged["attachment_id"],
        )

    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-approval",
        text="approve",
        source="test",
        idempotency_key="approval-key",
    )
    token = store.mark_request_running(accepted.request_id, worker_id="worker")
    approval = store.create_approval(
        run_id=accepted.run_id,
        owner_id=owner,
        fencing_token=token,
        scope={"tool": "write"},
    )
    assert (
        store.decide_approval(
            approval_id=approval["approval_id"], owner_id=owner, decision="approved"
        )["decision"]
        == "approved"
    )

    second = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-expired",
        text="expire",
        source="test",
        idempotency_key="expired-key",
    )
    token2 = store.mark_request_running(second.request_id, worker_id="worker")
    expired = store.create_approval(
        run_id=second.run_id,
        owner_id=owner,
        fencing_token=token2,
        scope={"tool": "write"},
    )
    store.cancel_run(second.run_id, owner_id=owner)
    with pytest.raises(StaleFencingToken):
        store.decide_approval(
            approval_id=expired["approval_id"], owner_id=owner, decision="approved"
        )


def test_backend_bindings_are_keyed_by_session_generation_and_backend(tmp_path):
    store = _store(tmp_path)
    session = store.ensure_default_session(owner_id="user:7", agent_id="lily")
    store.save_backend_binding(
        agent_id="lily",
        session_id=session["session_id"],
        context_generation=1,
        backend_id="codex-cli",
        backend_thread_id="thread-one",
    )

    assert (
        store.backend_binding(
            agent_id="lily",
            session_id=session["session_id"],
            context_generation=1,
            backend_id="codex-cli",
        )
        == "thread-one"
    )
    assert (
        store.backend_binding(
            agent_id="lily",
            session_id=session["session_id"],
            context_generation=2,
            backend_id="codex-cli",
        )
        is None
    )
    assert (
        store.backend_binding(
            agent_id="lily",
            session_id=session["session_id"],
            context_generation=1,
            backend_id="claude-cli",
        )
        is None
    )


def test_event_consumer_replays_until_monotonic_ack(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")
    _complete(
        store,
        session_id=session["session_id"],
        owner_id=owner,
        request_id="req-events",
        key="events-key",
        text="question",
        answer="answer",
    )
    consumer = store.create_event_consumer(
        session_id=session["session_id"], owner_id=owner
    )

    first = store.poll_event_consumer(
        session_id=session["session_id"],
        owner_id=owner,
        consumer_id=consumer["consumer_id"],
    )
    replay = store.poll_event_consumer(
        session_id=session["session_id"],
        owner_id=owner,
        consumer_id=consumer["consumer_id"],
    )
    assert [row["event_id"] for row in replay["events"]] == [
        row["event_id"] for row in first["events"]
    ]

    issued = first["issued_through_sequence"]
    acknowledged = store.acknowledge_event_consumer(
        session_id=session["session_id"],
        owner_id=owner,
        consumer_id=consumer["consumer_id"],
        sequence=issued,
    )
    assert acknowledged["acknowledged_sequence"] == issued
    assert (
        store.poll_event_consumer(
            session_id=session["session_id"],
            owner_id=owner,
            consumer_id=consumer["consumer_id"],
        )["events"]
        == []
    )

    # A stale ACK is idempotent and cannot move the cursor backwards.
    stale = store.acknowledge_event_consumer(
        session_id=session["session_id"],
        owner_id=owner,
        consumer_id=consumer["consumer_id"],
        sequence=max(0, issued - 1),
    )
    assert stale["acknowledged_sequence"] == issued

    with pytest.raises(SessionConflict):
        store.acknowledge_event_consumer(
            session_id=session["session_id"],
            owner_id=owner,
            consumer_id=consumer["consumer_id"],
            sequence=issued + 1,
        )


def test_fresh_is_blocked_until_active_run_is_terminal(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")
    store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-active",
        text="still queued",
        source="test",
        idempotency_key="active-key",
    )

    with pytest.raises(SessionConflict):
        store.start_fresh_generation(session["session_id"])

    store.finish_request(
        "req-active", success=False, error_text="cancelled before execution"
    )
    assert (
        store.start_fresh_generation(session["session_id"])["context_generation"] == 2
    )


def test_restart_reconciliation_terminalizes_queued_and_running_runs_once(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")
    queued = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-queued-restart",
        text="accepted before restart",
        source="test",
        idempotency_key="queued-restart",
    )
    running = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-running-restart",
        text="running during restart",
        source="test",
        idempotency_key="running-restart",
    )
    assert (
        store.mark_request_running(
            running.request_id, worker_id="worker-before-restart"
        )
        == 1
    )

    restarted = _store(tmp_path)
    reconciled = restarted.reconcile_incomplete_runs()

    assert {row["run_id"] for row in reconciled} == {
        queued.run_id,
        running.run_id,
    }
    for accepted, prior_state in ((queued, "queued"), (running, "running")):
        run = restarted.get_run(accepted.run_id, owner_id=owner)
        assert run["state"] == "interrupted"
        assert run["error_code"] == "runtime_restart_interrupted"
        events = restarted.events(session["session_id"], owner_id=owner)
        terminal = [
            event
            for event in events
            if event["run_id"] == accepted.run_id and event["kind"] == "run.interrupted"
        ]
        assert len(terminal) == 1
        assert terminal[0]["detail"]["prior_state"] == prior_state

    assert restarted.reconcile_incomplete_runs() == []


def test_restart_preserves_only_queued_exchange_runs_for_inbox_recovery(
    tmp_path,
):
    store = _store(tmp_path)
    owner = "exchange:authority_1:actor_alice"
    session = store.resolve_session(
        owner_id=owner,
        agent_id="reviewer",
        surface="hchat",
        channel_key="instance_alice:conversation_1",
    )
    queued_exchange = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="reviewer",
        request_id="request_exchange_queued",
        text="accepted before restart",
        source="hchat-exchange",
        idempotency_key="exchange-queued",
    )
    running_exchange = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="reviewer",
        request_id="request_exchange_running",
        text="execution state is unknown",
        source="hchat-exchange",
        idempotency_key="exchange-running",
    )
    ordinary = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="reviewer",
        request_id="request_ordinary_queued",
        text="ordinary queue has no durable inbox",
        source="api",
        idempotency_key="ordinary-queued",
    )
    store.mark_request_running(
        running_exchange.request_id,
        worker_id="worker-before-restart",
    )

    reconciled = store.reconcile_incomplete_runs()

    assert {row["run_id"] for row in reconciled} == {
        running_exchange.run_id,
        ordinary.run_id,
    }
    assert store.get_run(queued_exchange.run_id)["state"] == "queued"
    assert store.get_run(running_exchange.run_id)["state"] == "interrupted"
    assert store.get_run(ordinary.run_id)["state"] == "interrupted"


def test_agent_restart_reconciliation_does_not_interrupt_other_agents(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    alpha_session = store.ensure_default_session(owner_id=owner, agent_id="alpha")
    beta_session = store.ensure_default_session(owner_id=owner, agent_id="beta")
    alpha = store.accept_run(
        session_id=alpha_session["session_id"],
        owner_id=owner,
        agent_id="alpha",
        request_id="req-alpha-restart",
        text="alpha work",
        source="test",
        idempotency_key="alpha-restart",
    )
    beta = store.accept_run(
        session_id=beta_session["session_id"],
        owner_id=owner,
        agent_id="beta",
        request_id="req-beta-still-live",
        text="beta work",
        source="test",
        idempotency_key="beta-still-live",
    )
    store.mark_request_running(alpha.request_id, worker_id="alpha-before-restart")
    store.mark_request_running(beta.request_id, worker_id="beta-still-live")

    reconciled = store.reconcile_incomplete_runs(agent_id="alpha")

    assert [row["run_id"] for row in reconciled] == [alpha.run_id]
    alpha_run = store.get_run(alpha.run_id, owner_id=owner)
    assert alpha_run["state"] == "interrupted"
    assert alpha_run["error_code"] == "runtime_restart_interrupted"
    assert store.get_run(beta.run_id, owner_id=owner)["state"] == "running"
    alpha_events = store.events(alpha_session["session_id"], owner_id=owner)
    terminal = [event for event in alpha_events if event["kind"] == "run.interrupted"]
    assert len(terminal) == 1
    assert terminal[0]["detail"]["agent_id"] == "alpha"
    assert terminal[0]["detail"]["recovery_scope"] == "agent"
