"""One authority queues its own writers instead of losing a valid UI action."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from threading import Event

import pytest

from tests.test_permission_actions import _issue, setup_actions as setup_actions


def test_same_authority_waits_for_its_active_commit_without_sqlite_busy_failure(setup_actions, monkeypatch):
    authority, _, _, _, context, scope, request = setup_actions
    committing, release, second_started = Event(), Event(), Event()
    connect = authority._connect
    advance = authority._advance_continuity

    def no_external_writer_wait():
        connection = connect()
        # Fail immediately if another connection is used while our own writer
        # is active. Independent-process contention still belongs to SQLite.
        connection.execute("PRAGMA busy_timeout=0")
        return connection

    def held_commit(connection):
        advance(connection)
        if not committing.is_set():
            committing.set()
            assert release.wait(10), "test did not release the durable writer"

    def render(*, second=False):
        if second:
            second_started.set()
        return _issue(
            authority, context, request, [scope],
            presentation_id="same-menu", presentation_slot="approve-once",
        )

    monkeypatch.setattr(authority, "_connect", no_external_writer_wait)
    monkeypatch.setattr(authority, "_advance_continuity", held_commit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(render)
        try:
            assert committing.wait(10), "first writer did not reach its commit"
            second = pool.submit(render, second=True)
            assert second_started.wait(10), "second caller did not start"
            # Its action depends on an uncommitted presentation. It must queue,
            # not raise SQLITE_BUSY or publish a second durable action.
            with pytest.raises(FutureTimeout):
                second.result(timeout=0.2)
        finally:
            release.set()
        first_action, second_action = first.result(timeout=10), second.result(timeout=10)

    assert first_action == second_action
    assert authority.decision_count(request.request_id) == 0
    assert not authority.evaluate(context, [scope], operation_id="operation")
# HASHI-Fingerprint: hfp_43b6547acd8f4ef3826059d1587a5249
