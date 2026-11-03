"""Durable attachment-only presentation collection, independent of chat views."""

import hashlib
import json
import sqlite3

import pytest

from orchestrator.session_store import SessionStore


def store_at(tmp_path):
    return SessionStore(tmp_path / "sessions.sqlite3", instance_id="CATALOGUE-TEST")


def publish(
    store, owner="user:7", agent="alice", name="paper.txt", text="source", key="one"
):
    session = store.ensure_default_session(owner_id=owner, agent_id=agent)
    payload = b"synthetic file " + key.encode()
    part = store.stage_attachment(
        session_id=session["session_id"],
        owner_id=owner,
        filename=name,
        media_type="text/plain",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    store.upload_attachment_bytes(
        session_id=session["session_id"],
        owner_id=owner,
        attachment_id=part["attachment_id"],
        payload=payload,
    )
    store.commit_attachment(
        session_id=session["session_id"],
        owner_id=owner,
        attachment_id=part["attachment_id"],
    )
    message = store.append_presentation_message(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id=agent,
        role="assistant",
        text=text,
        source="test",
        idempotency_key=key,
        content=[{"type": "attachment", "attachment_id": part["attachment_id"]}],
        history_eligible=True,
    )
    return session, message, part


def test_catalogue_pages_are_owner_agent_scoped_and_only_formal_visible_attachments(
    tmp_path,
):
    store = store_at(tmp_path)
    session, message, _ = publish(store)
    publish(store, owner="user:other", key="other-owner")
    publish(store, agent="bob", key="other-agent")
    store.append_presentation_message(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="alice",
        role="assistant",
        text="no file",
        source="test",
        idempotency_key="text",
    )
    page = store.attachment_catalogue_page(
        owner_id="user:7", agent_ids=["alice"], after=0, limit=1
    )
    assert len(page["messages"]) == 1
    assert page["messages"][0]["message_id"] == message["message_id"]
    assert page["messages"][0]["agent_id"] == "alice"
    assert page["complete"] is True


def test_catalogue_cursor_observes_late_old_dated_imports_and_survives_restart(
    tmp_path,
):
    store = store_at(tmp_path)
    publish(store, key="first")
    first = store.attachment_catalogue_page(
        owner_id="user:7", agent_ids=["alice"], after=0
    )
    _, message, _ = publish(store, key="imported")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE messages SET created_at='2000-01-01T00:00:00+00:00' WHERE message_id=?",
            (message["message_id"],),
        )
    reopened = store_at(tmp_path)
    second = reopened.attachment_catalogue_page(
        owner_id="user:7", agent_ids=["alice"], after=first["next_after"]
    )
    assert [row["message_id"] for row in second["messages"]] == [message["message_id"]]
    assert second["messages"][0]["created_at"].startswith("2000-")
    assert (
        reopened.attachment_catalogue_page(
            owner_id="user:7", agent_ids=["alice"], after=second["next_after"]
        )["messages"]
        == []
    )


def test_catalogue_detects_visible_promotion_and_does_not_reuse_sequence_after_delete(
    tmp_path,
):
    store = store_at(tmp_path)
    _, first, _ = publish(store, key="first")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE messages SET visibility='internal' WHERE message_id=?",
            (first["message_id"],),
        )
    empty = store.attachment_catalogue_page(owner_id="user:7", agent_ids=["alice"])
    assert empty["messages"] == []
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE messages SET visibility='visible' WHERE message_id=?",
            (first["message_id"],),
        )
    visible = store.attachment_catalogue_page(
        owner_id="user:7", agent_ids=["alice"], after=empty["next_after"]
    )
    assert len(visible["messages"]) == 1
    _, second, _ = publish(store, key="second")
    next_page = store.attachment_catalogue_page(
        owner_id="user:7", agent_ids=["alice"], after=visible["next_after"]
    )
    assert next_page["next_after"] > visible["next_after"]
    assert next_page["messages"][-1]["message_id"] == second["message_id"]


def test_catalogue_schema_backfills_existing_files_once_without_requeueing_on_each_open(
    tmp_path,
):
    store = store_at(tmp_path)
    _, message, _ = publish(store)
    with sqlite3.connect(store.db_path) as connection:
        for trigger in ["attachment_catalogue_insert", "attachment_catalogue_update"]:
            connection.execute("DROP TRIGGER " + trigger)
        connection.execute("DROP TABLE attachment_catalogue_entries")
        connection.execute(
            "UPDATE schema_metadata SET value='31' WHERE key='schema_version'"
        )
    reopened = store_at(tmp_path)
    first = reopened.attachment_catalogue_page(owner_id="user:7", agent_ids=["alice"])
    assert first["messages"][0]["message_id"] == message["message_id"]
    again = store_at(tmp_path).attachment_catalogue_page(
        owner_id="user:7", agent_ids=["alice"], after=first["next_after"]
    )
    assert again["messages"] == []
    assert again["next_after"] == first["next_after"]


@pytest.mark.asyncio
async def test_catalogue_api_signs_scope_cursor_and_never_exposes_paths_or_inline_bytes(
    tmp_path,
):
    from tests.test_session_api import _server, _Request

    server, _ = _server(tmp_path)
    session, _, _ = publish(server.session_store, agent="lily")
    response = await server.handle_v1_attachment_catalogue(
        _Request(query={"limit": "1"})
    )
    assert response.status == 200
    body = json.loads(response.text)
    assert len(body["messages"]) == 1
    assert body["messages"][0]["attachments"][0]["filename"] == "paper.txt"
    assert body["next_cursor"]
    assert "local_ref" not in response.text and str(tmp_path) not in response.text
    assert "synthetic file" not in response.text
    again = await server.handle_v1_attachment_catalogue(
        _Request(query={"cursor": body["next_cursor"]})
    )
    assert json.loads(again.text)["messages"] == []
    tampered = body["next_cursor"][:-1] + (
        "A" if body["next_cursor"][-1] != "A" else "B"
    )
    rejected = await server.handle_v1_attachment_catalogue(
        _Request(query={"cursor": tampered})
    )
    assert rejected.status == 400


@pytest.mark.asyncio
async def test_source_jump_rechecks_owner_agent_and_exact_attachment_without_selecting_a_session(
    tmp_path,
):
    from tests.test_session_api import _server, _Request

    server, _ = _server(tmp_path)
    session, message, part = publish(server.session_store, agent="lily")
    query = {
        "session_id": session["session_id"],
        "message_id": message["message_id"],
        "attachment_id": part["attachment_id"],
    }
    before = server.session_store.ensure_default_session(
        owner_id="user:7", agent_id="lily"
    )
    response = await server.handle_v1_attachment_catalogue_source(_Request(query=query))
    assert response.status == 200
    data = json.loads(response.text)
    assert data["target_message_id"] == message["message_id"]
    assert any(row["message_id"] == message["message_id"] for row in data["messages"])
    assert "local_ref" not in response.text and str(tmp_path) not in response.text
    after = server.session_store.ensure_default_session(
        owner_id="user:7", agent_id="lily"
    )
    assert after["session_id"] == before["session_id"]
    rejected = await server.handle_v1_attachment_catalogue_source(
        _Request(query={**query, "attachment_id": "unrelated"})
    )
    assert rejected.status == 404
    foreign_session, foreign, foreign_part = publish(
        server.session_store, owner="user:other", agent="lily", key="foreign"
    )
    rejected = await server.handle_v1_attachment_catalogue_source(
        _Request(
            query={
                "session_id": foreign_session["session_id"],
                "message_id": foreign["message_id"],
                "attachment_id": foreign_part["attachment_id"],
            }
        )
    )
    assert rejected.status == 404
