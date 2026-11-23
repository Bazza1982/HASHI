"""HAL durable store: CRUD, revision conflicts, idempotent execution rows."""
import pytest

from hal import compile_hal
from hal.runtime import EXEC_ACTIVE, EXEC_FAILED
from hal.store import HalStore, HalStoreError


@pytest.fixture()
def store(tmp_path):
    s = HalStore(tmp_path / "hal" / "hal_v1.sqlite")
    yield s
    s.close()


SCRIPT = 'if @agent_any #turn_finish then @lily $prompt "hi".'


def test_upsert_creates_and_updates_with_revision(store):
    rule, created = store.upsert_rule(owner_id="user:7", name="n1", script=SCRIPT)
    assert created is True
    assert rule["revision"] == 1
    assert rule["enabled"] == 0

    updated, created = store.upsert_rule(
        owner_id="user:7", name="n1", script=SCRIPT, rule_id=rule["rule_id"],
        expected_revision=1,
    )
    assert created is False
    assert updated["revision"] == 2

    with pytest.raises(HalStoreError):
        store.upsert_rule(
            owner_id="user:7", name="n1", script=SCRIPT, rule_id=rule["rule_id"],
            expected_revision=1,
        )


def test_upsert_rejects_blank_fields(store):
    with pytest.raises(HalStoreError):
        store.upsert_rule(owner_id="user:7", name="", script=SCRIPT)
    with pytest.raises(HalStoreError):
        store.upsert_rule(owner_id="user:7", name="n", script="  ")


def test_enable_toggle_and_owner_scoping(store):
    rule, _ = store.upsert_rule(owner_id="user:7", name="n", script=SCRIPT)
    assert store.enabled_owner_ids() == []
    store.set_enabled("user:7", rule["rule_id"], True)
    assert store.enabled_owner_ids() == ["user:7"]
    assert store.enabled_rule_count() == 1
    # Another owner cannot see or flip it.
    assert store.get_rule("user:8", rule["rule_id"]) is None
    assert store.set_enabled("user:8", rule["rule_id"], False) is None
    store.set_enabled("user:7", rule["rule_id"], False)
    assert store.enabled_owner_ids() == []


def test_create_exec_is_unique_per_rule_and_event(store):
    program = compile_hal(SCRIPT)
    ok = store.create_exec(
        owner_id="user:7", rule_id="r1", rule_revision=1, rule_name="n",
        workflow_index=0, exec_id="hx1", trigger_event_id="evt-1",
        trigger_agent="@lily@hashi4", trigger_kind="run.completed",
        program_blob=program,
    )
    assert ok is True
    dup = store.create_exec(
        owner_id="user:7", rule_id="r1", rule_revision=1, rule_name="n",
        workflow_index=0, exec_id="hx2", trigger_event_id="evt-1",
        trigger_agent="@lily@hashi4", trigger_kind="run.completed",
        program_blob=program,
    )
    assert dup is False
    row = store.get_exec("user:7", "hx1")
    assert row["state"] == EXEC_ACTIVE
    assert row["stage"] == 0


def test_exec_lifecycle_and_receipts(store):
    program = compile_hal(SCRIPT)
    store.create_exec(
        owner_id="user:7", rule_id="r1", rule_revision=1, rule_name="n",
        workflow_index=0, exec_id="hx1", trigger_event_id="evt-1",
        trigger_agent="@lily@hashi4", trigger_kind="run.completed",
        program_blob=program,
    )
    store.upsert_receipt(
        exec_id="hx1", stage=0, idempotency_key="hal:hx1:0", kind="prompt",
        target="@lily", payload="hi", state="dispatched", detail="accepted",
    )
    assert store.get_receipt("hx1", 0)["state"] == "dispatched"
    store.update_exec("hx1", stage=1, state=EXEC_FAILED, detail="boom")
    assert store.get_exec("user:7", "hx1")["state"] == EXEC_FAILED
    assert store.list_active_execs("user:7") == []
    assert len(store.list_execs("user:7")) == 1


def test_consumed_events_are_once_only(store):
    assert store.mark_exec_event_consumed("hx1", "evt-9") is True
    assert store.mark_exec_event_consumed("hx1", "evt-9") is False
    assert store.mark_exec_event_consumed("hx1", "evt-10") is True


def test_cursor_defaults_and_advances(store):
    assert store.get_cursor("user:7") == -1
    store.set_cursor("user:7", 12)
    assert store.get_cursor("user:7") == 12
    store.set_cursor("user:7", 15)
    assert store.get_cursor("user:7") == 15
# HASHI-Fingerprint: hfp_1a8547dce6b446f69a5d6009ef471ff1
