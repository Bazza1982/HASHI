"""Shared native metadata owner; these tests do not qualify physical tracks."""
import multiprocessing
import os

import pytest

from orchestrator.session_store import SessionStore


def _claim(store, scope, capture="capture-a", purpose="call", now=1000, **extra):
    return store.media_capture_acquire(owner_id="user:7", binding=scope,
        capture_id=capture, purpose=purpose, issued_at=now, now=now, **extra)


def _compete(db, scope, purpose, ready, queue):
    store = SessionStore(db, instance_id="CAPTURE-TEST")
    ready.wait(15)
    try:
        result = _claim(store, scope, "capture-" + purpose, purpose)
        queue.put((os.getpid(), purpose, result["granted"], result["reason"]))
    except Exception as error:
        queue.put((os.getpid(), purpose, False, type(error).__name__))


@pytest.fixture
def native(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="CAPTURE-TEST")
    session = store.ensure_default_session(owner_id="user:7", agent_id="alice")
    scope = {key:session[key] for key in ("agent_id","session_id","context_generation")}
    scope["client_id"] = "browser-one"
    return store, scope


def auth(lease):
    return {key:lease[key] for key in ("capture_id","lease_token","epoch")}


def test_real_process_call_phone_compete_for_same_owner_instance(native):
    store, scope = native
    context = multiprocessing.get_context("spawn")
    ready, queue = context.Event(), context.Queue()
    processes = [context.Process(target=_compete,
        args=(str(store.db_path), {**scope,"client_id":"browser-"+kind}, kind, ready, queue))
        for kind in ("call","phone")]
    try:
        for process in processes:
            process.start()
        ready.set()
        results = [queue.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(10)
        assert len({result[0] for result in results}) == 2
        assert all(result[0] != os.getpid() for result in results)
        assert sorted(result[2] for result in results) == [False, True], results
        assert next(result[3] for result in results if not result[2]) == "capture_media_busy"
        assert all(process.exitcode == 0 for process in processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        queue.close()
        queue.join_thread()


def test_grant_recovery_renew_idempotency_expiry_and_late_reply(native):
    store, scope = native
    lease = _claim(store, scope)
    assert lease["granted"] is True and lease["expires_at"] == 1015
    assert _claim(store, scope) == lease
    renewed = store.media_capture_renew(owner_id="user:7", binding=scope,
        **auth(lease), sequence=1, now=1005)
    assert renewed["expires_at"] == 1020
    assert store.media_capture_renew(owner_id="user:7", binding=scope,
        **auth(lease), sequence=1, now=1009) == renewed
    expired = store.media_capture_renew(owner_id="user:7", binding=scope,
        **auth(lease), sequence=2, now=1020)
    assert expired["granted"] is False
    # Moving the clock backwards cannot revive a receipt or expired capture.
    assert store.media_capture_renew(owner_id="user:7", binding=scope,
        **auth(lease), sequence=2, now=1005)["granted"] is False
    assert _claim(store, scope, now=1020)["granted"] is False
    store.media_capture_release(owner_id="user:7",binding=scope,
        **auth(lease),stopped=True,now=1020)
    replacement = _claim(store, scope, "capture-b", now=1021)
    assert replacement["granted"] and replacement["epoch"] > lease["epoch"]
    # A late stop acknowledgement for the old capture cannot stop its replacement.
    stopped = store.media_capture_release(owner_id="user:7", binding=scope,
        **auth(lease), stopped=True, now=1022)
    assert stopped["granted"] is False
    current = store.media_capture_status(owner_id="user:7", binding=scope,
        **auth(replacement), now=1022)
    assert current["granted"] is True


def test_revoke_does_not_claim_physical_release_and_status_does_not_renew(native):
    store, scope = native
    lease = _claim(store, scope)
    before = store.media_capture_status(owner_id="user:7", binding=scope,
        **auth(lease), now=1004)
    assert before["expires_at"] == 1015
    pending = store.media_capture_release(owner_id="user:7", binding=scope,
        **auth(lease), stopped=False, now=1005)
    assert pending["state"] == "REVOKING" and not pending["granted"]
    assert not _claim(store, {**scope,"client_id":"browser-two"}, "capture-b", "phone", 1006)["granted"]
    released = store.media_capture_release(owner_id="user:7", binding=scope,
        **auth(lease), stopped=True, now=1007)
    assert released["state"] == "RELEASED"
    assert _claim(store, {**scope,"client_id":"browser-two"}, "capture-b", "phone", 1007)["granted"]


def test_scope_token_and_sequence_rejections_do_not_mutate_the_winner(native):
    from orchestrator.frontend_capture import CaptureError
    store, scope = native
    lease = _claim(store, scope)
    for changes in ({"lease_token":"forged"}, {"epoch":True},
                    {"binding":{**scope,"client_id":"other-browser"}},
                    {"owner_id":"user:other"}):
        request = {"owner_id":"user:7","binding":scope,**auth(lease),"sequence":1,"now":1001}
        request.update(changes)
        with pytest.raises(CaptureError):
            store.media_capture_renew(**request)
    with pytest.raises(CaptureError):
        store.media_capture_renew(owner_id="user:7",binding=scope,
            **auth(lease),sequence=2,now=1001)
    assert store.media_capture_status(owner_id="user:7",binding=scope,
        **auth(lease),now=1002)["expires_at"] == 1015


def test_standby_preemption_commits_pause_before_any_call_grant(native):
    store, scope = native
    binding = {k:v for k,v in scope.items() if k != "client_id"}
    control = store.standby_control(owner_id="user:7", body={
        "operation":"configure","operation_id":"fixture-control","issued_at":1000,
        "binding":binding,"profile_id":"quiet","profile_revision":"a"*64}, now=1000)
    # Protocol fixture only: no media, device, model or live qualification.
    with store._connection() as connection:
        connection.execute("UPDATE standby_controls SET lifecycle='ACTIVE' WHERE standby_id=?",
            (control["standby_id"],))
    lease = _claim(store, scope, "capture-standby", "standby",
        standby_id=control["standby_id"],standby_epoch=control["epoch"])
    pending = _claim(store, {**scope,"client_id":"call-browser"},"capture-call","call",1005)
    assert not pending["granted"] and pending["reason"] == "capture_stop_pending"
    paused = store.standby_snapshot(owner_id="user:7",binding=binding,standby_id=control["standby_id"])
    assert paused["lifecycle"] == "PAUSED" and paused["epoch"] == control["epoch"]+1
    # A second connection observes the persisted fence, not a local busy flag.
    second = SessionStore(store.db_path, instance_id="CAPTURE-TEST")
    assert not second.media_capture_renew(owner_id="user:7",binding=scope,
        **auth(lease),sequence=1,now=1006)["granted"]
    second.media_capture_release(owner_id="user:7",binding=scope,
        **auth(lease),stopped=True,now=1006)
    call = _claim(store,{**scope,"client_id":"call-browser"},"capture-call","call",1006)
    assert call["granted"]
    store.media_capture_release(owner_id="user:7",binding={**scope,"client_id":"call-browser"},
        **auth(call),stopped=True,now=1007)
    assert store.standby_snapshot(owner_id="user:7",binding=binding,
        standby_id=control["standby_id"])["lifecycle"] == "PAUSED"


def test_expired_standby_stays_locked_until_stop_receipt_and_cannot_renew(native):
    store, scope = native
    binding = {k:v for k,v in scope.items() if k != "client_id"}
    control = store.standby_control(owner_id="user:7",body={
        "operation":"configure","operation_id":"fixture-expiry","issued_at":1000,
        "binding":binding,"profile_id":"quiet","profile_revision":"a"*64},now=1000)
    with store._connection() as connection:
        connection.execute("UPDATE standby_controls SET lifecycle='ACTIVE' WHERE standby_id=?",
            (control["standby_id"],))
    lease = _claim(store,scope,"capture-standby","standby",standby_id=control["standby_id"],
        standby_epoch=control["epoch"])
    assert not _claim(store,scope,"capture-call","call",1004)["granted"]
    assert not _claim(store,scope,"capture-call","call",1015)["granted"]
    store.media_capture_release(owner_id="user:7",binding=scope,
        **auth(lease),stopped=True,now=1015)
    assert _claim(store,scope,"capture-call","call",1015)["granted"]
    assert not store.media_capture_release(owner_id="user:7",binding=scope,
        **auth(lease),stopped=True,now=1016)["granted"]


def test_multiple_owners_and_instances_are_distinct_and_no_content_is_created(native):
    store, scope = native
    lease = _claim(store,scope)
    other = store.ensure_default_session(owner_id="user:8",agent_id="bob")
    other_scope = {key:other[key] for key in ("agent_id","session_id","context_generation")}
    other_scope["client_id"] = "other-owner-browser"
    assert store.media_capture_acquire(owner_id="user:8",binding=other_scope,
        capture_id="owner-eight",purpose="phone",issued_at=1000,now=1000)["granted"]
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM media_capture_leases").fetchone()[0] == 2
    assert store.media_capture_status(owner_id="user:7",binding=scope,
        **auth(lease),now=1001)["granted"]
    other_store = SessionStore(store.db_path.parent / "other-instance.sqlite3",instance_id="OTHER")
    other_session = other_store.ensure_default_session(owner_id="user:7",agent_id="alice")
    other_binding = {key:other_session[key] for key in ("agent_id","session_id","context_generation")}
    other_binding["client_id"] = scope["client_id"]
    assert _claim(other_store,other_binding)["granted"]


def test_expired_request_ids_and_closed_capture_ids_never_open_again(native):
    from orchestrator.frontend_capture import CaptureError
    store, scope = native
    with pytest.raises(CaptureError,match="capture_request_expired"):
        store.media_capture_acquire(owner_id="user:7",binding=scope,
            capture_id="too-old",purpose="call",issued_at=900,now=1000)
    lease = _claim(store,scope)
    store.media_capture_release(owner_id="user:7",binding=scope,**auth(lease),stopped=True,now=1001)
    assert _claim(store,scope)["granted"] is False
    assert _claim(store,scope,"capture-b",now=1002)["granted"]
    assert _claim(store,scope,now=1003)["granted"] is False

def test_observed_clock_rollback_revokes_without_renewal(native):
    store,scope = native
    lease = _claim(store,scope)
    assert store.media_capture_status(owner_id="user:7",binding=scope,
        **auth(lease),now=1004)["expires_at"] == 1015
    changed = store.media_capture_renew(owner_id="user:7",binding=scope,
        **auth(lease),sequence=1,now=1003)
    assert changed["state"] == "REVOKING" and not changed["granted"]
    assert not _claim(store,scope,"capture-b",now=1005)["granted"]


def test_generation_revocation_keeps_stop_available(native):
    store,scope = native
    lease = _claim(store,scope)
    with store._connection() as connection:
        connection.execute("UPDATE sessions SET context_generation=context_generation+1 WHERE session_id=?",
            (scope["session_id"],))
    status = store.media_capture_status(owner_id="user:7",binding=scope,
        **auth(lease),now=1001)
    assert status["state"] == "REVOKING" and not status["granted"]
    assert store.media_capture_release(owner_id="user:7",binding=scope,
        **auth(lease),stopped=True,now=1001)["state"] == "RELEASED"


def test_full_acquisition_budget_cannot_block_stop(native):
    from orchestrator.frontend_capture import CaptureError,MAX_RECENT_ATTEMPTS
    store,scope = native
    for index in range(MAX_RECENT_ATTEMPTS):
        lease = _claim(store,scope,"capture-"+str(index))
        store.media_capture_release(owner_id="user:7",binding=scope,
            **auth(lease),stopped=True,now=1000)
    with pytest.raises(CaptureError,match="capture_capacity_reached"):
        _claim(store,scope,"capture-over-budget")
    assert store.media_capture_release(owner_id="user:7",binding=scope,
        **auth(lease),stopped=True,now=1000)["state"] == "RELEASED"
    replacement = _claim(store,scope,"capture-after-pruning",now=1121)
    assert replacement["granted"]


def test_native_standby_cannot_bypass_control_lifecycle_or_epoch(native):
    from orchestrator.frontend_capture import CaptureError
    store,scope = native
    binding = {k:v for k,v in scope.items() if k != "client_id"}
    control = store.standby_control(owner_id="user:7",body={
        "operation":"configure","operation_id":"not-live","issued_at":1000,
        "binding":binding,"profile_id":"quiet","profile_revision":"a"*64},now=1000)
    with pytest.raises(CaptureError,match="capture_standby_scope_changed"):
        _claim(store,scope,"capture-standby","standby",standby_id=control["standby_id"],
            standby_epoch=control["epoch"])
    with store._connection() as connection:
        connection.execute("UPDATE standby_controls SET lifecycle='ACTIVE' WHERE standby_id=?",
            (control["standby_id"],))
    with pytest.raises(CaptureError,match="capture_standby_scope_changed"):
        _claim(store,scope,"capture-standby","standby",standby_id=control["standby_id"],
            standby_epoch=control["epoch"]+1)

def test_expiry_is_not_a_physical_stop_receipt(native):
    store,scope = native
    lease = _claim(store,scope)
    expired = store.media_capture_status(owner_id="user:7",binding=scope,
        **auth(lease),now=1015)
    assert not expired["granted"]
    waiting = _claim(store,{**scope,"client_id":"another-browser"},"capture-phone","phone",1016)
    assert not waiting["granted"], "expiry alone cannot prove an unqualified browser stopped"
    store.media_capture_release(owner_id="user:7",binding=scope,
        **auth(lease),stopped=False,now=1017)
    assert not _claim(store,scope,"capture-phone","phone",1018)["granted"]
    store.media_capture_release(owner_id="user:7",binding=scope,
        **auth(lease),stopped=True,now=1018)
    assert _claim(store,scope,"capture-phone","phone",1018)["granted"]


def test_lost_grant_can_be_recovered_after_expiry_without_restarting_capture(native):
    store,scope = native
    original = {"owner_id":"user:7","binding":scope,"capture_id":"lost-response",
                "purpose":"call","issued_at":1000}
    lease = store.media_capture_acquire(**original,now=1000)
    recovered = store.media_capture_acquire(**original,now=1016)
    assert not recovered["granted"]
    assert recovered["lease_token"] == lease["lease_token"]
    assert recovered["expires_at"] == lease["expires_at"]
    assert store.media_capture_release(owner_id="user:7",binding=scope,
        **auth(recovered),stopped=True,now=1016)["state"] == "RELEASED"
