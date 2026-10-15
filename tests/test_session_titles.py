from __future__ import annotations

import pytest

from orchestrator.session_store import SessionConflict, SessionStore
from orchestrator.session_titles import automatic_session_title, is_system_session_title


def _store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("请帮我整理 Workbench 会话列表。后面还有细节", "整理 Workbench 会话列表"),
        (
            "Please help me investigate why the backend selector cannot open HASHI3 today",
            "investigate why the backend selector cannot open HASHI3…",
        ),
        ("# Archive and restore sessions\nkeep the raw history", "Archive and restore sessions"),
    ],
)
def test_automatic_session_title_is_short_and_deterministic(message, expected):
    assert automatic_session_title(message) == expected


def test_system_title_detection_covers_existing_placeholders():
    assert is_system_session_title("New session")
    assert is_system_session_title("agent1 default")
    assert is_system_session_title("新建对话")
    assert not is_system_session_title("Workbench 会话整理")


def test_first_accepted_user_turn_titles_an_untouched_session_once(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="lily")

    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="auto-title",
        text="internal transport text",
        display_text="请帮我整理 Workbench 会话列表。后面还有细节",
        source="workbench",
        idempotency_key="auto-title",
    )

    titled = store.get_session(session["session_id"], owner_id=owner)
    assert titled["title"] == "整理 Workbench 会话列表"
    assert titled["title_source"] == "maintenance"
    assert titled["revision"] == session["revision"] + 1
    title_events = [
        event
        for event in store.events(session["session_id"], owner_id=owner)
        if event["kind"] == "session.title_updated"
    ]
    assert len(title_events) == 1
    assert title_events[0]["detail"] == {"source": "maintenance"}

    replay = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="auto-title",
        text="internal transport text",
        display_text="请帮我整理 Workbench 会话列表。后面还有细节",
        source="workbench",
        idempotency_key="auto-title",
    )
    assert replay.run_id == accepted.run_id
    assert replay.message_id == accepted.message_id
    assert replay.replayed is True
    assert store.get_session(session["session_id"], owner_id=owner)["revision"] == titled["revision"]


def test_manual_or_explicit_title_is_never_overwritten(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.create_session(owner_id=owner, agent_id="lily", title="Customer plan")
    assert session["title_source"] == "user"

    store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="keep-title",
        text="Please rename this from my first turn",
        source="workbench",
        idempotency_key="keep-title",
    )

    unchanged = store.get_session(session["session_id"], owner_id=owner)
    assert unchanged["title"] == "Customer plan"
    assert unchanged["title_source"] == "user"


def test_listing_backfills_legacy_placeholder_without_changing_activity_date(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.create_session(
        owner_id=owner,
        agent_id="lily",
        title="Temporary migration fixture",
    )
    store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="legacy-title",
        text="Review the HASHI3 backend selection and Session archive flow",
        source="workbench",
        idempotency_key="legacy-title",
    )
    legacy_updated_at = "2026-10-01T01:02:03.000000Z"
    with store._lock, store._connection() as connection:
        connection.execute(
            """
            UPDATE sessions SET title='New conversation', title_source='system',
                updated_at=? WHERE session_id=?
            """,
            (legacy_updated_at, session["session_id"]),
        )

    listed = store.list_sessions(owner_id=owner, agent_id="lily")
    migrated = next(row for row in listed if row["session_id"] == session["session_id"])
    assert migrated["title"] == "Review the HASHI3 backend selection and Session archive…"
    assert migrated["title_source"] == "maintenance"
    assert migrated["updated_at"] == legacy_updated_at
    title_events = [
        event
        for event in store.events(session["session_id"], owner_id=owner)
        if event["kind"] == "session.title_updated"
    ]
    assert title_events[-1]["detail"] == {
        "source": "maintenance",
        "reason": "placeholder_backfill",
    }

    repeated = store.list_sessions(owner_id=owner, agent_id="lily")
    assert next(row for row in repeated if row["session_id"] == session["session_id"])[
        "revision"
    ] == migrated["revision"]


def test_archive_and_restore_preserve_messages_and_use_revision_cas(tmp_path):
    store = _store(tmp_path)
    owner = "user:7"
    session = store.create_session(owner_id=owner, agent_id="lily")
    store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="archive-me",
        text="Keep this message",
        source="workbench",
        idempotency_key="archive-me",
    )
    current = store.get_session(session["session_id"], owner_id=owner)

    archived = store.update_session(
        session["session_id"],
        owner_id=owner,
        expected_revision=current["revision"],
        status="archived",
    )
    assert archived["status"] == "archived"
    assert all(
        row["session_id"] != session["session_id"]
        for row in store.list_sessions(owner_id=owner, agent_id="lily")
    )
    assert any(
        row["session_id"] == session["session_id"]
        for row in store.list_sessions(
            owner_id=owner,
            agent_id="lily",
            include_archived=True,
        )
    )
    assert [message["text"] for message in store.messages(session["session_id"], owner_id=owner)] == [
        "Keep this message"
    ]

    restored = store.update_session(
        session["session_id"],
        owner_id=owner,
        expected_revision=archived["revision"],
        status="active",
    )
    assert restored["status"] == "active"
    assert [event["kind"] for event in store.events(session["session_id"], owner_id=owner)][-2:] == [
        "session.archived",
        "session.restored",
    ]

    with pytest.raises(SessionConflict, match="revision"):
        store.update_session(
            session["session_id"],
            owner_id=owner,
            expected_revision=current["revision"],
            status="archived",
        )


def test_permanent_default_session_cannot_be_archived_by_patch(tmp_path):
    store = _store(tmp_path)
    session = store.ensure_default_session(owner_id="user:7", agent_id="lily")

    with pytest.raises(SessionConflict, match="permanent default"):
        store.update_session(
            session["session_id"],
            owner_id="user:7",
            expected_revision=session["revision"],
            status="archived",
        )
