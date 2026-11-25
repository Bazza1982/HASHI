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


def _new_exec(store, rule, exec_id="hx1"):
    program = compile_hal(SCRIPT)
    store.create_exec(
        owner_id="user:7", rule_id=rule["rule_id"], rule_revision=rule["revision"],
        rule_name="n", workflow_index=0, exec_id=exec_id, trigger_event_id="evt-1",
        trigger_agent="@lily@hashi4", trigger_kind="run.completed", program_blob=program,
    )


def test_enable_rule_atomic_conflict_writes_nothing(store):
    rule, _ = store.upsert_rule(owner_id="user:7", name="n", script=SCRIPT)
    store.set_cursor("user:7", 100)
    with pytest.raises(HalStoreError):
        store.enable_rule(
            owner_id="user:7", rule_id=rule["rule_id"],
            expected_revision=99, baseline_cursor=150,
        )
    r = store.get_rule("user:7", rule["rule_id"])
    assert r["enabled"] == 0
    assert r["enable_cursor"] is None
    assert store.get_cursor("user:7") == 100  # owner cursor untouched on conflict


def test_enable_rule_anchors_owner_cursor_only_when_absent(store):
    rule_a, _ = store.upsert_rule(owner_id="user:7", name="a", script=SCRIPT)
    rule_b, _ = store.upsert_rule(owner_id="user:7", name="b", script=SCRIPT)
    store.enable_rule(
        owner_id="user:7", rule_id=rule_a["rule_id"],
        expected_revision=None, baseline_cursor=42,
    )
    assert store.get_cursor("user:7") == 42
    # A second enable must not move the shared owner cursor (that would drop
    # already-consumed history for rule A).
    store.enable_rule(
        owner_id="user:7", rule_id=rule_b["rule_id"],
        expected_revision=None, baseline_cursor=99,
    )
    assert store.get_cursor("user:7") == 42
    assert store.get_rule("user:7", rule_a["rule_id"])["enable_cursor"] == 42
    assert store.get_rule("user:7", rule_b["rule_id"])["enable_cursor"] == 99


def test_admit_stage_dispatch_atomic(store):
    rule, _ = store.upsert_rule(owner_id="user:7", name="n", script=SCRIPT)
    store.set_enabled("user:7", rule["rule_id"], True)
    _new_exec(store, rule)
    res = store.admit_stage_dispatch(
        owner_id="user:7", rule_id=rule["rule_id"], exec_id="hx1", stage=0,
        idempotency_key="hal:hx1:0", kind="prompt", target="@lily", payload="hi",
        pre_state="pending",
    )
    assert res == "admitted"
    assert store.get_receipt("hx1", 0)["state"] == "pending"


def test_admit_stage_dispatch_refuses_disabled_rule(store):
    rule, _ = store.upsert_rule(owner_id="user:7", name="n", script=SCRIPT)
    _new_exec(store, rule)  # rule never enabled
    res = store.admit_stage_dispatch(
        owner_id="user:7", rule_id=rule["rule_id"], exec_id="hx1", stage=0,
        idempotency_key="hal:hx1:0", kind="prompt", target="@lily", payload="hi",
        pre_state="pending",
    )
    assert res == "rule_disabled"
    assert store.get_receipt("hx1", 0) is None


def test_admit_stage_dispatch_refuses_settled_exec(store):
    rule, _ = store.upsert_rule(owner_id="user:7", name="n", script=SCRIPT)
    store.set_enabled("user:7", rule["rule_id"], True)
    _new_exec(store, rule)
    store.update_exec("hx1", state="disabled", detail="rule disabled")
    res = store.admit_stage_dispatch(
        owner_id="user:7", rule_id=rule["rule_id"], exec_id="hx1", stage=0,
        idempotency_key="hal:hx1:0", kind="prompt", target="@lily", payload="hi",
        pre_state="pending",
    )
    assert res == "exec_not_active"
    assert store.get_receipt("hx1", 0) is None


def test_admit_stage_dispatch_never_overwrites_terminal_receipt(store):
    rule, _ = store.upsert_rule(owner_id="user:7", name="n", script=SCRIPT)
    store.set_enabled("user:7", rule["rule_id"], True)
    _new_exec(store, rule)
    store.upsert_receipt(
        exec_id="hx1", stage=0, idempotency_key="hal:hx1:0", kind="prompt",
        target="@lily", payload="hi", state="dispatched", detail="accepted",
    )
    res = store.admit_stage_dispatch(
        owner_id="user:7", rule_id=rule["rule_id"], exec_id="hx1", stage=0,
        idempotency_key="hal:hx1:0", kind="prompt", target="@lily", payload="hi",
        pre_state="pending",
    )
    assert res == "already_terminal"
    assert store.get_receipt("hx1", 0)["state"] == "dispatched"


def test_advance_and_fail_never_overwrite_disabled(store):
    rule, _ = store.upsert_rule(owner_id="user:7", name="n", script=SCRIPT)
    _new_exec(store, rule)
    store.update_exec("hx1", state="disabled", detail="rule disabled")
    assert store.advance_exec_if_active(
        owner_id="user:7", exec_id="hx1", stage=1, state="completed"
    ) is False
    assert store.fail_exec_if_active(
        owner_id="user:7", exec_id="hx1", detail="boom"
    ) is False
    assert store.get_exec("user:7", "hx1")["state"] == "disabled"


def test_concurrent_enable_rule_does_not_corrupt_baselines(store):
    from concurrent.futures import ThreadPoolExecutor

    rule_a, _ = store.upsert_rule(owner_id="user:7", name="a", script=SCRIPT)
    rule_b, _ = store.upsert_rule(owner_id="user:7", name="b", script=SCRIPT)

    def enable(rid):
        store.enable_rule(
            owner_id="user:7", rule_id=rid, expected_revision=None, baseline_cursor=42
        )

    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(enable, [rule_a["rule_id"], rule_b["rule_id"]]))

    assert store.get_rule("user:7", rule_a["rule_id"])["enable_cursor"] == 42
    assert store.get_rule("user:7", rule_b["rule_id"])["enable_cursor"] == 42
    # The shared owner cursor is anchored exactly once, never clobbered.
    assert store.get_cursor("user:7") == 42


def test_migration_key_schema_migration_on_existing_store(tmp_path):
    import sqlite3

    path = tmp_path / "hal" / "hal_v1.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Simulate a pre-migration_key store: hal_rules without the new column.
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE hal_rules (
            rule_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
            name TEXT NOT NULL, script TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 0,
            enable_cursor INTEGER,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_hal_rules_owner ON hal_rules(owner_id);
        """
    )
    conn.commit()
    conn.close()

    # Opening the pre-existing store must add the column + unique index without
    # crashing, and the create/find contract must work on the migrated schema.
    s = HalStore(path)
    try:
        rule, created = s.upsert_rule(
            owner_id="user:7", name="n1", script=SCRIPT, migration_key="hea:1@A",
        )
        assert created is True
        assert rule["migration_key"] == "hea:1@A"
        found = s.find_rule_by_migration_key("user:7", "hea:1@A")
        assert found is not None
        assert found["rule_id"] == rule["rule_id"]

        # Reopen to prove the migrated schema (column + index) persists.
        s.close()
        s = HalStore(path)
        found2 = s.find_rule_by_migration_key("user:7", "hea:1@A")
        assert found2 is not None
        assert found2["rule_id"] == rule["rule_id"]
    finally:
        s.close()
# HASHI-Fingerprint: hfp_1a8547dce6b446f69a5d6009ef471ff1
