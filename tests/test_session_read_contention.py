from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from orchestrator.session_store import SessionStore


@pytest.mark.parametrize("operation", ["default", "primary"])
def test_existing_session_read_finishes_while_another_connection_holds_write_lock(tmp_path, operation):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="TEST")
    expected = store.resolve_primary_session(owner_id="user:7", agent_id="lily", establish=True)
    before = store.db_path.read_bytes()
    writer = sqlite3.connect(store.db_path)
    writer.execute("BEGIN IMMEDIATE")
    reader = store.ensure_default_session if operation == "default" else store.resolve_primary_session
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(reader, owner_id="user:7", agent_id="lily")
        try:
            actual = future.result(timeout=0.35)
        finally:
            writer.rollback()
            writer.close()
            future.result(timeout=3)
    assert actual["session_id"] == expected["session_id"]
    assert actual["updated_at"] == expected["updated_at"]
    assert store.db_path.read_bytes() == before


def test_existing_primary_read_does_not_queue_behind_local_writer_waiting_for_sqlite(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="TEST")
    expected = store.resolve_primary_session(owner_id="user:7", agent_id="lily", establish=True)
    external = sqlite3.connect(store.db_path)
    external.execute("BEGIN IMMEDIATE")
    entered = threading.Event()
    original_connect = store._connect

    def connect():
        connection = original_connect()
        connection.set_trace_callback(
            lambda statement: entered.set() if statement == "BEGIN IMMEDIATE" else None
        )
        return connection

    store._connect = connect
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting_writer = pool.submit(store.ensure_default_session, owner_id="user:8", agent_id="new")
        assert entered.wait(2), "Real local writer must reach its SQLite write transaction"
        read = pool.submit(store.resolve_primary_session, owner_id="user:7", agent_id="lily")
        try:
            actual = read.result(timeout=0.35)
        finally:
            external.rollback()
            external.close()
            waiting_writer.result(timeout=3)
            read.result(timeout=3)
    assert actual["session_id"] == expected["session_id"]


def test_concurrent_first_use_still_creates_one_default_per_owner_agent(tmp_path):
    stores = [SessionStore(tmp_path / "sessions.sqlite3", instance_id="TEST") for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        sessions = list(pool.map(
            lambda store: store.ensure_default_session(owner_id="user:7", agent_id="LILY"),
            stores,
        ))
    assert len({session["session_id"] for session in sessions}) == 1
    assert stores[0].ensure_default_session(owner_id="user:8", agent_id="lily")["session_id"] != sessions[0]["session_id"]
