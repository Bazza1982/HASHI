"""Schema 36 -> 37 migration and rollback behaviour for the HEA event index.

Schema 37 adds ``automation_event_index``: an append-only, AUTOINCREMENT
projection of durable terminal Run events used by the HEA feed cursor. The
Session data model itself is unchanged; Sessions, Runs, messages and run_events
carry over untouched.

Baseline policy (documented): events are projected into the index from the
moment the upgraded store starts writing them. Terminal events recorded on a
schema-36 database BEFORE the migration are not retro-projected, so the feed
head starts at zero and consumers initialise with after=-1 (no history replay).

Rollback policy (documented): a 36 reader refuses to open a 37 database by
design ("Session database requires a newer schema reader"). Rollback is
therefore a restore operation: restore the pre-upgrade backup, or (explicitly
and with HEA consumers stopped) set schema_metadata to 36 and drop
automation_event_index. Re-upgrading re-creates the index and the feed
re-baselines through the reset_required contract.
"""
import sqlite3
from pathlib import Path

import pytest

from orchestrator.automation_events import terminal_events_page
from orchestrator.session_store import SessionStore


class Schema36Store(SessionStore):
    SCHEMA_VERSION = 36


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "sessions.db"


def _meta_version(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        connection.close()


def _downgrade_to_36(path: Path) -> None:
    """Turn a 37 database into the exact shape a 36-era build would have:
    the event index table removed and the stored schema version reset."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE IF EXISTS automation_event_index")
        connection.execute(
            "UPDATE schema_metadata SET value='36' WHERE key='schema_version'"
        )
        connection.commit()
    finally:
        connection.close()


def _seed_session_and_run(store: SessionStore, agent: str = "agent-a") -> dict:
    session = store.resolve_primary_session(owner_id="user:7", agent_id=agent, establish=True)
    run = store.accept_run(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id=agent,
        request_id=f"req-{agent}",
        text="seed",
        source="test.schema",
        idempotency_key=f"seed-key-{agent}",
    )
    return {"session_id": session["session_id"], "run_id": run.run_id}


def _append_terminal(store: SessionStore, session_id: str, kind: str = "run.completed") -> None:
    with store._lock, store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        store._append_event(
            connection, session_id=session_id, run_id=None, kind=kind,
            status="completed" if kind == "run.completed" else "failed", phase="terminal",
        )


def test_fresh_database_is_current_schema_with_event_index(db_path):
    store = SessionStore(db_path, instance_id="HASHI1")
    assert _meta_version(db_path) == SessionStore.SCHEMA_VERSION
    with store._lock, store._connection() as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='automation_event_index'"
        ).fetchone()
    assert table is not None


def test_schema36_database_migrates_with_sessions_and_runs_intact(db_path):
    # Build a genuine schema-36 shape: seed data, then remove the index table
    # and reset the stored version exactly as a 36 build would have it.
    store = SessionStore(db_path, instance_id="HASHI1")
    seeded = _seed_session_and_run(store)
    _append_terminal(store, seeded["session_id"], "run.completed")
    _append_terminal(store, seeded["session_id"], "run.failed")
    del store
    _downgrade_to_36(db_path)
    assert _meta_version(db_path) == 36

    # The 37 build migrates on open: table recreated, version restored, and
    # all prior Session/Run data intact.
    reopened = SessionStore(db_path, instance_id="HASHI1")
    assert _meta_version(db_path) == SessionStore.SCHEMA_VERSION
    assert reopened.get_session(seeded["session_id"], owner_id="user:7") is not None
    run = reopened.get_run(seeded["run_id"], owner_id="user:7")
    assert run["run_id"] == seeded["run_id"]
    with reopened._lock, reopened._connection() as connection:
        index_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_event_index'"
        ).fetchone()
    assert index_exists is not None


def test_old_terminal_events_follow_the_baseline_policy(db_path):
    # Events recorded BEFORE the upgrade are not retro-projected: the feed
    # head starts at zero and after=-1 baselines there (no history replay).
    store = SessionStore(db_path, instance_id="HASHI1")
    seeded = _seed_session_and_run(store)
    _append_terminal(store, seeded["session_id"], "run.completed")
    del store
    _downgrade_to_36(db_path)

    reopened = SessionStore(db_path, instance_id="HASHI1")
    baseline = terminal_events_page(reopened, owner_id="user:7", after=-1, limit=50)
    assert baseline["events"] == []
    assert baseline["next_cursor"] == 0
    assert baseline["latest_cursor"] == 0


def test_new_terminal_events_receive_monotonic_feed_ids(db_path):
    store = SessionStore(db_path, instance_id="HASHI1")
    seeded = _seed_session_and_run(store)
    _append_terminal(store, seeded["session_id"], "run.completed")
    _append_terminal(store, seeded["session_id"], "run.failed")
    page = terminal_events_page(store, owner_id="user:7", after=-1, limit=50)
    assert page["next_cursor"] == 2
    _append_terminal(store, seeded["session_id"], "run.completed")
    after = terminal_events_page(store, owner_id="user:7", after=2, limit=50)
    assert [event["cursor"] for event in after["events"]] == [3]
    assert after["events"][0]["event_id"]


def test_deletion_never_reuses_feed_ids(db_path):
    store = SessionStore(db_path, instance_id="HASHI1")
    seeded = _seed_session_and_run(store)
    _append_terminal(store, seeded["session_id"], "run.completed")
    _append_terminal(store, seeded["session_id"], "run.completed")
    assert terminal_events_page(store, owner_id="user:7", after=-1, limit=50)["next_cursor"] == 2

    # Destructive history deletion removes the indexed rows...
    store.purge_owner(owner_id="user:7")
    after_purge = terminal_events_page(store, owner_id="user:7", after=0, limit=50)
    assert after_purge["latest_cursor"] == 0

    # ...but AUTOINCREMENT must never hand a deleted feed_id back out.
    session = store.resolve_primary_session(owner_id="user:7", agent_id="agent-b", establish=True)
    _append_terminal(store, session["session_id"], "run.completed")
    page = terminal_events_page(store, owner_id="user:7", after=0, limit=50)
    assert [event["cursor"] for event in page["events"]] == [3]
    with store._lock, store._connection() as connection:
        max_feed = connection.execute(
            "SELECT MAX(feed_id) FROM automation_event_index"
        ).fetchone()[0]
    assert max_feed == 3


def test_reopen_after_migration_keeps_version_and_events(db_path):
    store = SessionStore(db_path, instance_id="HASHI1")
    seeded = _seed_session_and_run(store)
    _append_terminal(store, seeded["session_id"], "run.completed")
    del store
    _downgrade_to_36(db_path)

    first = SessionStore(db_path, instance_id="HASHI1")
    _append_terminal(first, seeded["session_id"], "run.failed")
    del first

    second = SessionStore(db_path, instance_id="HASHI1")
    assert _meta_version(db_path) == SessionStore.SCHEMA_VERSION
    page = terminal_events_page(second, owner_id="user:7", after=-1, limit=50)
    assert page["next_cursor"] == 1  # only the post-migration event
    assert page["events"] == []


def test_older_reader_refuses_a_newer_database(db_path):
    store = SessionStore(db_path, instance_id="HASHI1")
    del store
    with pytest.raises(RuntimeError, match="requires a newer schema reader"):
        Schema36Store(db_path, instance_id="HASHI1")


def test_downgrade_procedure_then_reupgrade_rebaselines(db_path):
    """Documented rollback path: 36 metadata + no event index opens cleanly on
    a 36 reader; re-upgrading re-creates the index and the feed re-baselines
    through reset_required instead of silently skipping events."""
    store = SessionStore(db_path, instance_id="HASHI1")
    seeded = _seed_session_and_run(store)
    _append_terminal(store, seeded["session_id"], "run.completed")
    del store

    # Roll back to schema 36 (documented manual procedure).
    _downgrade_to_36(db_path)
    legacy = Schema36Store(db_path, instance_id="HASHI1")
    assert _meta_version(db_path) == 36
    assert legacy.get_session(seeded["session_id"], owner_id="user:7") is not None
    del legacy

    # Re-upgrade.
    upgraded = SessionStore(db_path, instance_id="HASHI1")
    assert _meta_version(db_path) == SessionStore.SCHEMA_VERSION
    # The feed head restarts at zero; a consumer holding an old cursor must
    # see reset_required and re-baseline (documented contract), never skip.
    stale_consumer = terminal_events_page(upgraded, owner_id="user:7", after=5, limit=50)
    assert stale_consumer["reset_required"] is True
    fresh_consumer = terminal_events_page(upgraded, owner_id="user:7", after=-1, limit=50)
    assert fresh_consumer["next_cursor"] == 0
# HASHI-Fingerprint: hfp_fafdbb52a7c2438491a947410551dc52
