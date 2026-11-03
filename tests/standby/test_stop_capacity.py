"""Emergency controls have bounded storage independent of ordinary admission."""
import hashlib
import json

import pytest

from orchestrator.session_store import SessionStore
from orchestrator.standby.contract import StandbyError


OWNER = "user:7"


@pytest.fixture
def native(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3",instance_id="STOP-TEST")
    session = store.ensure_default_session(owner_id=OWNER,agent_id="alice")
    binding = {k:session[k] for k in ("agent_id","session_id","context_generation")}
    control = store.standby_control(owner_id=OWNER,body={
        "operation":"configure","operation_id":"configure-one","issued_at":1000,
        "binding":binding,"profile_id":"quiet","profile_revision":"a"*64},now=1000)
    return store,binding,control


def fill_receipts(store,control):
    digest = hashlib.sha256(b"synthetic-capacity-fixture").hexdigest()
    with store._connection() as connection:
        count = connection.execute("SELECT count(*) FROM standby_control_operations").fetchone()[0]
        connection.executemany("INSERT INTO standby_control_operations VALUES (?,?,?,?,?,?,?)",[
            (store.instance_id,OWNER,"alice","capacity-"+str(i),digest,json.dumps(control),1000)
            for i in range(4096-count)])


def request(binding,current,operation,key):
    return {"operation":operation,"operation_id":key,"issued_at":1000,"binding":binding,
        "standby_id":current["standby_id"],"expected_revision":current["revision"],
        "epoch":current["epoch"]}


@pytest.mark.parametrize("operation",["pause","end","delete_data"])
def test_full_ordinary_receipts_cannot_prevent_emergency_control(native,operation):
    store,binding,first = native
    fill_receipts(store,first)
    body = request(binding,first,operation,"emergency-"+operation)
    result = store.standby_control(owner_id=OWNER,body=body,now=1000)
    assert result["lifecycle"] == ("PAUSED" if operation == "pause" else "STOPPED")
    assert result["epoch"] == first["epoch"]+1
    assert result["revision"] == first["revision"]+1
    assert store.standby_control(owner_id=OWNER,body=body,now=1001) == result
    receipt = store.standby_operation_status(owner_id=OWNER,agent_id="alice",
        operation_id=body["operation_id"])
    assert receipt == {"state":"committed","session":result}
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM standby_control_operations").fetchone()[0] == 4096
        assert connection.execute("SELECT count(*) FROM standby_safety_operations").fetchone()[0] == 1


def test_full_ordinary_budget_still_rejects_new_config_without_partial_handle(native):
    store,binding,first = native
    fill_receipts(store,first)
    with pytest.raises(StandbyError,match="standby_capacity_reached"):
        store.standby_control(owner_id=OWNER,body={
            "operation":"configure","operation_id":"over-budget","issued_at":1000,
            "binding":binding,"profile_id":"quiet","profile_revision":"b"*64},now=1000)
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM standby_controls").fetchone()[0] == 1
    stopped = store.standby_control(owner_id=OWNER,
        body=request(binding,first,"end","end-under-load"),now=1000)
    assert stopped["lifecycle"] == "STOPPED"


def test_emergency_receipts_are_bounded_and_superseded_request_cannot_reexecute(native):
    store,binding,first = native
    fill_receipts(store,first)
    oldest = request(binding,first,"end","end-first")
    current = store.standby_control(owner_id=OWNER,body=oldest,now=1000)
    for index in range(20):
        current = store.standby_control(owner_id=OWNER,
            body=request(binding,current,"end","end-"+str(index)),now=1000)
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) FROM standby_safety_operations").fetchone()[0] == 1
        ordinary = connection.execute("SELECT result_json FROM standby_control_operations WHERE operation_id='configure-one'").fetchone()
        assert json.loads(ordinary[0]) == first
    with pytest.raises(StandbyError,match="standby_revision_conflict"):
        store.standby_control(owner_id=OWNER,body=oldest,now=1000)
    assert store.standby_snapshot(owner_id=OWNER,binding=binding,
        standby_id=first["standby_id"]) == current
    assert store.standby_operation_status(owner_id=OWNER,agent_id="alice",
        operation_id="end-19")["session"] == current


def live_lease(store,binding,control):
    # Storage protocol fixture, not device/Judge/Engine qualification.
    with store._connection() as connection:
        connection.execute("UPDATE standby_controls SET lifecycle='ACTIVE' WHERE standby_id=?",
            (control["standby_id"],))
    capture_binding = {**binding,"client_id":"synthetic-browser"}
    lease = store.media_capture_acquire(owner_id=OWNER,binding=capture_binding,
        capture_id="synthetic-standby",purpose="standby",issued_at=1000,now=1000,
        standby_id=control["standby_id"],standby_epoch=control["epoch"])
    return capture_binding,lease


def auth(lease):
    return {k:lease[k] for k in ("capture_id","lease_token","epoch")}


@pytest.mark.parametrize("operation",["pause","end","delete_data"])
def test_control_fences_capture_in_the_same_native_transaction(native,operation):
    store,binding,first = native
    capture_binding,lease = live_lease(store,binding,first)
    current = store.standby_control(owner_id=OWNER,
        body=request(binding,first,operation,"control-"+operation),now=1000)
    assert current["epoch"] > first["epoch"]
    # Inspect before asking the capture service: revocation cannot rely on a
    # later poll or a process-local callback to become durable.
    with store._connection() as connection:
        row = connection.execute("SELECT state,fence_epoch FROM media_capture_leases").fetchone()
        assert row["state"] == "REVOKING" and row["fence_epoch"] > lease["epoch"]
    assert not store.media_capture_renew(owner_id=OWNER,binding=capture_binding,
        **auth(lease),sequence=1,now=1001)["granted"]


@pytest.mark.parametrize("change",[
    "epoch=epoch+1",
    "lifecycle='PAUSED'",
    "disposal='PURGED_LOCAL'",
    None,
])
def test_late_capture_checks_current_standby_authority_even_without_a_callback(native,change):
    store,binding,first = native
    capture_binding,lease = live_lease(store,binding,first)
    # A second native connection changes canonical authority; no callback/cache.
    second = SessionStore(store.db_path,instance_id=store.instance_id)
    with second._connection() as connection:
        if change:
            connection.execute("UPDATE standby_controls SET "+change+" WHERE standby_id=?",
                (first["standby_id"],))
        else:
            connection.execute("DELETE FROM standby_controls WHERE standby_id=?",(first["standby_id"],))
    assert not store.media_capture_status(owner_id=OWNER,binding=capture_binding,
        **auth(lease),now=1001)["granted"]
    assert not store.media_capture_renew(owner_id=OWNER,binding=capture_binding,
        **auth(lease),sequence=1,now=1001)["granted"]
    assert store.media_capture_release(owner_id=OWNER,binding=capture_binding,
        **auth(lease),stopped=True,now=1001)["state"] == "RELEASED"


def test_stopping_old_standby_does_not_revoke_new_call_or_other_owner(native):
    store,binding,first = native
    capture_binding,lease = live_lease(store,binding,first)
    paused = store.standby_control(owner_id=OWNER,
        body=request(binding,first,"pause","pause-first"),now=1000)
    store.media_capture_release(owner_id=OWNER,binding=capture_binding,
        **auth(lease),stopped=True,now=1001)
    call = store.media_capture_acquire(owner_id=OWNER,binding=capture_binding,
        capture_id="new-call",purpose="call",issued_at=1001,now=1001)
    other = store.ensure_default_session(owner_id="user:8",agent_id="bob")
    other_binding = {k:other[k] for k in ("agent_id","session_id","context_generation")}
    other_binding["client_id"] = "other-browser"
    other_lease = store.media_capture_acquire(owner_id="user:8",binding=other_binding,
        capture_id="other-call",purpose="phone",issued_at=1001,now=1001)
    store.standby_control(owner_id=OWNER,
        body=request(binding,paused,"delete_data","delete-old"),now=1001)
    assert store.media_capture_status(owner_id=OWNER,binding=capture_binding,
        **auth(call),now=1001)["granted"]
    assert store.media_capture_status(owner_id="user:8",binding=other_binding,
        **auth(other_lease),now=1001)["granted"]
