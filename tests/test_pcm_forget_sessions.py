from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from orchestrator.chat_transcript_projection import build_chat_projection
from orchestrator.session_store import SessionConflict, SessionStore
from tests.test_session_store import _complete
from tests.test_live_voice_actions import phone


def stamp(hour: int, minute: int = 0) -> str:
    return datetime(2026, 10, 28, hour, minute, tzinfo=timezone.utc).isoformat()


def test_forget_session_history_does_not_return_from_promotion_or_audit_projection(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    owner, agent = "user:7", "lily"
    first = store.create_session(owner_id=owner, agent_id=agent)
    second = store.create_session(owner_id=owner, agent_id=agent)
    other = store.create_session(owner_id=owner, agent_id="another")

    def complete(session, label, when, who=agent):
        monkeypatch.setattr("orchestrator.session_store._utc_now", lambda: when)
        return _complete(
            store, session_id=session["session_id"], owner_id=owner, agent_id=who,
            request_id=label, key=label, text=f"{label} request", answer=f"{label} answer",
        )

    complete(first, "old", stamp(8))
    bad = complete(first, "forget-first", stamp(9, 5))
    with store._lock, store._connection() as connection:
        connection.execute(
            """INSERT OR REPLACE INTO run_projection_records
               (run_id, session_id, projection_json, updated_at) VALUES (?, ?, ?, ?)""",
            (bad.run_id, first["session_id"], json.dumps({"sentinel": "forget-first secret"}), stamp(9, 6)),
        )
    complete(second, "forget-second", stamp(10))
    complete(second, "new", stamp(11, 5))
    complete(other, "other-agent", stamp(9, 30), "another")

    workspace = store.session_workspace(first["session_id"], 1)
    transcript = workspace / "transcript.jsonl"
    entries = [
        {"role": "user", "text": "old request", "source": "test", "ts": stamp(8)},
        {"role": "assistant", "text": "old answer", "source": "test", "ts": stamp(8)},
        {"role": "user", "text": "forget-first request", "source": "test", "ts": stamp(9, 5)},
        {"role": "assistant", "text": "forget-first answer", "source": "test", "ts": stamp(9, 5)},
    ]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in entries), encoding="utf-8")
    old_generation = store.get_session(first["session_id"])["history_generation"]
    monkeypatch.setattr("orchestrator.session_store._utc_now", lambda: stamp(12))

    result = store.forget_pcm_window(
        owner_id=owner, agent_id=agent,
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
    )

    assert result["messages"] == 4
    assert result["runs"] == 2
    assert store.get_session(first["session_id"])["context_generation"] == 2
    assert store.get_session(second["session_id"])["context_generation"] == 2
    assert store.get_session(other["session_id"])["context_generation"] == 1
    assert store.get_session(first["session_id"])["history_generation"] > old_generation
    page = store.agent_history_page(owner_id=owner, agent_id=agent)
    assert {row["text"] for row in page["messages"]} == {
        "old request", "old answer", "new request", "new answer",
    }
    assert "forget-first" not in json.dumps(store.messages(first["session_id"], owner_id=owner))
    assert {row["user_text"] for row in store.recent_agent_exchanges(owner_id=owner, agent_id=agent)} == {
        "old request", "new request",
    }
    assert {row["user_text"] for row in store.promotion_candidates(agent_id=agent)} == {
        "old request", "new request",
    }
    projection = build_chat_projection(
        store, session=store.get_session(first["session_id"]), owner_id=owner,
        known_history_generation=old_generation,
    )
    assert projection["history_reset"]
    assert "old request" in json.dumps(projection)
    assert "forget-first" not in json.dumps(projection)
    assert "forget-first secret" not in json.dumps(store.snapshot(first["session_id"], owner_id=owner))
    assert "forget-first request" in transcript.read_text(encoding="utf-8")
    assert "other-agent request" in json.dumps(store.messages(other["session_id"], owner_id=owner))
    with store._connection() as connection:
        audit = connection.execute(
            "SELECT payload_json FROM pcm_forget_audit WHERE source_kind='message' AND source_id=?",
            (bad.message_id,),
        ).fetchone()
    assert audit is not None and "forget-first request" in audit[0]


def test_forget_preserves_prior_history_without_crossing_an_older_fresh_boundary(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    session = store.create_session(owner_id="user:7", agent_id="lily")
    monkeypatch.setattr("orchestrator.session_store._utc_now", lambda: stamp(7))
    _complete(store, session_id=session["session_id"], owner_id="user:7", agent_id="lily",
              request_id="before-fresh", key="before-fresh", text="before fresh", answer="before reply")
    store.start_fresh_generation(session["session_id"], reason="user_fresh")
    monkeypatch.setattr("orchestrator.session_store._utc_now", lambda: stamp(8))
    _complete(store, session_id=session["session_id"], owner_id="user:7", agent_id="lily",
              request_id="retained", key="retained", text="retained history", answer="retained reply")
    monkeypatch.setattr("orchestrator.session_store._utc_now", lambda: stamp(10))
    _complete(store, session_id=session["session_id"], owner_id="user:7", agent_id="lily",
              request_id="forgotten", key="forgotten", text="forgotten history", answer="forgotten reply")
    store.forget_pcm_window(
        owner_id="user:7", agent_id="lily",
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
    )
    messages = store.recent_messages(session["session_id"], owner_id="user:7")
    assert [row["text"] for row in messages] == ["retained history", "retained reply"]
    exchanges = store.recent_exchanges(session["session_id"], limit=10)
    assert [row["user_text"] for row in exchanges] == ["retained history"]
    for reader in (
        store.recent_history_messages,
        store.recent_visible_messages,
        store.visible_messages_after,
    ):
        rows = reader(session["session_id"], owner_id="user:7")
        assert [row["text"] for row in rows] == ["retained history", "retained reply"]

    store.start_fresh_generation(session["session_id"], reason="user_fresh")
    assert store.recent_messages(session["session_id"], owner_id="user:7") == []
    assert store.recent_history_messages(session["session_id"], owner_id="user:7") == []
    assert store.recent_visible_messages(session["session_id"], owner_id="user:7") == []
    assert store.recent_exchanges(session["session_id"]) == []


def test_generated_title_and_command_receipt_do_not_reintroduce_forgotten_text(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    session = store.create_session(owner_id="user:7", agent_id="lily")
    monkeypatch.setattr("orchestrator.session_store._utc_now", lambda: stamp(9, 30))
    _complete(store, session_id=session["session_id"], owner_id="user:7", agent_id="lily",
              request_id="old", key="old", text="wrong idea in title", answer="wrong answer")
    assert "wrong idea" in store.get_session(session["session_id"])["title"]
    with store._lock, store._connection() as connection:
        connection.execute(
            """INSERT INTO frontend_command_invocations
               (session_id, owner_id, client_id, request_id, request_digest, invocation_json,
                state, response_json, created_at, updated_at)
               VALUES (?, 'user:7', 'client', 'request', ?, '{}', 'completed', ?, ?, ?)""",
            (session["session_id"], "a" * 64, json.dumps({"text": "wrong idea receipt"}), stamp(9, 30), stamp(9, 30)),
        )
        connection.execute(
            """INSERT INTO run_events(event_id, session_id, sequence, kind, summary, detail_json, created_at)
               VALUES ('command-event', ?, 999, 'frontend.command_result', 'wrong idea event', ?, ?)""",
            (session["session_id"], json.dumps({"message": "wrong idea event"}), stamp(9, 30)),
        )
    store.forget_pcm_window(
        owner_id="user:7", agent_id="lily",
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
    )
    assert "wrong idea" not in store.get_session(session["session_id"])["title"]
    assert "wrong idea" not in json.dumps(store.events(session["session_id"], owner_id="user:7"))
    with store._connection() as connection:
        response = connection.execute(
            "SELECT response_json FROM frontend_command_invocations WHERE request_id='request'"
        ).fetchone()[0]
        audit = connection.execute(
            "SELECT payload_json FROM pcm_forget_audit WHERE source_kind='command_invocation'"
        ).fetchall()
    assert "wrong idea" not in str(response)
    assert any("wrong idea receipt" in row[0] for row in audit)


def test_forget_refuses_to_change_history_while_another_run_is_active(tmp_path):
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    session = store.create_session(owner_id="user:7", agent_id="lily")
    store.accept_run(
        session_id=session["session_id"], owner_id="user:7", agent_id="lily",
        request_id="active", text="still working", source="test", idempotency_key="active",
    )
    with pytest.raises(SessionConflict):
        store.forget_pcm_window(
            owner_id="user:7", agent_id="lily",
            start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
        )
    assert store.messages(session["session_id"], owner_id="user:7")[0]["text"] == "still working"


def test_incomplete_forget_blocks_new_work_for_only_that_agent_until_all_steps_finish(tmp_path):
    path = tmp_path / "state" / "sessions.sqlite3"
    store = SessionStore(path, instance_id="HASHI3")
    session = store.create_session(owner_id="user:7", agent_id="lily")
    other = store.create_session(owner_id="user:7", agent_id="other")
    result = store.forget_pcm_window(
        owner_id="user:7", agent_id="lily", operation_id="forget-test-1",
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
    )
    assert result["operation_id"] == "forget-test-1"
    reopened = SessionStore(path, instance_id="HASHI3")
    with pytest.raises(SessionConflict, match="forget"):
        reopened.accept_run(
            session_id=session["session_id"], owner_id="user:7", agent_id="lily",
            request_id="blocked", text="new", source="test", idempotency_key="blocked",
        )
    allowed = reopened.accept_run(
        session_id=other["session_id"], owner_id="user:7", agent_id="other",
        request_id="other-ok", text="other", source="test", idempotency_key="other-ok",
    )
    assert allowed.request_id == "other-ok"
    with pytest.raises(SessionConflict, match="incomplete"):
        reopened.complete_pcm_forget(owner_id="user:7", agent_id="lily", operation_id="forget-test-1")
    for step in ("local_memory", "continuity"):
        reopened.record_pcm_forget_step(
            owner_id="user:7", agent_id="lily", operation_id="forget-test-1", step=step,
        )
    with pytest.raises(SessionConflict, match="incomplete"):
        reopened.complete_pcm_forget(owner_id="user:7", agent_id="lily", operation_id="forget-test-1")
    new_session = reopened.create_session(owner_id="user:7", agent_id="lily")
    reopened.record_pcm_forget_new_session(
        owner_id="user:7", agent_id="lily", operation_id="forget-test-1",
        session_id=new_session["session_id"],
    )
    reopened.complete_pcm_forget(owner_id="user:7", agent_id="lily", operation_id="forget-test-1")
    allowed = reopened.accept_run(
        session_id=session["session_id"], owner_id="user:7", agent_id="lily",
        request_id="after", text="new", source="test", idempotency_key="after",
    )
    assert allowed.request_id == "after"



@pytest.mark.asyncio
async def test_forget_preserves_earlier_phone_history(phone):
    from orchestrator.frontend_live_voice.protocol import Fragment

    await phone.manager.append_fragment_once(
        phone.binding, Fragment("old-phone", "user", "retained phone idea", 0, 100)
    )
    with phone.store._lock, phone.store._connection() as connection:
        connection.execute(
            "UPDATE live_calls SET phase='ended', started_at=?, ended_at=? WHERE call_id=?",
            (stamp(8), stamp(8, 5), phone.call_id),
        )

    result = phone.store.forget_pcm_window(
        owner_id=phone.owner_id, agent_id=phone.agent_id,
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
    )

    assert result["phone_calls"] == 0
    segments = phone.store.live_transcript_segments(phone.session_id, owner_id=phone.owner_id)
    assert [segment["text"] for segment in segments] == ["retained phone idea"]
    history = phone.store.recent_history_messages(phone.session_id, owner_id=phone.owner_id)
    assert [item["text"] for item in history] == ["retained phone idea"]


@pytest.mark.asyncio
async def test_forget_removes_phone_transcripts_from_history_and_handoff(phone):
    from orchestrator.frontend_live_voice.protocol import Fragment
    from orchestrator.phone_context_handoff import _fragments

    await phone.manager.append_fragment_once(
        phone.binding, Fragment("phone-1", "user", "forgotten phone idea", 0, 100)
    )
    await phone.manager.append_fragment_once(
        phone.binding, Fragment("phone-2", "assistant", "forgotten phone answer", 120, 200)
    )
    with phone.store._lock, phone.store._connection() as connection:
        connection.execute(
            "UPDATE live_calls SET phase='ended', started_at=?, ended_at=? WHERE call_id=?",
            (stamp(9), stamp(10), phone.call_id),
        )
    assert phone.store.live_transcript_segments(phone.session_id, owner_id=phone.owner_id)

    result = phone.store.forget_pcm_window(
        owner_id=phone.owner_id, agent_id=phone.agent_id,
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
    )

    assert result["phone_calls"] == 1
    assert phone.store.live_transcript_segments(phone.session_id, owner_id=phone.owner_id) == []
    with phone.store._lock, phone.store._connection() as connection:
        rows = _fragments(
            connection, owner_id=phone.owner_id, agent_id=phone.agent_id,
            session_id=phone.session_id, context_generation=1, ended_only=True,
        )
        audit = connection.execute(
            "SELECT payload_json FROM pcm_forget_audit WHERE source_kind='live_event'"
        ).fetchall()
    assert rows == []
    assert any("forgotten phone idea" in item[0] for item in audit)


def test_forget_and_phone_reservation_exclude_each_other(tmp_path):
    import sqlite3

    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    session = store.create_session(owner_id="user:7", agent_id="lily")

    def reserve(attempt):
        with store._lock, store._connection() as connection:
            connection.execute(
                """INSERT INTO live_call_attempts (
                   attempt_id, owner_id, session_id, agent_id, instance_id,
                   instance_generation, context_generation, request_digest, created_at, updated_at
                ) VALUES (?, 'user:7', ?, 'lily', 'HASHI3', 'gen', 1, 'digest', ?, ?)""",
                (attempt, session["session_id"], stamp(9), stamp(9)),
            )

    reserve("in-flight-phone")
    with pytest.raises(SessionConflict, match="Phone"):
        store.forget_pcm_window(
            owner_id="user:7", agent_id="lily",
            start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
        )
    with store._lock, store._connection() as connection:
        connection.execute("UPDATE live_call_attempts SET state='cancelled'")

    result = store.forget_pcm_window(
        owner_id="user:7", agent_id="lily",
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
    )
    with pytest.raises(sqlite3.IntegrityError, match="pcm_forget_in_progress"):
        reserve("must-not-start")
    assert result["operation_id"]


def test_stale_transfer_cannot_reimport_forgotten_history(tmp_path, monkeypatch):
    source = SessionStore(tmp_path / "source.sqlite3", instance_id="HASHI2")
    target = SessionStore(tmp_path / "target.sqlite3", instance_id="HASHI3")
    owner, agent = "user:7", "lily"
    source_session = source.create_session(owner_id=owner, agent_id=agent)
    target.create_session(owner_id=owner, agent_id=agent)
    monkeypatch.setattr("orchestrator.session_store._utc_now", lambda: stamp(9, 5))
    _complete(source, session_id=source_session["session_id"], owner_id=owner,
              agent_id=agent, request_id="old", key="old", text="bad idea", answer="bad answer")
    capsule = source.export_conversation_continuity(
        owner_id=owner, agent_id=agent, source_instance="HASHI2",
        transfer_id="transfer-old", history_mode="move",
    )
    result = target.forget_pcm_window(
        owner_id=owner, agent_id=agent,
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
    )
    for step in ("local_memory", "continuity"):
        target.record_pcm_forget_step(
            owner_id=owner, agent_id=agent, operation_id=result["operation_id"], step=step,
        )
    new_session = target.create_session(owner_id=owner, agent_id=agent)
    target.record_pcm_forget_new_session(
        owner_id=owner, agent_id=agent, operation_id=result["operation_id"],
        session_id=new_session["session_id"],
    )
    target.complete_pcm_forget(owner_id=owner, agent_id=agent, operation_id=result["operation_id"])
    with pytest.raises(SessionConflict, match="forgotten"):
        target.import_conversation_continuity(
            capsule, owner_id=owner, agent_id=agent, transfer_id="transfer-old",
            history_mode="move",
        )
    assert "bad idea" not in json.dumps(target.agent_history_page(owner_id=owner, agent_id=agent))


def test_forget_accepts_historical_owner_sessions(tmp_path):
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3")
    store.create_session(owner_id="user:7", agent_id="lily")
    store.create_session(owner_id="user:8", agent_id="lily")
    result = store.forget_pcm_window(
        owner_id="user:7", agent_id="lily",
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)),
    )
    assert result["sessions"] == 2
