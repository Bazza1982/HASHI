from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.session_store import SessionConflict, SessionStore
from tests.test_session_store import _complete


NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


def test_preview_needs_matching_owner_agent_session_and_expires(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    session = store.create_session(owner_id="user:7", agent_id="lily")
    preview = store.preview_pcm_forget(
        owner_id="user:7", agent_id="lily", session_id=session["session_id"],
        duration=timedelta(hours=3), now=NOW,
    )
    token = preview["token"]
    with pytest.raises(SessionConflict):
        store.confirm_pcm_forget(
            owner_id="user:8", agent_id="lily", session_id=session["session_id"], token=token, now=NOW,
        )
    with pytest.raises(SessionConflict, match="expired"):
        store.confirm_pcm_forget(
            owner_id="user:7", agent_id="lily", session_id=session["session_id"],
            token=token, now=NOW + timedelta(minutes=6),
        )
    assert store.pending_pcm_forget(owner_id="user:7", agent_id="lily") is None


def test_confirm_only_redacts_once_and_replays_idempotently(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    session = store.create_session(owner_id="user:7", agent_id="lily")
    monkeypatch.setattr("orchestrator.session_store._utc_now", lambda: "2026-10-28T10:00:00+00:00")
    _complete(store, session_id=session["session_id"], owner_id="user:7", agent_id="lily",
              request_id="r", key="r", text="wrong idea", answer="wrong answer")
    preview = store.preview_pcm_forget(
        owner_id="user:7", agent_id="lily", session_id=session["session_id"],
        duration=timedelta(hours=3), now=NOW,
    )
    assert "wrong idea" in str(store.messages(session["session_id"], owner_id="user:7"))
    result = store.confirm_pcm_forget(
        owner_id="user:7", agent_id="lily", session_id=session["session_id"],
        token=preview["token"], now=NOW + timedelta(seconds=10),
    )
    assert result["messages"] == 2
    again = store.confirm_pcm_forget(
        owner_id="user:7", agent_id="lily", session_id=session["session_id"],
        token=preview["token"], now=NOW + timedelta(minutes=6),
    )
    assert again["operation_id"] == result["operation_id"]
    assert "wrong idea" not in str(store.messages(session["session_id"], owner_id="user:7"))


def test_conversation_change_invalidates_preview_without_redacting(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    session = store.create_session(owner_id="user:7", agent_id="lily")
    preview = store.preview_pcm_forget(
        owner_id="user:7", agent_id="lily", session_id=session["session_id"],
        duration=timedelta(hours=3), now=NOW,
    )
    monkeypatch.setattr("orchestrator.session_store._utc_now", lambda: "2026-10-28T12:01:00+00:00")
    _complete(store, session_id=session["session_id"], owner_id="user:7", agent_id="lily",
              request_id="new", key="new", text="new message", answer="new response")
    with pytest.raises(SessionConflict, match="changed"):
        store.confirm_pcm_forget(
            owner_id="user:7", agent_id="lily", session_id=session["session_id"],
            token=preview["token"], now=NOW + timedelta(minutes=2),
        )
    assert "new message" in str(store.messages(session["session_id"], owner_id="user:7"))
    assert store.pending_pcm_forget(owner_id="user:7", agent_id="lily") is None
