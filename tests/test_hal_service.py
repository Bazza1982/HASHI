"""HAL service end-to-end: real feed SQL projection + recording dispatcher.

These tests run the actual ``orchestrator.automation_events.terminal_events_page``
SQL against a minimal in-memory Session schema, so cursor handling, owner scope,
automation-origin filtering and correlation behave exactly as in production.
Dispatch is recorded (never executed) — live dispatch is exercised in the
non-production PIT acceptance.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hal import service as hal_service_mod
from hal import compile_hal
from hal.dispatch import DispatchResult, RecordingDispatchAdapter
from hal.event_source import HalTrigger
from hal.runtime import DispatchDirective
from hal.service import HalService, handle_hal_rules_get, handle_hal_rules_post, handle_hal_status, handle_hal_validate, handle_hal_rule_enable, handle_hal_rule_disable, handle_hal_runs_get
from hal.store import HalStore, HalStoreError

SCHEMA = """
CREATE TABLE automation_event_index (
    feed_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT, run_id TEXT, session_id TEXT, kind TEXT,
    created_at TEXT, agent_id TEXT, owner_id TEXT
);
CREATE TABLE sessions (session_id TEXT PRIMARY KEY, status TEXT);
CREATE TABLE runs (run_id TEXT PRIMARY KEY, idempotency_key TEXT, session_id TEXT);
"""


class FakeSessionStore:
    """Minimal SessionStore surface the real feed projection needs."""

    def __init__(self, instance_id="TESTINST"):
        self.instance_id = instance_id
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self.active_rows: list = []
        self.list_active_runs_calls = 0
        self._seq = 0

    @contextmanager
    def _connection(self):
        yield self._conn

    def add_event(
        self,
        *,
        owner_id="user:7",
        agent_id="lily",
        kind="run.completed",
        key=None,
        session_id=None,
    ):
        self._seq += 1
        session_id = session_id or f"sess-{self._seq}"
        run_id = f"run-{self._seq}"
        self._conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, status) VALUES (?, 'active')",
            (session_id,),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO runs (run_id, idempotency_key, session_id) VALUES (?,?,?)",
            (run_id, key or "", session_id),
        )
        self._conn.execute(
            "INSERT INTO automation_event_index (event_id, run_id, session_id, kind, "
            "created_at, agent_id, owner_id) VALUES (?,?,?,?,?,?,?)",
            (
                f"evt-{self._seq}",
                run_id,
                session_id,
                kind,
                "2026-11-23T00:00:00Z",
                agent_id,
                owner_id,
            ),
        )
        self._conn.commit()
        return f"evt-{self._seq}"

    def list_active_runs(self, *, owner_id, session_id=None):
        self.list_active_runs_calls += 1
        return self.active_rows


def make_service(tmp_path, feed, clock=None):
    store = HalStore(tmp_path / "hal" / "hal_v1.sqlite")
    adapter = RecordingDispatchAdapter()
    service = HalService(
        store=store, session_store=feed, dispatch=adapter,
        logger=logging.getLogger("hal.test"), clock=clock,
    )
    return service, adapter


class FakeClock:
    """Deterministic clock for the bounded-retry backoff."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_rule(service, owner_id, script, name="rule", rule_id=None):
    return service.upsert_rule(owner_id=owner_id, name=name, script=script, rule_id=rule_id)[0]


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ service


def test_entry_prompt_dispatch_binds_trigger_agent(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(
        service, "user:7",
        'if @agent_any #turn_finish then @alice $prompt "hi @agent_trigger".',
    )
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event(agent_id="lily")

    run(service._process_owner("user:7"))

    assert len(adapter.prompt_calls) == 1
    call = adapter.prompt_calls[0]
    assert call["target_agent"] == "alice"
    assert call["text"] == "hi @lily@TESTINST"
    assert call["idempotency_key"].startswith("hal:hx")
    execs = service.store.list_execs("user:7")
    assert len(execs) == 1
    assert execs[0]["state"] == "completed"
    assert execs[0]["stage"] == 1
    # Second cycle must not re-dispatch.
    run(service._process_owner("user:7"))
    assert len(adapter.prompt_calls) == 1


def test_automation_origin_event_never_starts_a_rule(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "hi".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event(key="hal:hx1:0")  # HAL-dispatched run completing

    run(service._process_owner("user:7"))
    assert adapter.prompt_calls == []


def test_bootstrap_adopts_cursor_without_replaying_history(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "hi".')
    feed.add_event()  # existed before HAL was enabled
    assert service.store.get_cursor("user:7") == -1

    # Enabling anchors the cursor: pre-enable history must never fire.
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    assert service.store.get_cursor("user:7") >= 1
    run(service._process_owner("user:7"))
    assert adapter.prompt_calls == []
    # New events after enable do fire.
    feed.add_event()
    run(service._process_owner("user:7"))
    assert len(adapter.prompt_calls) == 1


def test_dependent_chain_advances_only_on_correlated_run(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(
        service,
        "user:7",
        'if @agent_any #turn_finish then @alice $prompt "start"; '
        'if @agent_any #turn_finish then @bob $prompt "next".',
    )
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event(key="user-run-1")

    run(service._process_owner("user:7"))
    assert len(adapter.prompt_calls) == 1
    exec_id = adapter.prompt_calls[0]["idempotency_key"].split(":")[1]

    # Unrelated completion starts its own entry execution (correct semantics)
    # but must NOT advance the first workflow's dependent stage.
    feed.add_event(key="someone-else")
    run(service._process_owner("user:7"))
    assert len(adapter.prompt_calls) == 2
    chain_exec = next(
        row for row in service.store.list_execs("user:7")
        if row["trigger_event_id"] == "evt-1"
    )
    assert chain_exec["stage"] == 1
    assert chain_exec["state"] == "active"

    # Correlated completion of the HAL-dispatched run: advance + complete.
    feed.add_event(key=f"hal:{exec_id}:0")
    run(service._process_owner("user:7"))
    assert len(adapter.prompt_calls) == 3
    assert adapter.prompt_calls[2]["target_agent"] == "bob"
    updated = service.store.get_exec("user:7", chain_exec["exec_id"])
    assert updated["state"] == "completed"
    assert updated["stage"] == 2


def test_unavailable_dependent_stage_waits_honestly(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(
        service,
        "user:7",
        'if @agent_any #turn_finish then @alice $prompt "start"; '
        'if #reboot_ok then @bob $prompt "after reboot".',
    )
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event()
    run(service._process_owner("user:7"))
    execs = service.store.list_execs("user:7")
    assert execs[0]["state"] == "active"
    assert "no reboot-completion" in (execs[0]["detail"] or "")


def test_system_idle_is_unavailable_in_v01(tmp_path):
    """#system_idle keeps its vocabulary/compile support but has no reliable
    active->idle transition source in v0.1: enabling is refused and no idle
    edge is ever synthesized, so a terminal event never dispatches."""
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if #system_idle then @alice $prompt "cleanup".')
    with pytest.raises(HalStoreError):
        service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    assert service.store.get_rule("user:7", rule["rule_id"])["enabled"] == 0

    feed.active_rows = []
    feed.add_event()
    run(service._process_owner("user:7"))
    assert adapter.prompt_calls == []
    assert adapter.command_calls == []


def test_transient_prompt_failure_retries_same_key(tmp_path):
    clock = FakeClock()
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed, clock=clock)
    calls = {"n": 0}

    def script(key):
        calls["n"] += 1
        if calls["n"] == 1:
            return DispatchResult(accepted=False, transient=True, detail="busy")
        return DispatchResult(accepted=True, transient=False, detail="ok")

    adapter._prompt_script = script
    rule = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "hi".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event()
    # First attempt is transient and enters the bounded backoff: no same-cycle
    # storm retry; the loop reports no activity (waiting), not a 0.1s spin.
    assert run(service._process_owner("user:7")) is False
    assert len(adapter.prompt_calls) == 1
    assert service.store.list_execs("user:7")[0]["state"] == "active"

    # Once the backoff elapses, recovery retries with the SAME idempotency key.
    clock.advance(3.0)
    assert run(service._process_owner("user:7")) is True
    assert len(adapter.prompt_calls) == 2
    assert adapter.prompt_calls[0]["idempotency_key"] == adapter.prompt_calls[1]["idempotency_key"]
    assert service.store.list_execs("user:7")[0]["state"] == "completed"


def test_command_rejection_fails_stage_honestly(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    adapter._command_results = {
        "/reboot max": DispatchResult(accepted=False, transient=False, detail="command not supported or disabled")
    }
    rule = make_rule(service, "user:7", 'if @lily@TESTINST #turn_finish then /reboot max.')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event(agent_id="lily")
    run(service._process_owner("user:7"))
    execs = service.store.list_execs("user:7")
    assert execs[0]["state"] == "failed"
    assert "not supported" in (execs[0]["detail"] or "")


def test_ambiguous_command_never_retries_after_interruption(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if @lily #turn_finish then /reboot max.')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    # Simulate: receipt pre-written, crash before outcome recorded.
    service.store.create_exec(
        owner_id="user:7", rule_id=rule["rule_id"], rule_revision=rule["revision"],
        rule_name="rule", workflow_index=0, exec_id="hx1", trigger_event_id="evt-1",
        trigger_agent="@lily@TESTINST", trigger_kind="run.completed",
        program_blob=__import__("hal").compile_hal('if @lily #turn_finish then /reboot max.'),
    )
    service.store.upsert_receipt(
        exec_id="hx1", stage=0, idempotency_key="hal:hx1:0", kind="native_command",
        target=None, payload="/reboot max", state="unknown", detail=None,
    )
    run(service._recover_executions("user:7", [], {"hx1": service.store.get_exec("user:7", "hx1")}, {(("hx1", 0)): service.store.get_receipt("hx1", 0)}))
    assert adapter.command_calls == []  # never re-executed
    execs = service.store.list_execs("user:7")
    assert execs[0]["state"] == "failed"
    assert "unknown after interruption" in (execs[0]["detail"] or "")


def test_enable_rejects_unavailable_entry_event(tmp_path):
    feed = FakeSessionStore()
    service, _ = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if #turn_commentary then @alice $prompt "x".')
    with pytest.raises(HalStoreError):
        service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    assert service.store.get_rule("user:7", rule["rule_id"])["enabled"] == 0


# --- H1: multiple independent workflows -----------------------------------


def test_multi_workflow_rule_dispatches_both_entries(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(
        service,
        "user:7",
        'if @agent_any #turn_finish then @alice $prompt "a".\n'
        'if @agent_any #turn_finish then @bob $prompt "b".\n',
    )
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event()
    run(service._process_owner("user:7"))
    assert sorted(c["target_agent"] for c in adapter.prompt_calls) == ["alice", "bob"]
    execs = service.store.list_execs("user:7")
    assert sorted(e["workflow_index"] for e in execs) == [0, 1]
    # Same event must not re-dispatch on the next (empty) page.
    run(service._process_owner("user:7"))
    assert len(adapter.prompt_calls) == 2


# --- H2: @agent_trigger target binding + instance identity -----------------


def test_agent_trigger_as_prompt_target_binds_runtime(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(
        service,
        "user:7",
        'if @agent_any #turn_finish then @agent_trigger $prompt "hello @agent_trigger".',
    )
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event(agent_id="lily")
    run(service._process_owner("user:7"))
    assert len(adapter.prompt_calls) == 1
    assert adapter.prompt_calls[0]["target_agent"] == "lily"
    assert adapter.prompt_calls[0]["text"] == "hello @lily@TESTINST"


def test_cross_instance_prompt_target_rejected(tmp_path):
    feed = FakeSessionStore()  # instance_id == TESTINST
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(
        service,
        "user:7",
        'if @agent_any #turn_finish then @alice@OTHER $prompt "hi".',
    )
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event()
    run(service._process_owner("user:7"))
    assert adapter.prompt_calls == []
    execs = service.store.list_execs("user:7")
    assert execs[0]["state"] == "failed"
    assert "not this instance" in (execs[0]["detail"] or "")


# --- H4: retry without new events + settle on disable ----------------------


def test_pending_prompt_retries_without_new_event(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    clock = FakeClock()
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed, clock=clock)
    calls = {"n": 0}

    def script(key):
        calls["n"] += 1
        if calls["n"] <= 2:
            return DispatchResult(accepted=False, transient=True, detail="busy")
        return DispatchResult(accepted=True, transient=False, detail="ok")

    adapter._prompt_script = script
    rule = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "hi".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event()
    # Entry dispatch fails transient -> deferred (no same-cycle storm retry).
    assert run(service._process_owner("user:7")) is False
    assert len(adapter.prompt_calls) == 1
    assert service.store.list_execs("user:7")[0]["state"] == "active"

    # No new event: after the backoff elapses, recovery retries the SAME key.
    clock.advance(2.0)
    assert run(service._process_owner("user:7")) is True
    assert len(adapter.prompt_calls) == 2
    assert service.store.list_execs("user:7")[0]["state"] == "active"

    clock.advance(4.0)
    assert run(service._process_owner("user:7")) is True
    assert len(adapter.prompt_calls) == 3
    assert adapter.prompt_calls[0]["idempotency_key"] == adapter.prompt_calls[2]["idempotency_key"]
    assert service.store.list_execs("user:7")[0]["state"] == "completed"


def test_disabling_rule_settles_inflight_execs(tmp_path):
    feed = FakeSessionStore()
    service, _ = make_service(tmp_path, feed)
    rule = make_rule(
        service,
        "user:7",
        'if @agent_any #turn_finish then @alice $prompt "start"; '
        'if @agent_any #turn_finish then @bob $prompt "next".',
    )
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event()
    run(service._process_owner("user:7"))
    assert service.store.list_execs("user:7")[0]["state"] == "active"

    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=False)
    execs = service.store.list_execs("user:7")
    assert execs[0]["state"] == "disabled"
    assert "disabled while execution in progress" in (execs[0]["detail"] or "")


# --- H5: draft semantics + no history replay -------------------------------


def test_save_update_returns_rule_to_draft(tmp_path):
    feed = FakeSessionStore()
    service, _ = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "a".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    assert service.store.get_rule("user:7", rule["rule_id"])["enabled"] == 1

    updated, created = service.upsert_rule(
        owner_id="user:7",
        name="rule",
        script='if @agent_any #turn_finish then @alice $prompt "b".',
        rule_id=rule["rule_id"],
        expected_revision=1,
    )
    assert created is False
    assert updated["enabled"] == 0
    assert service.store.get_rule("user:7", rule["rule_id"])["enabled"] == 0


def test_enable_rejects_any_unavailable_workflow_entry(tmp_path):
    feed = FakeSessionStore()
    service, _ = make_service(tmp_path, feed)
    # workflow[0] entry available, workflow[1] entry unavailable: must not enable.
    rule = make_rule(
        service,
        "user:7",
        'if @agent_any #turn_finish then @alice $prompt "a".\n'
        'if #turn_commentary then @bob $prompt "b".\n',
    )
    with pytest.raises(HalStoreError):
        service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    assert service.store.get_rule("user:7", rule["rule_id"])["enabled"] == 0


def test_second_rule_does_not_replay_preenable_history(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule1 = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "one".')
    service.set_enabled(owner_id="user:7", rule_id=rule1["rule_id"], enabled=True)
    feed.add_event()
    run(service._process_owner("user:7"))
    assert len(adapter.prompt_calls) == 1

    rule2 = make_rule(service, "user:7", 'if @agent_any #turn_finish then @bob $prompt "two".')
    service.set_enabled(owner_id="user:7", rule_id=rule2["rule_id"], enabled=True)
    run(service._process_owner("user:7"))  # no new events
    assert len(adapter.prompt_calls) == 1  # rule2 did NOT consume evt-1

    feed.add_event()
    run(service._process_owner("user:7"))
    assert sorted(c["target_agent"] for c in adapter.prompt_calls[1:]) == ["alice", "bob"]


# --- Round 2: idle loop, replay, per-rule baseline, command truth, UUID ------


def test_waiting_exec_reports_no_activity_not_fast_loop(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(
        service,
        "user:7",
        'if @agent_any #turn_finish then @alice $prompt "start"; '
        'if @agent_any #turn_finish then @bob $prompt "next".',
    )
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event()
    assert run(service._process_owner("user:7")) is True  # stage 0 dispatched
    assert len(adapter.prompt_calls) == 1
    # Stage 1 now waits for a correlated #turn_finish. With no new event the
    # owner must report NO activity so the run loop sleeps POLL_INTERVAL rather
    # than the 0.1s fast loop.
    for _ in range(4):
        assert run(service._process_owner("user:7")) is False
    assert len(adapter.prompt_calls) == 1


def test_prompt_retry_limit_fails_permanently(tmp_path):
    from hal.service import MAX_RETRY_ATTEMPTS, _retry_backoff

    clock = FakeClock()
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed, clock=clock)
    adapter._prompt_script = lambda key: DispatchResult(accepted=False, transient=True, detail="busy")
    rule = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "hi".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event()
    assert run(service._process_owner("user:7")) is False  # first transient, deferred

    calls = 1
    while calls < MAX_RETRY_ATTEMPTS:
        clock.advance(_retry_backoff(calls) + 0.001)
        run(service._process_owner("user:7"))
        calls += 1
    clock.advance(999)
    run(service._process_owner("user:7"))  # attempts now at cap -> settle failed
    assert len(adapter.prompt_calls) == MAX_RETRY_ATTEMPTS
    execs = service.store.list_execs("user:7")
    assert execs[0]["state"] == "failed"
    assert "retry limit" in (execs[0]["detail"] or "")


def test_replayed_unverified_command_never_redispatches(tmp_path):
    feed = FakeSessionStore()
    adapter = RecordingDispatchAdapter()
    path = tmp_path / "hal" / "hal_v1.sqlite"

    # First "process": dispatch the command once, then crash before the cursor
    # advances. The store keeps the accepted-but-unverified execution.
    store1 = HalStore(path)
    service1 = HalService(store=store1, session_store=feed, dispatch=adapter, logger=logging.getLogger("hal.test"))
    rule = make_rule(service1, "user:7", 'if @lily@TESTINST #turn_finish then /reboot max.')
    service1.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    trigger = HalTrigger(
        event="#turn_finish", agent_identity="@lily@TESTINST",
        feed_event_id="evt-1", feed_kind="run.completed",
        run_idempotency_key=None, automation_origin=False, feed_cursor=1,
    )
    rules1 = run(service1._load_enabled_rules("user:7"))
    run(service1._apply_trigger("user:7", trigger, rules1, {}, {}, {}))
    assert len(adapter.command_calls) == 1
    assert service1.store.list_execs("user:7")[0]["state"] == "unverified"
    store1.close()

    # "Process restart": reopen the same SQLite and replay the same event.
    store2 = HalStore(path)
    service2 = HalService(store=store2, session_store=feed, dispatch=adapter, logger=logging.getLogger("hal.test"))
    rules2 = run(service2._load_enabled_rules("user:7"))
    run(service2._apply_trigger("user:7", trigger, rules2, {}, {}, {}))
    assert len(adapter.command_calls) == 1  # still exactly one dispatch
    execs = service2.store.list_execs("user:7")
    assert execs[0]["state"] == "unverified"
    assert execs[0]["stage"] == 1
    assert "completion not verified" in (execs[0]["detail"] or "")
    store2.close()


def test_per_rule_enable_baseline_covers_unconsumed_events(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule_a = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "A".')
    service.set_enabled(owner_id="user:7", rule_id=rule_a["rule_id"], enabled=True)

    # Events committed before rule B is enabled, but not yet consumed.
    feed.add_event()
    feed.add_event()
    rule_b = make_rule(service, "user:7", 'if @agent_any #turn_finish then @bob $prompt "B".')
    service.set_enabled(owner_id="user:7", rule_id=rule_b["rule_id"], enabled=True)

    run(service._process_owner("user:7"))
    # Only A matches the two pre-enable events; B must not replay that history.
    assert sorted(c["target_agent"] for c in adapter.prompt_calls) == ["alice", "alice"]

    feed.add_event()
    run(service._process_owner("user:7"))
    assert sorted(c["target_agent"] for c in adapter.prompt_calls) == ["alice", "alice", "alice", "bob"]


def test_native_command_acceptance_is_unverified_not_completed(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if @lily@TESTINST #turn_finish then /reboot max.')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event(agent_id="lily")
    run(service._process_owner("user:7"))
    assert len(adapter.command_calls) == 1
    execs = service.store.list_execs("user:7")
    assert execs[0]["state"] == "unverified"
    assert "completion not verified" in (execs[0]["detail"] or "")


def test_new_rule_id_is_uuid(tmp_path):
    feed = FakeSessionStore()
    service, _ = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "hi".')
    assert rule["rule_id"].startswith("hl")
    assert len(rule["rule_id"]) == 34  # "hl" + 32 hex chars


# ------------------------------------------------ A1/A2/A3 (Round 3 review)


def _prompt_exec(service, rule, exec_id="hx1"):
    service.store.create_exec(
        owner_id="user:7", rule_id=rule["rule_id"], rule_revision=rule["revision"],
        rule_name="rule", workflow_index=0, exec_id=exec_id, trigger_event_id="evt-1",
        trigger_agent="@lily@TESTINST", trigger_kind="run.completed",
        program_blob=__import__("hal").compile_hal('if @agent_any #turn_finish then @alice $prompt "hi".'),
    )


def test_dispatch_stage_refuses_rule_disabled_after_plan(tmp_path):
    """A plan produced before a disable must not admit a new side effect."""
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "hi".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    _prompt_exec(service, rule)
    stale = {e["exec_id"]: e for e in service.store.list_active_execs("user:7")}
    # Disable after the plan/exec exists but before dispatch is applied.
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=False)
    directive = DispatchDirective(
        exec_id="hx1", stage=0, kind="prompt", target="@alice",
        payload='$prompt "hi"', idempotency_key="hal:hx1:0",
    )
    run(service._dispatch_stage("user:7", stale["hx1"], directive, [], {}))
    assert adapter.prompt_calls == []
    assert service.store.get_exec("user:7", "hx1")["state"] == "disabled"


def test_disable_during_prompt_dispatch_preserves_settlement(tmp_path):
    """A disable landing mid-dispatch must never overwrite the settlement."""
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "hi".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    state = {"disabled": False}

    def script(idempotency_key):
        if not state["disabled"]:
            state["disabled"] = True
            service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=False)
        return DispatchResult(accepted=True, transient=False, detail="accepted")

    adapter._prompt_script = script
    feed.add_event(agent_id="lily")
    run(service._process_owner("user:7"))

    assert len(adapter.prompt_calls) == 1
    execs = service.store.list_execs("user:7")
    assert len(execs) == 1
    assert execs[0]["state"] == "disabled"  # not overwritten to completed
    receipt = service.store.get_receipt(execs[0]["exec_id"], 0)
    assert receipt["state"] == "dispatched"  # real receipt preserved


def test_disable_after_pending_receipt_blocks_recovery_redispatch(tmp_path):
    """Pre-dispatch (pending) receipt + disable: recovery must not re-dispatch."""
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "hi".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    _prompt_exec(service, rule)
    service.store.upsert_receipt(
        exec_id="hx1", stage=0, idempotency_key="hal:hx1:0", kind="prompt",
        target="@alice", payload='$prompt "hi"', state="pending_retry", detail=None,
    )
    stale = {e["exec_id"]: e for e in service.store.list_active_execs("user:7")}
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=False)
    run(service._recover_executions(
        "user:7", [], stale, {("hx1", 0): service.store.get_receipt("hx1", 0)},
    ))
    assert adapter.prompt_calls == []
    assert service.store.get_exec("user:7", "hx1")["state"] == "disabled"


def test_disable_after_dispatch_receipt_does_not_complete_exec(tmp_path):
    """Dispatch completed but exec not yet advanced; a disable must not complete it."""
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "hi".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    _prompt_exec(service, rule)
    service.store.upsert_receipt(
        exec_id="hx1", stage=0, idempotency_key="hal:hx1:0", kind="prompt",
        target="@alice", payload='$prompt "hi"', state="dispatched", detail=None,
    )
    stale = {e["exec_id"]: e for e in service.store.list_active_execs("user:7")}
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=False)
    run(service._recover_executions(
        "user:7", [], stale, {("hx1", 0): service.store.get_receipt("hx1", 0)},
    ))
    assert adapter.prompt_calls == []
    assert service.store.get_exec("user:7", "hx1")["state"] == "disabled"


def test_enable_fails_when_baseline_read_fails(tmp_path, monkeypatch):
    """A stale owner cursor is never an unsafe fallback for a rule baseline."""
    feed = FakeSessionStore()
    service, _ = make_service(tmp_path, feed)
    rule1 = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "one".')
    service.set_enabled(owner_id="user:7", rule_id=rule1["rule_id"], enabled=True)
    service.store.set_cursor("user:7", 100)  # owner cursor deliberately behind
    rule2 = make_rule(service, "user:7", 'if @agent_any #turn_finish then @bob $prompt "two".')

    def boom(*args, **kwargs):
        raise RuntimeError("feed down")

    monkeypatch.setattr(hal_service_mod, "_feed_page", boom)
    with pytest.raises(HalStoreError) as excinfo:
        service.set_enabled(owner_id="user:7", rule_id=rule2["rule_id"], enabled=True)
    assert "baseline" in str(excinfo.value)

    # Rule stays disabled; no baseline was written; existing cursors untouched.
    assert service.store.get_rule("user:7", rule2["rule_id"])["enabled"] == 0
    assert service.store.get_rule("user:7", rule2["rule_id"])["enable_cursor"] is None
    assert service.store.get_cursor("user:7") == 100
    assert service.store.get_rule("user:7", rule1["rule_id"])["enabled"] == 1


def test_enable_revision_conflict_does_not_touch_baseline(tmp_path):
    """A revision conflict must fail before any baseline/enabled mutation."""
    feed = FakeSessionStore()
    service, _ = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "hi".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    before = service.store.get_rule("user:7", rule["rule_id"])["enable_cursor"]
    # Save bumps revision to 2 (and disables). Stale enable must conflict first.
    service.upsert_rule(
        owner_id="user:7", rule_id=rule["rule_id"], name="rule",
        script='if @agent_any #turn_finish then @alice $prompt "hi".', expected_revision=1,
    )
    with pytest.raises(HalStoreError) as excinfo:
        service.set_enabled(
            owner_id="user:7", rule_id=rule["rule_id"], enabled=True, expected_revision=1,
        )
    assert "revision conflict" in str(excinfo.value)
    assert service.store.get_rule("user:7", rule["rule_id"])["enable_cursor"] == before


def test_backlog_pages_consumed_without_idling(tmp_path):
    """has_more keeps draining a backlog instead of idling POLL_INTERVAL/page."""
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if @target@TESTINST #turn_finish then @alice $prompt "hi".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    for _ in range(250):
        feed.add_event(agent_id="other")

    # Page 1 (100 events, has_more) -> fast (no idle).
    assert run(service._process_owner("user:7")) is True
    assert service.store.get_cursor("user:7") == 100
    # Page 2 (100 events, has_more) -> fast.
    assert run(service._process_owner("user:7")) is True
    assert service.store.get_cursor("user:7") == 200
    # Page 3 (50 events, drained) -> returns to low-frequency wait.
    assert run(service._process_owner("user:7")) is False
    assert service.store.get_cursor("user:7") == 250
    # No rule matched; nothing dispatched; idle now stays low-frequency.
    assert adapter.prompt_calls == []
    assert run(service._process_owner("user:7")) is False


# ------------------------------------------------------------------ API


class FakeServer:
    def __init__(self, service, admin=True):
        self.session_store = service.session_store
        self._hal_service = service
        self.global_config = SimpleNamespace(bridge_home=str(service.store.path.parent.parent))
        self.config_path = Path(service.store.path.parent.parent) / "config.json"
        self.logger = logging.getLogger("hal.test.api")
        self._admin = admin

    def _v1_owner_id(self, request):
        return "user:7"

    def _check_admin_auth(self, request):
        return self._admin


def api_client(server: FakeServer):
    app = web.Application()
    app.router.add_get("/hal/status", lambda r: handle_hal_status(server, r))
    app.router.add_post("/hal/validate", lambda r: handle_hal_validate(server, r))
    app.router.add_get("/hal/rules", lambda r: handle_hal_rules_get(server, r))
    app.router.add_post("/hal/rules", lambda r: handle_hal_rules_post(server, r))
    app.router.add_post("/hal/rules/{rule_id}/enable", lambda r: handle_hal_rule_enable(server, r))
    app.router.add_post("/hal/rules/{rule_id}/disable", lambda r: handle_hal_rule_disable(server, r))
    app.router.add_get("/hal/runs", lambda r: handle_hal_runs_get(server, r))
    return TestClient(TestServer(app))


def test_api_status_and_validate(tmp_path):
    feed = FakeSessionStore()
    service, _ = make_service(tmp_path, feed)
    server = FakeServer(service)

    async def scenario():
        async with api_client(server) as client:
            response = await client.get("/hal/status")
            payload = await response.json()
            assert response.status == 200
            assert payload["schema"] == "hashi.hal/v1"
            assert payload["event_availability"]["#turn_commentary"]["available"] is False
            assert payload["event_availability"]["#turn_finish"]["available"] is True

            response = await client.post("/hal/validate", json={"script": 'if #system_idle then @a $prompt "x".'})
            payload = await response.json()
            assert response.status == 200
            assert payload["ok"] is True
            assert payload["workflows"][0]["entry_available"] is False

            response = await client.post("/hal/validate", json={"script": "if #system_idle then"})
            payload = await response.json()
            assert payload["ok"] is False

            response = await client.post("/hal/validate", data=b"x" * (64 * 1024 + 1), headers={"Content-Type": "application/json"})
            assert response.status == 413

    asyncio.run(scenario())


def test_api_rule_lifecycle_and_revision_conflict(tmp_path):
    feed = FakeSessionStore()
    service, _ = make_service(tmp_path, feed)
    server = FakeServer(service)
    script = 'if @agent_any #turn_finish then @alice $prompt "hi".'

    async def scenario():
        async with api_client(server) as client:
            response = await client.post("/hal/rules", json={"name": "n1", "script": script})
            payload = await response.json()
            assert response.status == 201
            rule_id = payload["rule"]["rule_id"]
            assert payload["rule"]["revision"] == 1
            assert payload["rule"]["enabled"] is False

            # Enable works for available entry events (script revision unchanged).
            response = await client.post(f"/hal/rules/{rule_id}/enable", json={})
            assert (await response.json())["rule"]["enabled"] is True

            # Valid edit with matching revision bumps it.
            response = await client.post(
                "/hal/rules",
                json={"rule_id": rule_id, "name": "n1b", "script": script, "expected_revision": 1},
            )
            payload = await response.json()
            assert response.status == 200
            assert payload["rule"]["revision"] == 2

            # Stale edit now conflicts.
            response = await client.post(
                "/hal/rules",
                json={"rule_id": rule_id, "name": "n1c", "script": script, "expected_revision": 1},
            )
            assert response.status == 409

            response = await client.get("/hal/rules")
            payload = await response.json()
            assert len(payload["rules"]) == 1
            assert payload["rules"][0]["validation"]["ok"] is True

            response = await client.post(f"/hal/rules/{rule_id}/disable", json={})
            assert (await response.json())["rule"]["enabled"] is False

    asyncio.run(scenario())


def test_api_requires_admin_auth(tmp_path):
    feed = FakeSessionStore()
    service, _ = make_service(tmp_path, feed)
    server = FakeServer(service, admin=False)

    async def scenario():
        async with api_client(server) as client:
            response = await client.get("/hal/status")
            assert response.status == 401

    asyncio.run(scenario())


def test_api_runs_listing(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    server = FakeServer(service)
    rule = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "hi".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event()
    asyncio.run(service._process_owner("user:7"))

    async def scenario():
        async with api_client(server) as client:
            response = await client.get("/hal/runs")
            payload = await response.json()
            assert response.status == 200
            assert len(payload["runs"]) == 1
            assert payload["runs"][0]["state"] == "completed"
            assert payload["runs"][0]["trigger_agent"] == "@lily@TESTINST"

    asyncio.run(scenario())


# --- Round 4: A1 atomic enable ----------------------------------------------


def test_enable_after_edit_with_stale_revision_conflicts(tmp_path):
    """Enable racing an edit must never leave the old script enabled."""
    feed = FakeSessionStore()
    service, _ = make_service(tmp_path, feed)
    rule = make_rule(
        service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "v1".'
    )
    # An edit bumps revision 1 -> 2 and returns the rule to draft.
    service.upsert_rule(
        owner_id="user:7", rule_id=rule["rule_id"], name="rule",
        script='if @agent_any #turn_finish then @alice $prompt "v2".',
        expected_revision=1,
    )
    # A stale enable (expects revision 1) must conflict, never enable v1.
    with pytest.raises(HalStoreError):
        service.set_enabled(
            owner_id="user:7", rule_id=rule["rule_id"], enabled=True,
            expected_revision=1,
        )
    r = service.store.get_rule("user:7", rule["rule_id"])
    assert r["enabled"] == 0
    assert '"v2"' in r["script"]
    assert r["enable_cursor"] is None


# --- Round 5: A2 continuous workflow recovery ---------------------------------


def test_continuous_workflow_recovers_before_matching_correlated_event(tmp_path):
    """Crash after the stage-0 receipt write but before the stage advance:
    recovery repairs the stage first, then the persisted correlated completion
    event advances stage 1 exactly once — never re-dispatching stage 0."""
    feed = FakeSessionStore()
    path = tmp_path / "hal" / "hal_v1.sqlite"

    store1 = HalStore(path)
    service1 = HalService(
        store=store1, session_store=feed, dispatch=RecordingDispatchAdapter(),
        logger=logging.getLogger("hal.test.crash1"),
    )
    script = (
        'if @agent_any #turn_finish then @lily $prompt "start"; '
        'if @alice@TESTINST #turn_finish then @bob $prompt "next".'
    )
    rule = make_rule(service1, "user:7", script)
    service1.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)

    # Simulate the crash window: stage-0 receipt persisted (dispatched) but the
    # execution stage was never advanced.
    store1.create_exec(
        owner_id="user:7",
        rule_id=rule["rule_id"],
        rule_revision=rule["revision"],
        rule_name="rule",
        workflow_index=0,
        exec_id="hx1",
        trigger_event_id="evt-1",
        trigger_agent="@lily@TESTINST",
        trigger_kind="run.completed",
        program_blob=compile_hal(script),
    )
    store1.upsert_receipt(
        exec_id="hx1",
        stage=0,
        idempotency_key="hal:hx1:0",
        kind="prompt",
        target="@lily",
        payload='$prompt "start"',
        state="dispatched",
        detail=None,
    )
    assert store1.get_exec("user:7", "hx1")["stage"] == 0
    store1.close()

    # The correlated completion event (from @alice@TESTINST) is already persisted.
    feed.add_event(agent_id="alice", key="hal:hx1:0")

    adapter2 = RecordingDispatchAdapter()
    store2 = HalStore(path)
    service2 = HalService(
        store=store2, session_store=feed, dispatch=adapter2,
        logger=logging.getLogger("hal.test.crash2"),
    )
    run(service2._process_owner("user:7"))

    # Stage 0 was NOT re-dispatched; stage 1 fired exactly once.
    assert len(adapter2.prompt_calls) == 1
    assert adapter2.prompt_calls[0]["target_agent"] == "bob"
    execs = service2.store.list_execs("user:7")
    assert execs[0]["stage"] == 2
    assert execs[0]["state"] == "completed"
    store2.close()
def test_pending_receipt_with_completed_run_recovers_once(tmp_path):
    """The stage-0 prompt was accepted and its Run completed, but HAL crashed
    before writing the 'dispatched' receipt (receipt stuck in pending_retry).
    Recovery must honour the persisted completion: do NOT re-dispatch stage 0,
    preserve the correlation key, and advance stage 1 exactly once."""
    feed = FakeSessionStore()
    path = tmp_path / "hal" / "hal_v1.sqlite"
    script = (
        'if @agent_any #turn_finish then @lily $prompt "start"; '
        'if @alice@TESTINST #turn_finish then @bob $prompt "next".'
    )

    store1 = HalStore(path)
    service1 = HalService(
        store=store1, session_store=feed, dispatch=RecordingDispatchAdapter(),
        logger=logging.getLogger("hal.test.pending1"),
    )
    rule = make_rule(service1, "user:7", script)
    service1.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    store1.create_exec(
        owner_id="user:7",
        rule_id=rule["rule_id"],
        rule_revision=rule["revision"],
        rule_name="rule",
        workflow_index=0,
        exec_id="hx1",
        trigger_event_id="evt-1",
        trigger_agent="@lily@TESTINST",
        trigger_kind="run.completed",
        program_blob=compile_hal(script),
    )
    # Crash window: the pre-dispatch 'pending_retry' receipt was written but the
    # 'dispatched' write never happened, even though the prompt was accepted.
    store1.upsert_receipt(
        exec_id="hx1",
        stage=0,
        idempotency_key="hal:hx1:0",
        kind="prompt",
        target="@lily",
        payload='$prompt "start"',
        state="pending_retry",
        detail=None,
        attempts=0,
        next_retry_at=None,
    )
    assert store1.get_exec("user:7", "hx1")["stage"] == 0
    store1.close()

    # The correlated completion evidence is already persisted (the prompt's Run
    # finished under the same idempotency key).
    feed.add_event(agent_id="alice", key="hal:hx1:0")

    adapter2 = RecordingDispatchAdapter()
    store2 = HalStore(path)
    service2 = HalService(
        store=store2, session_store=feed, dispatch=adapter2,
        logger=logging.getLogger("hal.test.pending2"),
    )
    run(service2._process_owner("user:7"))

    # Stage 0 must NOT be re-dispatched: the only dispatch is stage 1 ("next").
    assert [c["target_agent"] for c in adapter2.prompt_calls] == ["bob"]
    assert adapter2.prompt_calls[0]["idempotency_key"] == "hal:hx1:1"
    execs = service2.store.list_execs("user:7")
    assert execs[0]["stage"] == 2
    assert execs[0]["state"] == "completed"
    store2.close()
# HASHI-Fingerprint: hfp_9717a97af9924e26925d98bfde62bb3d
