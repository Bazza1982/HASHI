"""HEA v1 terminal event projection: monotonic, owner-scoped, cursor-safe.

The feed reads from the append-only ``automation_event_index`` whose
AUTOINCREMENT identity is monotonic even after history deletion, so SQLite
rowid reuse can never silently rewind or skip events.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.automation_events import (
    terminal_events_page,
    handle_automation_events,
)
from orchestrator.session_store import SessionStore


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.db", instance_id="HASHI1")


def _session(store, owner, agent):
    return store.resolve_primary_session(owner_id=owner, agent_id=agent, establish=True)


def _run(store, session_id, owner, agent, key, text="triggered"):
    return store.accept_run(
        session_id=session_id,
        owner_id=owner,
        agent_id=agent,
        request_id="req-" + key,
        text=text,
        source="session-api.test",
        idempotency_key=key,
    )


def _append(store, session_id, kind, run_id=None, status=None):
    with store._lock, store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        store._append_event(
            connection,
            session_id=session_id,
            run_id=run_id,
            kind=kind,
            status=status or ("completed" if kind == "run.completed" else "failed"),
            phase="terminal",
        )


def test_baseline_and_projection_order(store):
    owner = "user:7"
    session_a = _session(store, owner, "agent-a")
    session_b = _session(store, owner, "agent-b")
    run_auto = _run(store, session_a["session_id"], owner, "agent-a", "hea:job-x")
    run_plain = _run(store, session_a["session_id"], owner, "agent-a", "plain-1")
    run_b = _run(store, session_b["session_id"], owner, "agent-b", "plain-2")
    _append(store, session_a["session_id"], "run.completed", run_id=run_auto.run_id)
    _append(store, session_a["session_id"], "run.failed", run_id=run_plain.run_id)
    _append(store, session_b["session_id"], "run.completed", run_id=run_b.run_id)

    baseline = terminal_events_page(store, owner_id=owner, after=-1, limit=50)
    assert baseline["events"] == []
    assert baseline["next_cursor"] == 3
    assert baseline["latest_cursor"] == 3
    assert baseline["reset_required"] is False

    page = terminal_events_page(store, owner_id=owner, after=0, limit=50)
    assert [event["type"] for event in page["events"]] == [
        "agent.run.completed", "agent.run.failed", "agent.run.completed",
    ]
    assert [event["agent_id"] for event in page["events"]] == [
        "agent-a", "agent-a", "agent-b",
    ]
    assert [event["cursor"] for event in page["events"]] == [1, 2, 3]
    assert page["events"][0]["automation_origin"] is True
    assert page["events"][1]["automation_origin"] is False
    assert page["next_cursor"] == 3
    assert page["has_more"] is False
    for event in page["events"]:
        assert "text" not in event and "error" not in event
        assert event["run_id"]
        assert event["session_id"]
        assert event["occurred_at"]
        assert event["event_id"]


def test_pagination_walks_the_feed(store):
    owner = "user:7"
    session = _session(store, owner, "agent-a")
    for index in range(5):
        _append(store, session["session_id"], "run.completed", run_id=None)
    first = terminal_events_page(store, owner_id=owner, after=0, limit=2)
    assert [event["cursor"] for event in first["events"]] == [1, 2]
    assert first["has_more"] is True
    second = terminal_events_page(store, owner_id=owner, after=first["next_cursor"], limit=2)
    assert [event["cursor"] for event in second["events"]] == [3, 4]
    third = terminal_events_page(store, owner_id=owner, after=second["next_cursor"], limit=2)
    assert [event["cursor"] for event in third["events"]] == [5]
    assert third["has_more"] is False


def test_history_purge_never_reuses_feed_identity(store):
    owner = "user:7"
    session = _session(store, owner, "agent-a")
    _append(store, session["session_id"], "run.completed", run_id=None)
    _append(store, session["session_id"], "run.completed", run_id=None)
    _append(store, session["session_id"], "run.failed", run_id=None)
    before = terminal_events_page(store, owner_id=owner, after=2, limit=50)
    assert [event["cursor"] for event in before["events"]] == [3]

    store.purge_owner(owner_id=owner)
    after_purge = terminal_events_page(store, owner_id=owner, after=3, limit=50)
    assert after_purge["events"] == []
    assert after_purge["reset_required"] is True
    assert after_purge["latest_cursor"] == 0

    # New events after destructive history deletion must keep strictly larger
    # feed identities: no silent loss, no cursor reuse.
    fresh = _session(store, owner, "agent-a")
    _append(store, fresh["session_id"], "run.completed", run_id=None)
    _append(store, fresh["session_id"], "run.failed", run_id=None)
    resumed = terminal_events_page(store, owner_id=owner, after=3, limit=50)
    cursors = [event["cursor"] for event in resumed["events"]]
    assert len(cursors) == 2
    assert min(cursors) > 3
    assert cursors == sorted(cursors)
    assert resumed["reset_required"] is False


def test_reset_is_detected_when_cursor_is_ahead_of_history(store):
    owner = "user:7"
    session = _session(store, owner, "agent-a")
    _append(store, session["session_id"], "run.completed", run_id=None)
    result = terminal_events_page(store, owner_id=owner, after=999, limit=50)
    assert result["reset_required"] is True
    assert result["next_cursor"] == 1


def test_owner_isolation(store):
    owner_a = "user:7"
    owner_b = "user:8"
    session_a = _session(store, owner_a, "agent-a")
    session_b = _session(store, owner_b, "agent-b")
    _append(store, session_a["session_id"], "run.completed", run_id=None)
    _append(store, session_b["session_id"], "run.completed", run_id=None)
    page_a = terminal_events_page(store, owner_id=owner_a, after=0, limit=50)
    page_b = terminal_events_page(store, owner_id=owner_b, after=0, limit=50)
    assert [event["agent_id"] for event in page_a["events"]] == ["agent-a"]
    assert [event["agent_id"] for event in page_b["events"]] == ["agent-b"]
    assert page_a["latest_cursor"] == 1
    assert page_b["latest_cursor"] == 2


def test_soft_deleted_session_events_are_hidden(store):
    owner = "user:7"
    session = _session(store, owner, "agent-a")
    _append(store, session["session_id"], "run.completed", run_id=None)
    with store._lock, store._connection() as connection:
        connection.execute(
            "UPDATE sessions SET status='deleted' WHERE session_id=?",
            (session["session_id"],),
        )
    page = terminal_events_page(store, owner_id=owner, after=0, limit=50)
    assert page["events"] == []
    # Cursor state stays monotonic: a later event in a live Session still
    # arrives with a larger cursor.
    live = _session(store, owner, "agent-a2")
    _append(store, live["session_id"], "run.completed", run_id=None)
    resumed = terminal_events_page(store, owner_id=owner, after=0, limit=50)
    assert len(resumed["events"]) == 1
    assert resumed["events"][0]["cursor"] == 2


def test_argument_validation(store):
    with pytest.raises(ValueError):
        terminal_events_page(store, owner_id="user:7", after=-2, limit=50)
    with pytest.raises(ValueError):
        terminal_events_page(store, owner_id="user:7", after=0, limit=0)
    with pytest.raises(ValueError):
        terminal_events_page(store, owner_id="user:7", after=0, limit=101)


class _FeedServer:
    def __init__(self, store):
        self.session_store = store
        self.logger = None

    def _v1_owner_id(self, _request):
        return "user:7"

    def _check_admin_auth(self, _request):
        return True


def test_handler_requires_owner_and_admin_auth(store):
    server = _FeedServer(store)

    class _Anonymous:
        def _v1_owner_id(self, _request):
            return None

        def _check_admin_auth(self, _request):
            return True

    denied = asyncio.run(handle_automation_events(
        _Anonymous(), SimpleNamespace(query={}, headers={}),
    ))
    assert denied.status == 401


def test_handler_serves_the_contract(store):
    server = _FeedServer(store)
    session = _session(store, "user:7", "agent-a")
    _append(store, session["session_id"], "run.completed", run_id=None)
    response = asyncio.run(handle_automation_events(
        server,
        SimpleNamespace(query={"after": "0", "limit": "50", "wait_seconds": "0"}, headers={}),
    ))
    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["ok"] is True
    assert payload["schema"] == "hashi.automation-events/v1"
    assert payload["instance_id"] == "HASHI1"
    assert payload["events"][0]["type"] == "agent.run.completed"
    assert response.headers["Cache-Control"] == "no-store"
# HASHI-Fingerprint: hfp_9724a37cd98c433ab672810edf40cf2d
