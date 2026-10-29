from datetime import datetime, timedelta, timezone

import sqlite3

import pytest

from orchestrator.session_store import SessionConflict, SessionStore
from tests.test_session_store import _complete


def stamp(hour):
    return datetime(2026, 10, 28, hour, tzinfo=timezone.utc).isoformat()


def add(store, monkeypatch, session, owner, agent, label, hour):
    monkeypatch.setattr("orchestrator.session_store._utc_now", lambda: stamp(hour))
    return _complete(store, session_id=session["session_id"], owner_id=owner, agent_id=agent,
                     request_id=label, key=label, text=label + " request", answer=label + " answer")


def test_forget_spans_agent_sessions_and_preserves_older_and_other_agents(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    current = store.create_session(owner_id="user:7", agent_id="lily")
    legacy = store.create_session(owner_id="exchange:old", agent_id="lily")
    other_user = store.create_session(owner_id="user:8", agent_id="lily")
    other_agent = store.create_session(owner_id="user:7", agent_id="other")
    add(store, monkeypatch, current, "user:7", "lily", "old-current", 8)
    add(store, monkeypatch, current, "user:7", "lily", "bad-current", 10)
    add(store, monkeypatch, legacy, "exchange:old", "lily", "old-exchange", 8)
    add(store, monkeypatch, other_user, "user:8", "lily", "bad-other", 10)
    add(store, monkeypatch, other_agent, "user:7", "other", "keep-other-agent", 10)
    result = store.forget_pcm_window(owner_id="user:7", agent_id="lily",
        start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)))
    assert result["messages"] == 4
    for session, owner, keep, remove in (
        (current, "user:7", "old-current", "bad-current"),
        (legacy, "exchange:old", "old-exchange", "bad-current"),
        (other_user, "user:8", "", "bad-other"),
        (other_agent, "user:7", "keep-other-agent", "bad-current"),
    ):
        text = str(store.messages(session["session_id"], owner_id=owner))
        assert keep in text
        assert remove not in text
    assert store.get_session(other_user["session_id"])["context_generation"] == 2
    assert store.get_session(other_agent["session_id"])["context_generation"] == 1
    with store._connection() as connection:
        owners = {row[0] for row in connection.execute(
            "SELECT owner_id FROM pcm_forget_windows WHERE agent_id='lily'")}
    assert owners == {"user:7", "user:8", "exchange:old"}


def test_preview_detects_changes_in_another_agent_session(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    current = store.create_session(owner_id="user:7", agent_id="lily")
    other = store.create_session(owner_id="exchange:old", agent_id="lily")
    now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    preview = store.preview_pcm_forget(owner_id="user:7", agent_id="lily",
        session_id=current["session_id"], duration=timedelta(hours=2), now=now)
    add(store, monkeypatch, other, "exchange:old", "lily", "new-other", 10)
    with pytest.raises(SessionConflict, match="changed"):
        store.confirm_pcm_forget(owner_id="user:7", agent_id="lily",
            session_id=current["session_id"], token=preview["token"], now=now)


def test_pending_forget_fences_other_owner_and_active_runs(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    current = store.create_session(owner_id="user:7", agent_id="lily")
    other = store.create_session(owner_id="user:8", agent_id="lily")
    run = store.accept_run(session_id=other["session_id"], owner_id="user:8", agent_id="lily",
                           request_id="active", text="working", source="test", idempotency_key="active")
    window = dict(start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)))
    with pytest.raises(SessionConflict, match="active"):
        store.forget_pcm_window(owner_id="user:7", agent_id="lily", **window)
    store.finish_request(run.request_id, success=False, error_text="stopped")
    store.forget_pcm_window(owner_id="user:7", agent_id="lily", **window)
    with pytest.raises(SessionConflict, match="forget"):
        store.accept_run(session_id=other["session_id"], owner_id="user:8", agent_id="lily",
                         request_id="blocked", text="working", source="test", idempotency_key="blocked")


def test_other_owner_phone_is_fenced_before_and_after_forget(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    store.create_session(owner_id="user:7", agent_id="lily")
    other = store.create_session(owner_id="user:8", agent_id="lily")

    def reserve(attempt):
        with store._lock, store._connection() as connection:
            connection.execute(
                """INSERT INTO live_call_attempts (
                    attempt_id, owner_id, session_id, agent_id, instance_id,
                    instance_generation, context_generation, request_digest, created_at, updated_at
                ) VALUES (?, 'user:8', ?, 'lily', 'HASHI3', 'gen', 1, 'digest', ?, ?)""",
                (attempt, other["session_id"], stamp(9), stamp(9)),
            )

    reserve("ongoing")
    window = dict(start=datetime.fromisoformat(stamp(9)), end=datetime.fromisoformat(stamp(11)))
    with pytest.raises(SessionConflict, match="Phone"):
        store.forget_pcm_window(owner_id="user:7", agent_id="lily", **window)
    with store._lock, store._connection() as connection:
        connection.execute("UPDATE live_call_attempts SET state='cancelled'")
    store.forget_pcm_window(owner_id="user:7", agent_id="lily", **window)
    with pytest.raises(sqlite3.IntegrityError, match="pcm_forget_in_progress"):
        reserve("after-pending")
