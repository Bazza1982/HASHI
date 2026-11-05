"""One authority queues its own writers instead of losing a valid UI action."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from threading import Event, local
import time

import pytest

from tests.test_permission_actions import _issue, _receipt, setup_actions as setup_actions
from tests.test_permission_signed_receipts import _token


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
def test_waiting_revocation_precedes_later_status_poll(setup_actions, monkeypatch):
    authority, key, _, _, context, scope, request = setup_actions
    action = _issue(authority, context, request, [scope], lifetime="task")
    lease = authority.decide(_token(key, _receipt(action, context)), approved_scopes=[scope], action_id=action.action_id)
    entered, release = Event(), Event()
    caller, commits = local(), []
    advance = authority._advance_continuity

    def commit(connection):
        advance(connection)
        commits.append(caller.label)
        if caller.label == "first":
            entered.set()
            assert release.wait(10)

    def invoke(label):
        caller.label = label
        if label == "revoke":
            return authority.revoke(context, lease.lease_id)
        return authority.evaluate(context, [scope], operation_id="future")

    def queued(count):
        # Observe arrival only to arrange the schedule; the assertion below
        # concerns the real committed revocation and later access result.
        until = time.monotonic() + 10
        while time.monotonic() < until:
            with authority._transaction_lock._condition:
                if len(authority._transaction_lock._waiting) >= count:
                    return
            time.sleep(.001)
        raise AssertionError("caller did not reach the transaction boundary")

    monkeypatch.setattr(authority, "_advance_continuity", commit)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(invoke, "first")
        try:
            assert entered.wait(10)
            revoke = pool.submit(invoke, "revoke")
            queued(1)
            later = pool.submit(invoke, "later")
            queued(2)
        finally:
            release.set()
        assert first.result(timeout=10)
        revoke.result(timeout=10)
        assert not later.result(timeout=10)
    assert commits == ["first", "revoke", "later"]
# HASHI-Fingerprint: hfp_43b6547acd8f4ef3826059d1587a5249
