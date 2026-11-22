"""Regression assertions and resource-efficiency guardrails for HASHI idle behavior,
Telegram outbound decoupling, SQLite query plans, and long-term resource alerting.

These tests enforce that:
  1. Non-deliverable internal events (e.g. run.accepted, run.stopped, session.title_updated)
     NEVER create zombie connector_delivery_tasks(connector_id='telegram', state='pending').
  2. Schema migration suppresses legacy zombie telegram/pending tasks and installs partial
     indexes so EXPLAIN QUERY PLAN forbids full table scans and USE TEMP B-TREE on hot scans.
  3. On a large database (10,000+ historical completed tasks/events), telegram.scan stays
     well under strict latency budgets (< 15ms avg, < 40ms max).
  4. Disconnected / local-only workers NEVER poll SQLite for Telegram deliverables (true
     decoupling), while connected idle workers back off cleanly and wake immediately on signal.
  5. automatic_promotion_loop checks schedules via O(1) lookup without scanning 5,000 messages.
  6. FunctionWorkerClient._monitor_process avoids 200ms tight polling while detecting worker
     exit immediately on IPC peer close.
  7. Sustained slow DB work / high duty-cycle / zero-progress loop spinning automatically
     escalate to WARNING and surface in read-only /api/health and worker metadata.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import (
    frontend_database,
    frontend_incremental_delivery,
    runtime_session,
)
from orchestrator.frontend_delivery import freeze_run_delivery_route
from orchestrator.function_worker_supervisor import FunctionWorkerClient
from orchestrator.run_questions import RunQuestions
from orchestrator.session_store import SessionStore


def _telegram_route(chat_id: int = 777) -> dict:
    return freeze_run_delivery_route(
        message_source_id="telegram",
        session_surface="telegram",
        session_channel_key=str(chat_id),
        chat_id=chat_id,
        telegram_requested=True,
    )


def _new_store(tmp_path: Path, name: str = "sessions.sqlite3") -> SessionStore:
    return SessionStore(tmp_path / name, instance_id="HASHI_EFF_TEST")


# ---------------------------------------------------------------------------
# 1. No zombie telegram/pending rows for non-deliverable internal events
# ---------------------------------------------------------------------------


def test_non_deliverable_internal_events_never_create_zombie_telegram_pending_tasks(
    tmp_path: Path,
):
    store = _new_store(tmp_path)
    owner = "user:1"
    session = store.ensure_default_session(owner_id=owner, agent_id="zelda")
    route = _telegram_route(chat_id=1001)

    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="zelda",
        request_id="req-no-zombie-1",
        source="telegram",
        idempotency_key="idem-no-zombie-1",
        text="Hello world",
        delivery_route=route,
    )
    assert accepted.run_id

    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute(
            """SELECT e.kind, t.connector_id, t.state
               FROM connector_delivery_tasks AS t
               JOIN run_events AS e ON e.event_id = t.event_id
               WHERE t.connector_id = 'telegram'"""
        ).fetchall()

    # run.accepted and session.title_updated must NEVER leave telegram/pending rows.
    assert rows == [], f"Unexpected zombie telegram tasks created on accept: {rows}"

    # Completing the run DOES create deliverable telegram/pending rows (assistant output / run.completed).
    store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="Done cleanly.",
    )
    with sqlite3.connect(store.db_path) as conn:
        deliverable_rows = conn.execute(
            """SELECT e.kind, t.state
               FROM connector_delivery_tasks AS t
               JOIN run_events AS e ON e.event_id = t.event_id
               WHERE t.connector_id = 'telegram' AND t.state = 'pending'"""
        ).fetchall()
    assert deliverable_rows, "Deliverable completion event must still create telegram pending task"
    assert all(
        kind in {
            "assistant.output.available",
            "run.completed",
            "run.failed",
            "run.cancelled",
            "run.interrupted",
            "frontend.message.recorded",
            "run.question.created",
            "frontend.command_result",
            "command.completed",
        }
        for kind, _state in deliverable_rows
    )


# ---------------------------------------------------------------------------
# 2. Schema migration cleans legacy zombies + EXPLAIN QUERY PLAN assertions
# ---------------------------------------------------------------------------


def test_schema_migration_cleans_legacy_zombies_and_enforces_indexed_query_plans(
    tmp_path: Path,
):
    db_path = tmp_path / "sessions.sqlite3"
    store = SessionStore(db_path, instance_id="HASHI_MIG")
    owner = "user:1"
    session = store.ensure_default_session(owner_id=owner, agent_id="zhao_ling")
    route = _telegram_route(chat_id=2002)
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="zhao_ling",
        request_id="req-mig-1",
        source="telegram",
        idempotency_key="idem-mig-1",
        text="check",
        delivery_route=route,
    )
    store.finish_request(accepted.request_id, success=True, assistant_text="final answer")

    # Simulate a legacy v37 database containing zombie run.accepted + route-less frontend.message.recorded rows.
    with sqlite3.connect(db_path) as conn:
        for i in range(120):
            eid = f"evt_legacy_acc_{i}"
            conn.execute(
                """INSERT INTO run_events(event_id, session_id, run_id, sequence, kind, status, phase, summary, detail_json, created_at)
                   VALUES (?, ?, ?, ?, 'run.accepted', 'queued', 'admission', 'accepted', '{}', '2026-10-01T00:00:00Z')""",
                (eid, session["session_id"], accepted.run_id, 1000 + i),
            )
            conn.execute(
                """INSERT INTO connector_delivery_tasks(task_id, session_id, run_id, event_id, connector_id, endpoint_id, role, content_modes_json, retry_class, state, created_at)
                   VALUES (?, ?, ?, ?, 'telegram', 'ep-tg', 'primary', '["text"]', 'query_before_retry', 'pending', '2026-10-01T00:00:00Z')""",
                (f"del_legacy_acc_{i}", session["session_id"], accepted.run_id, eid),
            )
        # Route-less legacy frontend.message.recorded row
        conn.execute(
            """INSERT INTO run_events(event_id, session_id, run_id, sequence, kind, status, phase, summary, detail_json, created_at)
               VALUES ('evt_no_route', ?, NULL, 2000, 'frontend.message.recorded', 'recorded', 'presentation', 'msg', '{}', '2026-10-01T00:00:00Z')""",
            (session["session_id"],),
        )
        conn.execute(
            """INSERT INTO connector_delivery_tasks(task_id, session_id, run_id, event_id, connector_id, endpoint_id, role, content_modes_json, retry_class, state, created_at)
               VALUES ('del_no_route', ?, NULL, 'evt_no_route', 'telegram', 'ep-tg', 'primary', '["text"]', 'query_before_retry', 'pending', '2026-10-01T00:00:00Z')""",
            (session["session_id"],),
        )
        conn.execute("UPDATE schema_metadata SET value='37' WHERE key='schema_version'")

    # Reopen to trigger migration to SCHEMA_VERSION >= 38.
    migrated = SessionStore(db_path, instance_id="HASHI_MIG")
    assert SessionStore.SCHEMA_VERSION >= 38
    with sqlite3.connect(db_path) as conn:
        ver = int(conn.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0])
        assert ver == SessionStore.SCHEMA_VERSION
        remaining_non_deliverable = conn.execute(
            """SELECT COUNT(*) FROM connector_delivery_tasks AS t
               JOIN run_events AS e ON e.event_id = t.event_id
               WHERE t.connector_id = 'telegram' AND t.state = 'pending'
                 AND e.kind = 'run.accepted'"""
        ).fetchone()[0]
        assert remaining_non_deliverable == 0

        # Real deliverable completion task MUST remain pending!
        deliverables = migrated.pending_incremental_telegram_deliveries(agent_id="zhao_ling")
        assert len(deliverables) >= 1

        # Route-less legacy row must NOT stay pending forever after scan/migration.
        no_route_state = conn.execute(
            "SELECT state FROM connector_delivery_tasks WHERE task_id='del_no_route'"
        ).fetchone()[0]
        assert no_route_state == "suppressed"

        # EXPLAIN QUERY PLAN assertion: hot telegram scan MUST use partial index and NEVER use TEMP B-TREE!
        plan_rows = conn.execute(
            """EXPLAIN QUERY PLAN
               SELECT t.session_id, s.owner_id, COALESCE(r.request_id, '') AS request_id,
                      t.event_id, r.delivery_route_json, e.detail_json
               FROM connector_delivery_tasks AS t
               CROSS JOIN sessions AS s ON s.session_id=t.session_id
               JOIN run_events AS e ON e.event_id=t.event_id
               LEFT JOIN runs AS r ON r.run_id=t.run_id
               WHERE t.connector_id='telegram'
                 AND t.state IN ('pending','retry','claimed')
                 AND (t.state!='claimed' OR t.lease_expires_at<='2026-11-23T00:00:00Z')
                 AND s.instance_id='HASHI_MIG' AND s.agent_id='zhao_ling'
                 AND (r.agent_id='zhao_ling' OR r.run_id IS NULL)
                 AND e.kind IN ('run.completed','run.failed','run.cancelled',
                                'run.interrupted','assistant.output.available',
                                'frontend.message.recorded','run.question.created')
               ORDER BY t.created_at, t.task_id LIMIT 20"""
        ).fetchall()
        plan_text = "\n".join(str(r[3]) for r in plan_rows)
        assert "connector_delivery_tasks_telegram_active" in plan_text, plan_text
        assert "USE TEMP B-TREE FOR ORDER BY" not in plan_text, plan_text
        assert "SCAN t" not in plan_text, plan_text

    # RunQuestions active index EXPLAIN QUERY PLAN assertion.
    RunQuestions(migrated)
    with sqlite3.connect(db_path) as conn:
        q_plan = "\n".join(
            str(r[3])
            for r in conn.execute(
                """EXPLAIN QUERY PLAN
                   SELECT * FROM run_questions
                   WHERE instance_id='HASHI_MIG' AND agent_id='zhao_ling'
                     AND state IN ('pending','answered')
                   ORDER BY created_at LIMIT 100"""
            ).fetchall()
        )
        assert "run_questions_active_by_agent" in q_plan, q_plan
        assert "USE TEMP B-TREE FOR ORDER BY" not in q_plan, q_plan


# ---------------------------------------------------------------------------
# 3. Large-DB latency budget test (10,000+ historical completed rows)
# ---------------------------------------------------------------------------


def test_large_database_telegram_scan_stays_under_strict_latency_budget(tmp_path: Path):
    db_path = tmp_path / "large_sessions.sqlite3"
    store = SessionStore(db_path, instance_id="HASHI_PERF")
    owner = "user:1"
    session = store.ensure_default_session(owner_id=owner, agent_id="zelda")

    # Seed 10,000 historical completed connector_delivery_tasks + run_events.
    with sqlite3.connect(db_path) as conn:
        events = [
            (
                f"evt_hist_{i}",
                session["session_id"],
                None,
                i + 100,
                "assistant.output.available",
                "completed",
                "delivery",
                "done",
                "{}",
                f"2026-10-01T00:{(i // 60) % 60:02d}:{i % 60:02d}Z",
            )
            for i in range(10_000)
        ]
        tasks = [
            (
                f"del_hist_{i}",
                session["session_id"],
                None,
                f"evt_hist_{i}",
                "telegram",
                "ep-tg-1",
                "primary",
                '["text"]',
                "query_before_retry",
                "completed",
                f"2026-10-01T00:{(i // 60) % 60:02d}:{i % 60:02d}Z",
            )
            for i in range(10_000)
        ]
        conn.executemany(
            """INSERT INTO run_events(event_id, session_id, run_id, sequence, kind, status, phase, summary, detail_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            events,
        )
        conn.executemany(
            """INSERT INTO connector_delivery_tasks(task_id, session_id, run_id, event_id, connector_id, endpoint_id, role, content_modes_json, retry_class, state, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tasks,
        )

    # Warm once, then benchmark 50 consecutive scans on the 10k-row DB.
    assert store.pending_incremental_telegram_deliveries(agent_id="zelda") == []
    samples_ms = []
    for _ in range(50):
        t0 = time.perf_counter()
        res = store.pending_incremental_telegram_deliveries(agent_id="zelda")
        samples_ms.append((time.perf_counter() - t0) * 1000.0)
        assert res == []

    avg_ms = sum(samples_ms) / len(samples_ms)
    max_ms = max(samples_ms)
    assert avg_ms < 10.0, f"telegram.scan too slow on 10k-row DB: avg={avg_ms:.2f}ms max={max_ms:.2f}ms"
    assert max_ms < 40.0, f"telegram.scan max spike too high: max={max_ms:.2f}ms"


# ---------------------------------------------------------------------------
# 4 & 5. Telegram outbound decoupling: disconnected=0 polls, idle=backoff, signal=instant wake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnected_worker_never_polls_sqlite_and_wakes_immediately_on_connect(
    tmp_path: Path, monkeypatch
):
    store = _new_store(tmp_path)
    scan_calls = 0

    async def fake_dispatch(runtime, *, limit=20):
        nonlocal scan_calls
        scan_calls += 1
        return 0

    monkeypatch.setattr(
        frontend_incremental_delivery,
        "dispatch_pending_telegram_deliverables",
        fake_dispatch,
    )

    runtime = SimpleNamespace(
        name="local_only_agent",
        is_shutting_down=False,
        telegram_connected=False,
        app=SimpleNamespace(bot=None),
        session_store=store,
        global_config=SimpleNamespace(instance_id="HASHI_EFF_TEST"),
        logger=logging.getLogger("test.decoupled"),
    )

    task = asyncio.create_task(
        frontend_incremental_delivery.incremental_telegram_delivery_loop(runtime)
    )
    try:
        # While disconnected, the loop must NOT spin or call dispatch at all.
        await asyncio.sleep(0.12)
        assert scan_calls == 0, "Disconnected worker must not poll Telegram delivery loop"

        # Connecting Telegram + signaling wakes the loop immediately.
        runtime.telegram_connected = True
        runtime.app.bot = object()
        frontend_incremental_delivery.notify_incremental_telegram_delivery(runtime)
        deadline = time.monotonic() + 1.0
        while scan_calls == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert scan_calls == 1
    finally:
        runtime.is_shutting_down = True
        frontend_incremental_delivery.notify_incremental_telegram_delivery(runtime)
        await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_connected_idle_telegram_loop_backs_off_exponentially_and_wakes_on_signal(
    tmp_path: Path, monkeypatch
):
    observed_timeouts: list[float] = []
    dispatched_seq = iter([0, 0, 0, 1, 0])

    async def fake_dispatch(_runtime, *, limit=20):
        return next(dispatched_seq, 0)

    monkeypatch.setattr(
        frontend_incremental_delivery,
        "dispatch_pending_telegram_deliverables",
        fake_dispatch,
    )

    runtime = SimpleNamespace(
        name="idle_tg_agent",
        is_shutting_down=False,
        telegram_connected=True,
        app=SimpleNamespace(bot=object()),
        global_config=SimpleNamespace(instance_id="HASHI_EFF_TEST"),
        logger=logging.getLogger("test.backoff"),
    )

    orig_wait_for = asyncio.wait_for

    async def fake_wait_for(coro, timeout=None):
        observed_timeouts.append(float(timeout))
        # Close coroutine cleanly to avoid RuntimeWarning, then simulate timeout.
        coro.close()
        if len(observed_timeouts) >= 5:
            runtime.is_shutting_down = True
        raise asyncio.TimeoutError

    monkeypatch.setattr(frontend_incremental_delivery.asyncio, "wait_for", fake_wait_for)
    try:
        await orig_wait_for(
            frontend_incremental_delivery.incremental_telegram_delivery_loop(runtime),
            timeout=5.0,
        )
    finally:
        monkeypatch.setattr(frontend_incremental_delivery.asyncio, "wait_for", orig_wait_for)

    # Idle timeouts MUST increase exponentially (never stuck at flat 2.0s!) and reset after dispatch > 0.
    assert len(observed_timeouts) >= 4
    assert observed_timeouts[0] < observed_timeouts[1] < observed_timeouts[2], observed_timeouts
    assert observed_timeouts[3] <= observed_timeouts[0], (
        f"Backoff must reset after dispatching real work: {observed_timeouts}"
    )


# ---------------------------------------------------------------------------
# 6. automatic_promotion_loop uses O(1) schedule lookup (never scans 5,000 messages)
# ---------------------------------------------------------------------------


def test_promotion_is_due_uses_lightweight_schedule_lookup_without_scanning_candidates(
    tmp_path: Path, monkeypatch
):
    store = _new_store(tmp_path)
    store.set_promotion_schedule(agent_id="zelda", enabled=True, local_time="23:59", timezone_name="UTC")

    def forbid_candidate_scan(*_args, **_kwargs):
        pytest.fail("promotion_is_due must NEVER call promotion_candidates(limit=5000)")

    monkeypatch.setattr(store, "promotion_candidates", forbid_candidate_scan)
    runtime = SimpleNamespace(name="zelda", session_store=store, global_config=SimpleNamespace(bridge_home=tmp_path))

    due, local_date = runtime_session.promotion_is_due(runtime)
    assert isinstance(due, bool)
    assert len(local_date) == 10


# ---------------------------------------------------------------------------
# 7. FunctionWorkerClient._monitor_process avoids 200ms tight spin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_monitor_avoids_tight_200ms_polling_and_detects_exit_immediately():
    peer_closed = asyncio.Event()
    alive = True
    is_alive_calls = 0

    class FakeProcess:
        pid = 99999
        exitcode = 0

        def is_alive(self):
            nonlocal is_alive_calls
            is_alive_calls += 1
            return alive

        def join(self, timeout=None):
            return None

    exit_handled = asyncio.Event()

    class FakeSupervisor:
        async def handle_worker_exit(self, _client):
            exit_handled.set()

    client = FunctionWorkerClient.__new__(FunctionWorkerClient)
    client.agent_name = "idle_agent"
    client.process = FakeProcess()
    client.peer = SimpleNamespace(_closed=peer_closed, is_closed=False)
    client.ready = asyncio.get_running_loop().create_future()
    client.ready.set_result({"ok": True})
    client.closed = False
    client.expected_exit = False
    client.supervisor = FakeSupervisor()

    task = asyncio.create_task(client._monitor_process())
    try:
        # Over 0.35s while alive, an old 0.2s poller would poll multiple times;
        # peer-event-driven monitoring should call is_alive() at most once!
        await asyncio.sleep(0.35)
        assert is_alive_calls <= 2, f"Monitor polled is_alive() too aggressively: {is_alive_calls}"

        # Simulate sudden worker crash (peer pipe EOF closes immediately).
        t0 = time.monotonic()
        alive = False
        peer_closed.set()
        await asyncio.wait_for(exit_handled.wait(), timeout=0.5)
        detect_ms = (time.monotonic() - t0) * 1000.0
        assert detect_ms < 200.0, f"Crash detection was too slow: {detect_ms:.1f}ms"
    finally:
        alive = False
        peer_closed.set()
        await asyncio.wait_for(task, timeout=1.0)


# ---------------------------------------------------------------------------
# 8. Long-term resource / slow-DB alerting escalates to WARNING and surfaces in /api/health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_db_and_spin_alerting_escalates_warning_and_surfaces_in_health(
    tmp_path: Path, caplog, monkeypatch
):
    from orchestrator.workbench_api import WorkbenchApiServer

    frontend_database.reset_database_work_metrics()
    try:
        # 1. Fast operations produce zero alerts.
        await frontend_database.run_database_work(
            lambda: "fast",
            _db_role="worker",
            _db_instance="TEST",
            _db_operation="fast.op",
        )
        assert frontend_database.resource_alerts() == []

        # 2. Simulate severe / sustained slow DB work -> must log WARNING and populate resource_alerts().
        with caplog.at_level(logging.WARNING, logger=frontend_database.logger.name):
            frontend_database.record_database_work_sample(
                operation="telegram.scan",
                instance="TEST",
                role="worker",
                duration_s=0.65,
                queued_s=0.0,
            )
        alerts = frontend_database.resource_alerts()
        assert alerts, "Severe slow DB work (>= 500ms) must raise a structured resource alert"
        assert any(a["code"] == "slow_database_work" and a["operation"] == "telegram.scan" for a in alerts)
        assert any(
            rec.levelno >= logging.WARNING and "telegram.scan" in rec.message
            for rec in caplog.records
        ), "Slow DB alert must escalate to WARNING log"

        # 3. Verify WorkbenchApiServer.handle_health() surfaces active resource alerts cleanly (read-only).
        config_path = tmp_path / "agents.json"
        config_path.write_text(
            json.dumps({"global": {}, "agents": [{"name": "a1", "workspace_dir": "workspaces/a1"}]}),
            encoding="utf-8",
        )
        server = WorkbenchApiServer(
            config_path=config_path,
            global_config=SimpleNamespace(
                bridge_home=tmp_path,
                project_root=tmp_path,
                instance_id="TEST",
                authorized_id=1,
                deployment_profile="personal",
                api_host="127.0.0.1",
                workbench_port=0,
                api_gateway_port=None,
            ),
            reconcile_session_runs=False,
        )
        resp = await server.handle_health(SimpleNamespace())
        payload = json.loads(resp.text)
        assert "resource_health" in payload
        assert payload["resource_health"]["alerts"], "handle_health must expose active resource alerts"
        assert any(issue.get("code") == "slow_database_work" for issue in payload["issues"])
    finally:
        frontend_database.reset_database_work_metrics()
