"""HEA v1 terminal event projection: read-only and cursor-safe."""
import sqlite3
import threading
from contextlib import contextmanager

from orchestrator.automation_events import terminal_events_page


class Store:
    instance_id = "HASHI1"

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE sessions(
                    session_id TEXT PRIMARY KEY, instance_id TEXT, owner_id TEXT,
                    agent_id TEXT, status TEXT
                );
                CREATE TABLE runs(run_id TEXT PRIMARY KEY, idempotency_key TEXT);
                CREATE TABLE run_events(
                    event_id TEXT PRIMARY KEY, session_id TEXT, run_id TEXT,
                    kind TEXT, created_at TEXT
                );
                INSERT INTO sessions VALUES ('s1','HASHI1','owner-a','agent-a','active');
                INSERT INTO sessions VALUES ('s2','HASHI1','owner-b','agent-b','active');
                INSERT INTO runs VALUES ('r1','manual');
                INSERT INTO runs VALUES ('r2','hea:linked-event');
                INSERT INTO run_events VALUES ('e1','s1','r1','run.started','t0');
                INSERT INTO run_events VALUES ('e2','s1','r1','run.completed','t1');
                INSERT INTO run_events VALUES ('e3','s2','r1','run.failed','t2');
                INSERT INTO run_events VALUES ('e4','s1','r2','run.failed','t3');
            """)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def test_owner_scope_cursor_bootstrap_and_automation_origin(tmp_path):
    store = Store(tmp_path / "hea.sqlite")
    result = terminal_events_page(store, owner_id="owner-a", after=0)
    assert [event["type"] for event in result["events"]] == [
        "agent.run.completed", "agent.run.failed",
    ]
    assert [event["event_id"] for event in result["events"]] == ["e2", "e4"]
    assert [event["automation_origin"] for event in result["events"]] == [False, True]
    assert all(event["agent_id"] == "agent-a" for event in result["events"])
    assert terminal_events_page(store, owner_id="owner-b", after=0)["events"][0]["event_id"] == "e3"
    boot = terminal_events_page(store, owner_id="owner-a", after=-1)
    assert boot["events"] == [] and boot["next_cursor"] == result["latest_cursor"]
    assert terminal_events_page(store, owner_id="owner-a", after=boot["next_cursor"])["events"] == []


def test_paging_reset_and_bounds(tmp_path):
    store = Store(tmp_path / "hea.sqlite")
    first = terminal_events_page(store, owner_id="owner-a", after=0, limit=1)
    assert first["has_more"] is True
    second = terminal_events_page(store, owner_id="owner-a", after=first["next_cursor"])
    assert [event["event_id"] for event in second["events"]] == ["e4"]
    assert terminal_events_page(store, owner_id="owner-a", after=9999)["reset_required"] is True
    for after, limit in ((-2, 5), (0, 0), (0, 101)):
        try:
            terminal_events_page(store, owner_id="owner-a", after=after, limit=limit)
            assert False, "expected ValueError"
        except ValueError:
            pass
