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

        # EXPLAIN QUERY PLAN assertions: both active-session discovery and per-session
        # top-K retrieval MUST use connector_delivery_tasks_telegram_active (session_id=?)
        # with ZERO full table scan and ZERO TEMP B-TREE!
        active_plan_text = "\n".join(
            str(r[3])
            for r in conn.execute(
                """EXPLAIN QUERY PLAN
                   SELECT s.session_id, s.owner_id
                   FROM sessions AS s
                   WHERE s.instance_id='HASHI_MIG' AND s.agent_id='zhao_ling'
                     AND EXISTS (
                         SELECT 1 FROM connector_delivery_tasks AS t
                         WHERE t.session_id=s.session_id
                           AND t.connector_id='telegram'
                           AND t.state IN ('pending','retry','claimed')
                           AND (t.state!='claimed' OR t.lease_expires_at<='2026-11-23T00:00:00Z')
                     )"""
            ).fetchall()
        )
        assert "connector_delivery_tasks_telegram_active (session_id=? AND connector_id=?)" in active_plan_text, active_plan_text
        assert "SCAN t" not in active_plan_text, active_plan_text

        plan_rows = conn.execute(
            """EXPLAIN QUERY PLAN
               SELECT t.task_id, t.session_id, t.created_at,
                      COALESCE(r.request_id, '') AS request_id,
                      t.event_id, r.delivery_route_json, e.detail_json
               FROM connector_delivery_tasks AS t
               JOIN run_events AS e ON e.event_id=t.event_id
               LEFT JOIN runs AS r ON r.run_id=t.run_id
               WHERE t.session_id=?
                 AND t.connector_id='telegram'
                 AND t.state IN ('pending','retry','claimed')
                 AND (t.state!='claimed' OR t.lease_expires_at<='2026-11-23T00:00:00Z')
                 AND (r.agent_id='zhao_ling' OR r.run_id IS NULL)
                 AND e.kind IN ('run.completed','run.failed','run.cancelled',
                                'run.interrupted','assistant.output.available',
                                'frontend.message.recorded','run.question.created')
               ORDER BY t.created_at, t.task_id LIMIT 20""",
            (session["session_id"],),
        ).fetchall()
        plan_text = "\n".join(str(r[3]) for r in plan_rows)
        assert "connector_delivery_tasks_telegram_active (session_id=? AND connector_id=?)" in plan_text, plan_text
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
        # Performance warnings MUST NOT break normal service availability contract!
        assert payload["ready"] is True
        assert payload["status"] == "ready"
        assert payload["degraded"] is False
    finally:
        frontend_database.reset_database_work_metrics()


# ---------------------------------------------------------------------------
# 9. P2 Multi-Agent SQLite scan isolation across 6 stress/correctness scenarios
# ---------------------------------------------------------------------------


def test_multi_agent_telegram_scan_isolation_and_ordering(tmp_path: Path):
    db_path = tmp_path / "multi_agent.sqlite3"
    store = SessionStore(db_path, instance_id="HASHI_ISO")
    owner = "user:1"
    sess_a1 = store.ensure_default_session(owner_id=owner, agent_id="agent_a")
    sess_b1 = store.ensure_default_session(owner_id=owner, agent_id="agent_b")
    sess_c1 = store.ensure_default_session(owner_id=owner, agent_id="agent_c")
    sess_c2 = store.create_session(owner_id=owner, agent_id="agent_c", title="second")
    sess_d1 = store.ensure_default_session(owner_id=owner, agent_id="agent_d")
    sess_e1 = store.ensure_default_session(owner_id=owner, agent_id="agent_e")

    route_json = json.dumps({"delivery_route": _telegram_route(chat_id=9001)})

    with sqlite3.connect(db_path) as conn:
        # 1) 10,000 completed historical rows
        conn.executemany(
            """INSERT INTO run_events(event_id, session_id, run_id, sequence, kind, status, phase, summary, detail_json, created_at)
               VALUES (?, ?, NULL, ?, 'assistant.output.available', 'completed', 'delivery', 'done', ?, ?)""",
            [(f"ev_comp_{i}", sess_a1["session_id"], 100 + i, route_json, f"2026-09-01T00:{(i // 60) % 60:02d}:{i % 60:02d}Z") for i in range(10_000)],
        )
        conn.executemany(
            """INSERT INTO connector_delivery_tasks(task_id, session_id, run_id, event_id, connector_id, endpoint_id, role, content_modes_json, retry_class, state, created_at)
               VALUES (?, ?, NULL, ?, 'telegram', 'ep-tg-a', 'primary', '["text"]', 'query_before_retry', 'completed', ?)""",
            [(f"del_comp_{i}", sess_a1["session_id"], f"ev_comp_{i}", f"2026-09-01T00:{(i // 60) % 60:02d}:{i % 60:02d}Z") for i in range(10_000)],
        )
        # 2) 20,000 active pending rows belonging ONLY to Agent A
        conn.executemany(
            """INSERT INTO run_events(event_id, session_id, run_id, sequence, kind, status, phase, summary, detail_json, created_at)
               VALUES (?, ?, NULL, ?, 'assistant.output.available', 'completed', 'delivery', 'pending', ?, ?)""",
            [(f"ev_a_{i}", sess_a1["session_id"], 20_000 + i, route_json, f"2026-10-{(i // 86400) + 1:02d}T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z") for i in range(20_000)],
        )
        conn.executemany(
            """INSERT INTO connector_delivery_tasks(task_id, session_id, run_id, event_id, connector_id, endpoint_id, role, content_modes_json, retry_class, state, created_at)
               VALUES (?, ?, NULL, ?, 'telegram', 'ep-tg-a', 'primary', '["text"]', 'query_before_retry', 'pending', ?)""",
            [(f"del_a_{i:05d}", sess_a1["session_id"], f"ev_a_{i}", f"2026-10-{(i // 86400) + 1:02d}T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z") for i in range(20_000)],
        )
        # 3) Agent C: interleaved pending tasks across 2 sessions (verify exact global created_at/task_id order + limit)
        c_rows = [
            ("ev_c_1", sess_c1["session_id"], 100, "del_c_1", "2026-11-01T00:00:01Z"),
            ("ev_c_2", sess_c2["session_id"], 100, "del_c_2", "2026-11-01T00:00:02Z"),
            ("ev_c_3", sess_c1["session_id"], 101, "del_c_3", "2026-11-01T00:00:03Z"),
            ("ev_c_4", sess_c2["session_id"], 101, "del_c_4", "2026-11-01T00:00:04Z"),
            ("ev_c_5", sess_c1["session_id"], 102, "del_c_5", "2026-11-01T00:00:05Z"),
        ]
        for eid, sid, seq, tid, ts in c_rows:
            conn.execute(
                """INSERT INTO run_events(event_id, session_id, run_id, sequence, kind, status, phase, summary, detail_json, created_at)
                   VALUES (?, ?, NULL, ?, 'assistant.output.available', 'completed', 'delivery', 'c', ?, ?)""",
                (eid, sid, seq, route_json, ts),
            )
            conn.execute(
                """INSERT INTO connector_delivery_tasks(task_id, session_id, run_id, event_id, connector_id, endpoint_id, role, content_modes_json, retry_class, state, created_at)
                   VALUES (?, ?, NULL, ?, 'telegram', 'ep-tg-c', 'primary', '["text"]', 'query_before_retry', 'pending', ?)""",
                (tid, sid, eid, ts),
            )
        # 4) Agent D: expired claimed + retry + unexpired claimed + unknown (unknown MUST NEVER be returned!)
        d_rows = []
        for i in range(400):
            eid = f"ev_d_exp_{i}"
            conn.execute(
                """INSERT INTO run_events(event_id, session_id, run_id, sequence, kind, status, phase, summary, detail_json, created_at)
                   VALUES (?, ?, NULL, ?, 'assistant.output.available', 'completed', 'delivery', 'd', ?, ?)""",
                (eid, sess_d1["session_id"], 100 + i, route_json, f"2026-11-02T00:{(i // 60) % 60:02d}:{i % 60:02d}Z"),
            )
            state = "claimed" if i % 2 == 0 else "retry"
            lease = "2020-01-01T00:00:00Z" if state == "claimed" else None
            d_rows.append((f"del_d_exp_{i:04d}", sess_d1["session_id"], eid, state, lease, f"2026-11-02T00:{(i // 60) % 60:02d}:{i % 60:02d}Z"))
        conn.executemany(
            """INSERT INTO connector_delivery_tasks(task_id, session_id, run_id, event_id, connector_id, endpoint_id, role, content_modes_json, retry_class, state, lease_expires_at, created_at)
               VALUES (?, ?, NULL, ?, 'telegram', 'ep-tg-d', 'primary', '["text"]', 'query_before_retry', ?, ?, ?)""",
            d_rows,
        )
        # Add an unexpired claimed row and an unknown row for Agent D
        for seq, eid, tid, st, lease in (
            (9000, "ev_d_active_claim", "del_d_active_claim", "claimed", "2099-01-01T00:00:00Z"),
            (9001, "ev_d_unknown", "del_d_unknown", "unknown", None),
        ):
            conn.execute(
                """INSERT INTO run_events(event_id, session_id, run_id, sequence, kind, status, phase, summary, detail_json, created_at)
                   VALUES (?, ?, NULL, ?, 'assistant.output.available', 'completed', 'delivery', 'd', ?, '2026-11-01T00:00:00Z')""",
                (eid, sess_d1["session_id"], seq, route_json),
            )
            conn.execute(
                """INSERT INTO connector_delivery_tasks(task_id, session_id, run_id, event_id, connector_id, endpoint_id, role, content_modes_json, retry_class, state, lease_expires_at, created_at)
                   VALUES (?, ?, NULL, ?, 'telegram', 'ep-tg-d', 'primary', '["text"]', 'query_before_retry', ?, ?, '2026-11-01T00:00:00Z')""",
                (tid, sess_d1["session_id"], eid, st, lease),
            )

    # Scenario A: Agent B has ZERO pending while Agent A has 20,000 pending + 10,000 completed -> must be < 5ms!
    t0 = time.perf_counter()
    for _ in range(50):
        assert store.pending_incremental_telegram_deliveries(agent_id="agent_b") == []
    avg_b_ms = ((time.perf_counter() - t0) / 50.0) * 1000.0
    assert avg_b_ms < 5.0, f"Agent B scan impacted by Agent A's 20k backlog: avg={avg_b_ms:.2f}ms"

    # Scenario B: Agent A with 20,000 active pending returns exact top 20 in created_at order rapidly!
    t0 = time.perf_counter()
    a_items = store.pending_incremental_telegram_deliveries(agent_id="agent_a", limit=20)
    a_ms = (time.perf_counter() - t0) * 1000.0
    assert len(a_items) == 20
    assert [it["event_id"] for it in a_items[:3]] == ["ev_a_0", "ev_a_1", "ev_a_2"]
    assert a_ms < 25.0, f"Agent A 20k-pending top-20 query too slow: {a_ms:.2f}ms"

    # Scenario C: Multi-session Agent C preserves global (created_at, task_id) order and exact limit
    c_top3 = store.pending_incremental_telegram_deliveries(agent_id="agent_c", limit=3)
    assert [it["event_id"] for it in c_top3] == ["ev_c_1", "ev_c_2", "ev_c_3"]
    c_all = store.pending_incremental_telegram_deliveries(agent_id="agent_c", limit=20)
    assert [it["event_id"] for it in c_all] == ["ev_c_1", "ev_c_2", "ev_c_3", "ev_c_4", "ev_c_5"]

    # Scenario D: Expired claimed + retry returned; unexpired claimed & unknown NEVER returned
    d_items = store.pending_incremental_telegram_deliveries(agent_id="agent_d", limit=10)
    assert len(d_items) == 10
    d_ids = {it["event_id"] for it in d_items}
    assert "ev_d_unknown" not in d_ids
    assert "ev_d_active_claim" not in d_ids

    # Scenario E: Zero-task Agent E
    assert store.pending_incremental_telegram_deliveries(agent_id="agent_e") == []


# ---------------------------------------------------------------------------
# PIT A: Connected idle worker (in 60s backoff) wakes IMMEDIATELY on new output
#        (strictly post-commit; rolled-back writes never wake)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pit_a_idle_worker_in_60s_backoff_wakes_immediately_on_committed_output(
    tmp_path: Path, monkeypatch
):
    store = _new_store(tmp_path)
    owner = "user:1"
    session = store.ensure_default_session(owner_id=owner, agent_id="zelda")
    route = _telegram_route(chat_id=1001)
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="zelda",
        request_id="req-pit-a",
        source="telegram",
        idempotency_key="idem-pit-a",
        text="hello",
        delivery_route=route,
    )

    delivered_events: list[str] = []
    delivered_event = asyncio.Event()

    async def fake_dispatch(rt, *, limit=20):
        rows = rt.session_store.pending_incremental_telegram_deliveries(agent_id=rt.name, limit=limit)
        if not rows:
            return 0
        for row in rows:
            delivered_events.append(row["event_id"])
        delivered_event.set()
        return len(rows)

    monkeypatch.setattr(frontend_incremental_delivery, "dispatch_pending_telegram_deliverables", fake_dispatch)
    # Force base idle delay to 60s to prove delivery wakes from signal, NOT timer!
    monkeypatch.setattr(frontend_incremental_delivery, "IDLE_SWEEP_BASE_SECONDS", 60.0)

    runtime = SimpleNamespace(
        name="zelda",
        is_shutting_down=False,
        telegram_connected=True,
        app=SimpleNamespace(bot=object()),
        session_store=store,
        global_config=SimpleNamespace(instance_id="HASHI_EFF_TEST"),
        logger=logging.getLogger("test.pit_a"),
    )

    frontend_incremental_delivery.start_incremental_telegram_delivery(runtime)
    try:
        # Let initial sweep finish and enter the 60s wait.
        await asyncio.sleep(0.05)
        assert delivered_events == []

        # Verify rollback NEVER wakes the loop!
        with pytest.raises(RuntimeError, match="abort"):
            with store._connection() as conn:
                store._append_event(
                    conn,
                    session_id=session["session_id"],
                    run_id=accepted.run_id,
                    kind="assistant.output.available",
                    status="completed",
                    phase="delivery",
                    summary="rolled back",
                    detail={"delivery_route": route},
                    outbox=True,
                )
                raise RuntimeError("abort")
        await asyncio.sleep(0.05)
        assert not delivered_event.is_set()

        # Commit genuine output -> must wake and deliver in << 1s (NOT 60s)!
        t0 = time.monotonic()
        store.finish_request(accepted.request_id, success=True, assistant_text="Instant delivery!")
        await asyncio.wait_for(delivered_event.wait(), timeout=1.0)
        latency_ms = (time.monotonic() - t0) * 1000.0
        assert delivered_events, "Expected committed output to be delivered"
        assert latency_ms < 500.0, f"Delivery waited too long: {latency_ms:.1f}ms"
    finally:
        runtime.is_shutting_down = True
        frontend_incremental_delivery.unbind_telegram_delivery_wakeup(runtime)
        frontend_incremental_delivery.notify_incremental_telegram_delivery(runtime)
        await asyncio.wait_for(runtime._incremental_telegram_delivery_task, timeout=1.0)


# ---------------------------------------------------------------------------
# PIT B: Cross-process output wakes owning Worker over existing IPC pipe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pit_b_cross_process_output_wakes_worker_via_existing_ipc(
    tmp_path: Path, monkeypatch
):
    import multiprocessing
    from orchestrator.function_worker_host import FunctionWorkerHost
    from orchestrator.function_worker_protocol import JsonConnectionPeer
    from orchestrator.function_worker_supervisor import AgentRuntimeHandle, FunctionWorkerSupervisor
    from orchestrator.workbench_api import WorkbenchApiServer

    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps({"global": {}, "agents": [{"name": "zelda", "workspace_dir": "workspaces/zelda"}]}),
        encoding="utf-8",
    )
    global_cfg = SimpleNamespace(
        bridge_home=tmp_path,
        project_root=tmp_path,
        instance_id="HASHI_PIT_B",
        authorized_id=1,
        deployment_profile="personal",
        api_host="127.0.0.1",
        workbench_port=0,
        api_gateway_port=None,
    )

    # Worker side with real JsonConnectionPeer over multiprocessing.Pipe
    worker_store = SessionStore(tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI_PIT_B")
    delivered = asyncio.Event()

    async def fake_dispatch(rt, *, limit=20):
        rows = rt.session_store.pending_incremental_telegram_deliveries(agent_id=rt.name, limit=limit)
        if rows:
            delivered.set()
            return len(rows)
        return 0

    monkeypatch.setattr(frontend_incremental_delivery, "dispatch_pending_telegram_deliverables", fake_dispatch)
    monkeypatch.setattr(frontend_incremental_delivery, "IDLE_SWEEP_BASE_SECONDS", 60.0)

    worker_runtime = SimpleNamespace(
        name="zelda",
        is_shutting_down=False,
        telegram_connected=True,
        app=SimpleNamespace(bot=object()),
        session_store=worker_store,
        global_config=global_cfg,
        logger=logging.getLogger("test.pit_b.worker"),
    )
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.agent_name = "zelda"
    host.runtime = worker_runtime
    host.facade = None
    host.phase = "ACTIVE"

    left, right = multiprocessing.Pipe()
    core_peer = JsonConnectionPeer(left, label="pit-b-core")
    worker_peer = JsonConnectionPeer(
        right,
        label="pit-b-worker",
        request_handler=host.handle_request,
        event_handler=host.handle_event,
    )
    core_peer.start()
    worker_peer.start()

    client = SimpleNamespace(agent_name="zelda", peer=core_peer, closed=False)
    handle = AgentRuntimeHandle.__new__(AgentRuntimeHandle)
    handle.name = "zelda"
    handle._client = client
    handle.metadata = {"telegram_connected": True}

    supervisor = FunctionWorkerSupervisor.__new__(FunctionWorkerSupervisor)
    supervisor.kernel = SimpleNamespace(_runtime_map=lambda: {"zelda": handle})
    orchestrator = SimpleNamespace(function_workers=supervisor, runtimes=[handle])

    # Shared Functions / Workbench API side (separate SessionStore instance!)
    server = WorkbenchApiServer(
        config_path=config_path,
        global_config=global_cfg,
        orchestrator=orchestrator,
        reconcile_session_runs=False,
    )

    frontend_incremental_delivery.start_incremental_telegram_delivery(worker_runtime)
    try:
        await asyncio.sleep(0.05)
        assert not delivered.is_set()

        # Persist output from WorkbenchApiServer's separate SessionStore (simulating functions_host process)
        session = server.session_store.ensure_default_session(owner_id="user:1", agent_id="zelda")
        accepted = server.session_store.accept_run(
            session_id=session["session_id"],
            owner_id="user:1",
            agent_id="zelda",
            request_id="req-pit-b",
            source="workbench",
            idempotency_key="idem-pit-b",
            text="cross process",
            delivery_route=_telegram_route(chat_id=777),
        )
        t0 = time.monotonic()
        server.session_store.finish_request(accepted.request_id, success=True, assistant_text="Cross-process output!")
        await asyncio.wait_for(delivered.wait(), timeout=2.0)
        latency_ms = (time.monotonic() - t0) * 1000.0
        assert latency_ms < 1000.0, f"Cross-process wake took too long: {latency_ms:.1f}ms"
    finally:
        worker_runtime.is_shutting_down = True
        frontend_incremental_delivery.unbind_telegram_delivery_wakeup(worker_runtime)
        frontend_incremental_delivery.notify_incremental_telegram_delivery(worker_runtime)
        await asyncio.wait_for(worker_runtime._incremental_telegram_delivery_task, timeout=1.0)
        await asyncio.gather(core_peer.close(), worker_peer.close())


# ---------------------------------------------------------------------------
# PIT C: 24 idle workers keep polling near-zero (never flat 2s spin)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pit_c_24_idle_workers_do_not_spin_every_2s(tmp_path: Path, monkeypatch):
    # Scale time 100x faster (base=0.02s, max=0.60s) to simulate 60s of real idle across 24 workers in 0.6s!
    monkeypatch.setattr(frontend_incremental_delivery, "IDLE_SWEEP_BASE_SECONDS", 0.02)
    monkeypatch.setattr(frontend_incremental_delivery, "IDLE_SWEEP_MAX_SECONDS", 0.60)
    scan_counts = {f"agent_{i}": 0 for i in range(24)}
    disconnected_scans = 0

    async def fake_dispatch(rt, *, limit=20):
        nonlocal disconnected_scans
        if not rt.telegram_connected:
            disconnected_scans += 1
        else:
            scan_counts[rt.name] += 1
        return 0

    monkeypatch.setattr(frontend_incremental_delivery, "dispatch_pending_telegram_deliverables", fake_dispatch)

    runtimes = []
    tasks = []
    for i in range(24):
        rt = SimpleNamespace(
            name=f"agent_{i}",
            is_shutting_down=False,
            telegram_connected=(i < 12),  # 12 connected idle + 12 disconnected
            app=SimpleNamespace(bot=object() if i < 12 else None),
            global_config=SimpleNamespace(instance_id="HASHI_24"),
            logger=logging.getLogger(f"test.pit_c.{i}"),
        )
        runtimes.append(rt)
        tasks.append(asyncio.create_task(frontend_incremental_delivery.incremental_telegram_delivery_loop(rt)))

    try:
        # 0.65s scaled = ~65s real wall time.
        # Old flat 2s polling (0.02s scaled) would do ~32 scans per worker (~384 scans for 12 workers).
        # With exponential backoff (0.02 -> 0.04 -> 0.08 -> 0.16 -> 0.32 -> 0.60), each connected worker
        # does <= 7 scans total (~5x+ reduction even in minute 1, and 30x reduction in steady state)!
        await asyncio.sleep(0.65)
    finally:
        for rt in runtimes:
            rt.is_shutting_down = True
            frontend_incremental_delivery.notify_incremental_telegram_delivery(rt)
        await asyncio.gather(*tasks, return_exceptions=True)

    assert disconnected_scans == 0, "Disconnected workers must perform 0 scans"
    total_connected_scans = sum(scan_counts[f"agent_{i}"] for i in range(12))
    assert total_connected_scans < 12 * 10, f"24 idle workers polled too aggressively: {total_connected_scans}"


# ---------------------------------------------------------------------------
# PIT D: New run.question.created wakes idle worker immediately
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pit_d_run_question_created_wakes_idle_worker_immediately(
    tmp_path: Path, monkeypatch
):
    store = _new_store(tmp_path)
    owner = "user:1"
    session = store.ensure_default_session(owner_id=owner, agent_id="zelda")
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="zelda",
        request_id="req-pit-d",
        source="telegram",
        idempotency_key="idem-pit-d",
        text="ask me",
        delivery_route=_telegram_route(chat_id=4004),
    )

    question_delivered = asyncio.Event()
    seen_questions: list[str] = []

    async def fake_dispatch(rt, *, limit=20):
        qs = RunQuestions(rt.session_store).pending_telegram_deliveries(agent_id=rt.name, limit=limit)
        if qs:
            seen_questions.extend(q["event_id"] for q in qs)
            question_delivered.set()
            return len(qs)
        return 0

    monkeypatch.setattr(frontend_incremental_delivery, "dispatch_pending_telegram_deliverables", fake_dispatch)
    monkeypatch.setattr(frontend_incremental_delivery, "IDLE_SWEEP_BASE_SECONDS", 60.0)

    runtime = SimpleNamespace(
        name="zelda",
        is_shutting_down=False,
        telegram_connected=True,
        app=SimpleNamespace(bot=object()),
        session_store=store,
        global_config=SimpleNamespace(instance_id="HASHI_EFF_TEST"),
        logger=logging.getLogger("test.pit_d"),
    )

    frontend_incremental_delivery.start_incremental_telegram_delivery(runtime)
    try:
        await asyncio.sleep(0.05)
        assert not question_delivered.is_set()

        t0 = time.monotonic()
        with store._connection() as conn:
            conn.execute("UPDATE runs SET state='running' WHERE run_id=?", (accepted.run_id,))
        RunQuestions(store).create(
            request_id=accepted.request_id,
            agent_id="zelda",
            payload={
                "idempotency_key": "q-pit-d-1",
                "question": "Pick a color?",
                "options": [
                    {"id": "chartreuse", "label": "Chartreuse"},
                    {"id": "blue", "label": "Blue"},
                ],
                "allow_free_text": True,
                "purpose": "preference",
            },
        )
        await asyncio.wait_for(question_delivered.wait(), timeout=1.0)
        assert seen_questions
        assert (time.monotonic() - t0) * 1000.0 < 500.0
    finally:
        runtime.is_shutting_down = True
        frontend_incremental_delivery.unbind_telegram_delivery_wakeup(runtime)
        frontend_incremental_delivery.notify_incremental_telegram_delivery(runtime)
        await asyncio.wait_for(runtime._incremental_telegram_delivery_task, timeout=1.0)


# ---------------------------------------------------------------------------
# PIT E: Detached HERV3 background requests are recognized as active runs
# ---------------------------------------------------------------------------


def test_pit_e_detached_herv3_background_request_not_treated_as_idle():
    rt_idle = SimpleNamespace(
        is_generating=False,
        is_processing=False,
        current_request_meta=None,
        _background_request_ids=set(),
        _outstanding_request_ids=set(),
    )
    assert frontend_incremental_delivery._runtime_has_active_run(rt_idle) is False

    rt_detached = SimpleNamespace(
        is_generating=False,
        is_processing=False,
        current_request_meta=None,
        _background_request_ids={"req-herv3-detached-1"},
    )
    assert frontend_incremental_delivery._runtime_has_active_run(rt_detached) is True

    rt_detached._background_request_ids.clear()
    assert frontend_incremental_delivery._runtime_has_active_run(rt_detached) is False


# ---------------------------------------------------------------------------
# PIT F: Concurrent notify + recovery sweep never duplicates sends & preserves unknown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pit_f_no_duplicate_send_under_concurrent_wakeup_and_preserves_unknown(
    tmp_path: Path, monkeypatch, caplog
):
    store = _new_store(tmp_path)
    owner = "user:1"
    session = store.ensure_default_session(owner_id=owner, agent_id="zelda")
    route = _telegram_route(chat_id=5005)
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="zelda",
        request_id="req-pit-f-1",
        source="telegram",
        idempotency_key="idem-pit-f-1",
        text="once only",
        delivery_route=route,
    )
    store.finish_request(accepted.request_id, success=True, assistant_text="Deliver exactly once.")

    sent_event_ids: list[str] = []
    completed_outbox_ids: list[str] = []

    async def fake_claimed_dispatch(rt, *, chat_id, store, claim, **_kwargs):
        sent_event_ids.append(str(claim["event_id"]))
        completed_outbox_ids.append(str(claim["outbox_id"]))
        result = store.complete_delivery_outbox(
            outbox_id=str(claim["outbox_id"]),
            lease_token=str(claim["lease_token"]),
            status="completed",
        )
        assert result["state"] == "completed"

    monkeypatch.setattr(frontend_incremental_delivery, "dispatch_claimed_telegram_event", fake_claimed_dispatch)
    monkeypatch.setattr("orchestrator.frontend_run_questions.refresh_telegram_question_renditions", lambda _r, _s: asyncio.sleep(0))

    runtime = SimpleNamespace(
        name="zelda",
        is_shutting_down=False,
        telegram_connected=True,
        app=SimpleNamespace(bot=object()),
        session_store=store,
        global_config=SimpleNamespace(instance_id="HASHI_EFF_TEST"),
        logger=logging.getLogger("test.pit_f"),
    )

    # Fire two concurrent sweeps (simulating notification + recovery sweep colliding).
    with caplog.at_level(logging.WARNING, logger="test.pit_f"):
        r1, r2 = await asyncio.gather(
            frontend_incremental_delivery.dispatch_pending_telegram_deliverables(runtime),
            frontend_incremental_delivery.dispatch_pending_telegram_deliverables(runtime),
        )
    assert not caplog.records, f"Unexpected warning/error swallowed during dispatch: {[r.message for r in caplog.records]}"
    assert r1 + r2 >= 1
    assert sent_event_ids, "Expected at least one event to be dispatched"
    assert len(sent_event_ids) == len(set(sent_event_ids)), f"Duplicate sends detected: {sent_event_ids}"

    # Verify REAL SQLite state transitioned to 'completed' (NOT stuck in 'claimed'!).
    with sqlite3.connect(store.db_path) as conn:
        for oid in completed_outbox_ids:
            row = conn.execute(
                "SELECT state, completed_at FROM connector_delivery_tasks WHERE task_id=?",
                (oid,),
            ).fetchone()
            assert row is not None and row[0] == "completed" and row[1], f"Expected completed in SQLite, got {row}"
    # Subsequent sweep must return 0 (already completed).
    assert await frontend_incremental_delivery.dispatch_pending_telegram_deliverables(runtime) == 0

    # Verify unknown outcome is persisted in SQLite and NEVER resent on subsequent sweeps!
    accepted2 = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="zelda",
        request_id="req-pit-f-unknown",
        source="telegram",
        idempotency_key="idem-pit-f-unknown",
        text="uncertain send",
        delivery_route=route,
    )
    store.finish_request(accepted2.request_id, success=True, assistant_text="Uncertain transport.")
    unknown_calls = 0
    unknown_outbox_ids: list[str] = []

    async def fake_unknown_dispatch(rt, *, chat_id, store, claim, **_kwargs):
        nonlocal unknown_calls
        unknown_calls += 1
        unknown_outbox_ids.append(str(claim["outbox_id"]))
        result = store.complete_delivery_outbox(
            outbox_id=str(claim["outbox_id"]),
            lease_token=str(claim["lease_token"]),
            status="unknown",
            error_code="telegram_send_outcome_unknown",
        )
        assert result["state"] == "unknown"

    monkeypatch.setattr(frontend_incremental_delivery, "dispatch_claimed_telegram_event", fake_unknown_dispatch)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="test.pit_f"):
        await frontend_incremental_delivery.dispatch_pending_telegram_deliverables(runtime)
    assert not caplog.records, f"Unexpected warning/error during unknown dispatch: {[r.message for r in caplog.records]}"
    first_count = unknown_calls
    assert first_count >= 1
    assert unknown_outbox_ids

    # Verify REAL SQLite state is persisted as 'unknown'!
    with sqlite3.connect(store.db_path) as conn:
        for oid in unknown_outbox_ids:
            row = conn.execute(
                "SELECT state, last_error_code FROM connector_delivery_tasks WHERE task_id=?",
                (oid,),
            ).fetchone()
            assert row == ("unknown", "telegram_send_outcome_unknown"), f"Expected unknown in SQLite, got {row}"

    # Subsequent sweeps MUST NOT retry the unknown task!
    assert await frontend_incremental_delivery.dispatch_pending_telegram_deliverables(runtime) == 0
    assert unknown_calls == first_count


# ---------------------------------------------------------------------------
# PIT G: Worker alert propagates via metadata IPC, expires automatically,
#        and never breaks service availability (ready/status/degraded)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pit_g_worker_alert_propagates_expires_and_preserves_availability(
    tmp_path: Path, monkeypatch
):
    from orchestrator.function_worker_host import FunctionWorkerHost
    from orchestrator.workbench_api import WorkbenchApiServer

    frontend_database.reset_database_work_metrics()
    published_metadata: list[dict] = []
    published_event = asyncio.Event()

    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.agent_name = "zelda"
    host.phase = "ACTIVE"
    host.runtime = SimpleNamespace(name="zelda")
    host._resource_alert_listener = None
    host._resource_metadata_publish_task = None

    async def fake_publish():
        snap = frontend_database.database_work_health_snapshot()
        published_metadata.append({"resource_health": snap})
        published_event.set()

    host.publish_metadata = fake_publish
    host._install_resource_alert_bridge()

    try:
        # Trigger slow DB alert inside Worker -> edge-triggered metadata push fires immediately!
        frontend_database.record_database_work_sample(
            operation="telegram.scan",
            instance="TEST",
            role="worker",
            duration_s=0.75,
        )
        await asyncio.wait_for(published_event.wait(), timeout=1.0)
        assert published_metadata and published_metadata[-1]["resource_health"]["alerts"]
        worker_meta = published_metadata[-1]

        # Reset host-local metrics so ONLY the Worker's cached metadata carries the alert.
        frontend_database.reset_database_work_metrics()

        worker_proxy = SimpleNamespace(
            name="zelda",
            startup_success=True,
            is_function_worker_proxy=True,
            worker_pid=4242,
            generation_id="gen-1",
            metadata={"worker_phase": "ACTIVE", "worker_accepting": True, **worker_meta},
            client=None,
        )
        config_path = tmp_path / "agents.json"
        config_path.write_text(
            json.dumps({"global": {}, "agents": [{"name": "zelda", "workspace_dir": "workspaces/zelda"}]}),
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
            orchestrator=SimpleNamespace(
                runtimes=[worker_proxy],
                startup_status={"ready": True, "degraded": False, "phase": "ready", "issues": []},
            ),
            reconcile_session_runs=False,
        )

        # 1. While alert is active: visible in resource_health & issues, yet service remains ready & NOT degraded!
        resp = await server.handle_health(SimpleNamespace())
        payload = json.loads(resp.text)
        assert payload["resource_health"]["alert_count"] == 1
        assert any(i.get("code") == "slow_database_work" and i.get("agent") == "zelda" for i in payload["issues"])
        assert payload["ready"] is True
        assert payload["status"] == "ready"
        assert payload["degraded"] is False

        # 2. Once alert's expires_at is in the past: /api/health automatically expires it
        #    even without another Worker IPC message!
        worker_proxy.metadata["resource_health"]["alerts"][0]["expires_at"] = time.time() - 5.0
        resp2 = await server.handle_health(SimpleNamespace())
        payload2 = json.loads(resp2.text)
        assert payload2["resource_health"]["alert_count"] == 0
        assert payload2["resource_health"]["workers"]["zelda"]["alerts"] == []
        assert not any(i.get("code") == "slow_database_work" for i in payload2["issues"])
        assert payload2["ready"] is True
        assert payload2["status"] == "ready"
        assert payload2["degraded"] is False
    finally:
        host._remove_resource_alert_bridge()
        frontend_database.reset_database_work_metrics()


# ---------------------------------------------------------------------------
# PIT H: Legacy Schema 37 -> 38 migration with thousands of zombie rows is fast
# ---------------------------------------------------------------------------


def test_pit_h_schema_37_to_38_large_zombie_migration_is_fast_and_usable(tmp_path: Path):
    db_path = tmp_path / "legacy37.sqlite3"
    store = SessionStore(db_path, instance_id="HASHI_MIG_PERF")
    owner = "user:1"
    session = store.ensure_default_session(owner_id=owner, agent_id="zelda")
    route = _telegram_route(chat_id=8008)
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="zelda",
        request_id="req-leg-real",
        source="telegram",
        idempotency_key="idem-leg-real",
        text="real work",
        delivery_route=route,
    )
    store.finish_request(accepted.request_id, success=True, assistant_text="Real output survives migration.")

    # Inject 5,000 zombie telegram/pending tasks (run.accepted, run.stopped, session.title_updated)
    # + 200 route-less frontend.message.recorded rows and downgrade marker to v37.
    with sqlite3.connect(db_path) as conn:
        zombie_kinds = ("run.accepted", "run.stopped", "session.title_updated", "run.superseded")
        events = [
            (f"ev_z_{i}", session["session_id"], accepted.run_id, 500 + i, zombie_kinds[i % 4], "queued", "admission", "z", "{}", "2026-10-01T00:00:00Z")
            for i in range(5_000)
        ] + [
            (f"ev_nr_{i}", session["session_id"], None, 10_000 + i, "frontend.message.recorded", "recorded", "presentation", "nr", "{}", "2026-10-01T00:00:00Z")
            for i in range(200)
        ]
        tasks = [
            (f"del_z_{i}", session["session_id"], accepted.run_id, f"ev_z_{i}", "telegram", "ep-tg", "primary", '["text"]', "query_before_retry", "pending", "2026-10-01T00:00:00Z")
            for i in range(5_000)
        ] + [
            (f"del_nr_{i}", session["session_id"], None, f"ev_nr_{i}", "telegram", "ep-tg", "primary", '["text"]', "query_before_retry", "pending", "2026-10-01T00:00:00Z")
            for i in range(200)
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
        conn.execute("DROP INDEX IF EXISTS connector_delivery_tasks_telegram_active")
        conn.execute("UPDATE schema_metadata SET value='37' WHERE key='schema_version'")

    traced_sql: list[str] = []
    orig_connect = SessionStore._connect

    def tracing_connect(self):
        conn = orig_connect(self)
        conn.set_trace_callback(traced_sql.append)
        return conn

    orig_cls_connect = SessionStore._connect
    SessionStore._connect = tracing_connect
    try:
        t0 = time.perf_counter()
        upgraded = SessionStore(db_path, instance_id="HASHI_MIG_PERF")
        migration_ms = (time.perf_counter() - t0) * 1000.0
    finally:
        SessionStore._connect = orig_cls_connect

    assert migration_ms < 2000.0, f"Schema 37->38 migration blocked startup too long: {migration_ms:.1f}ms"
    # Verify NO duplicate DROP/CREATE of connector_delivery_tasks_telegram_active during upgrade!
    tg_idx_drops = [s for s in traced_sql if "DROP INDEX" in s.upper() and "connector_delivery_tasks_telegram_active" in s]
    tg_idx_creates = [s for s in traced_sql if "CREATE INDEX" in s.upper() and "connector_delivery_tasks_telegram_active" in s]
    assert len(tg_idx_drops) == 0, f"Unexpected DROP INDEX on upgrade: {tg_idx_drops}"
    assert len(tg_idx_creates) == 1, f"Expected exactly 1 CREATE INDEX on upgrade, got {len(tg_idx_creates)}"

    with sqlite3.connect(db_path) as conn:
        zombies_left = conn.execute(
            """SELECT COUNT(*) FROM connector_delivery_tasks AS t
               JOIN run_events AS e ON e.event_id=t.event_id
               WHERE t.connector_id='telegram' AND t.state='pending'
                 AND e.kind IN ('run.accepted','run.stopped','session.title_updated','run.superseded')"""
        ).fetchone()[0]
        assert zombies_left == 0

    # Real deliverable survived and is immediately queryable in < 5ms!
    t1 = time.perf_counter()
    deliverables = upgraded.pending_incremental_telegram_deliveries(agent_id="zelda")
    scan_ms = (time.perf_counter() - t1) * 1000.0
    assert len(deliverables) >= 1
    assert scan_ms < 10.0, f"Post-migration scan too slow: {scan_ms:.2f}ms"


# ---------------------------------------------------------------------------
# P1 Deterministic IPC Race Test: second deliverable while first emit Task
# still in flight must NOT be dropped (never waits for 60s recovery sweep)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ipc_wakeup_race_second_output_never_dropped_while_first_emit_in_flight(
    tmp_path: Path, monkeypatch
):
    from orchestrator.function_worker_supervisor import AgentRuntimeHandle, FunctionWorkerSupervisor

    store = _new_store(tmp_path)
    owner = "user:1"
    session = store.ensure_default_session(owner_id=owner, agent_id="zelda")
    route = _telegram_route(chat_id=6006)

    seen_events: list[str] = []
    scan1_done = asyncio.Event()
    scan2_done = asyncio.Event()
    release_first_emit = asyncio.Event()

    async def fake_dispatch(rt, *, limit=20):
        rows = rt.session_store.pending_incremental_telegram_deliveries(agent_id=rt.name, limit=limit)
        if not rows:
            return 0
        for row in rows:
            eid = str(row["event_id"])
            if eid not in seen_events:
                seen_events.append(eid)
        with rt.session_store._connection() as c:
            c.executemany(
                "UPDATE connector_delivery_tasks SET state='completed' WHERE event_id=? AND connector_id='telegram'",
                [(str(row["event_id"]),) for row in rows],
            )
        if len(seen_events) >= 1:
            scan1_done.set()
        if len(seen_events) >= 2:
            scan2_done.set()
        return len(rows)

    monkeypatch.setattr(frontend_incremental_delivery, "dispatch_pending_telegram_deliverables", fake_dispatch)
    monkeypatch.setattr(frontend_incremental_delivery, "IDLE_SWEEP_BASE_SECONDS", 60.0)

    worker_runtime = SimpleNamespace(
        name="zelda",
        is_shutting_down=False,
        telegram_connected=True,
        app=SimpleNamespace(bot=object()),
        session_store=store,
        global_config=SimpleNamespace(instance_id="HASHI_EFF_TEST"),
        logger=logging.getLogger("test.ipc_race"),
    )

    emit_count = 0

    class RacingPeer:
        is_closed = False

        async def emit(self, event: str, payload: dict):
            nonlocal emit_count
            emit_count += 1
            call_num = emit_count
            assert event == "core.telegram_delivery_wake"
            # Worker receives wake immediately...
            frontend_incremental_delivery.notify_incremental_telegram_delivery(worker_runtime)
            if call_num == 1:
                # ...but first emit Task stays unfinished while Worker finishes scan #1
                # and re-enters the 60s wait!
                await release_first_emit.wait()

    client = SimpleNamespace(agent_name="zelda", peer=RacingPeer(), closed=False)
    handle = AgentRuntimeHandle.__new__(AgentRuntimeHandle)
    handle.name = "zelda"
    handle._client = client
    handle.metadata = {"telegram_connected": True}

    supervisor = FunctionWorkerSupervisor.__new__(FunctionWorkerSupervisor)
    supervisor.kernel = SimpleNamespace(_runtime_map=lambda: {"zelda": handle})

    # Start worker loop WITHOUT in-process store binding so ONLY supervisor IPC wakes it!
    loop_task = asyncio.create_task(
        frontend_incremental_delivery.incremental_telegram_delivery_loop(worker_runtime)
    )
    try:
        await asyncio.sleep(0.05)

        # Deliverable #1 commits -> notify #1 wakes Worker -> Worker finishes scan #1 and goes back to 60s sleep,
        # while emit #1 Task is STILL in flight!
        acc1 = store.accept_run(
            session_id=session["session_id"],
            owner_id=owner,
            agent_id="zelda",
            request_id="req-race-1",
            source="workbench",
            idempotency_key="idem-race-1",
            text="first",
            delivery_route=route,
        )
        store.finish_request(acc1.request_id, success=True, assistant_text="First output.")
        supervisor.notify_telegram_delivery("zelda")
        await asyncio.wait_for(scan1_done.wait(), timeout=1.0)
        # Allow Worker loop to finish scan #1 and re-enter 60s wait_for().
        await asyncio.sleep(0.05)
        assert len(supervisor._telegram_wake_tasks) == 1, "First emit Task must still be in-flight for the race!"

        # Deliverable #2 commits WHILE first emit Task is STILL unfinished!
        acc2 = store.accept_run(
            session_id=session["session_id"],
            owner_id=owner,
            agent_id="zelda",
            request_id="req-race-2",
            source="workbench",
            idempotency_key="idem-race-2",
            text="second",
            delivery_route=route,
        )
        store.finish_request(acc2.request_id, success=True, assistant_text="Second output.")
        t0 = time.monotonic()
        supervisor.notify_telegram_delivery("zelda")

        # Second output MUST trigger scan #2 immediately (NOT dropped / NOT waiting 60s)!
        await asyncio.wait_for(scan2_done.wait(), timeout=1.0)
        assert (time.monotonic() - t0) * 1000.0 < 500.0
        assert emit_count == 2
    finally:
        release_first_emit.set()
        worker_runtime.is_shutting_down = True
        frontend_incremental_delivery.notify_incremental_telegram_delivery(worker_runtime)
        await asyncio.wait_for(loop_task, timeout=1.0)


# ---------------------------------------------------------------------------
# P2 Decorator Placement Test: @timed("worker.activate") belongs on activate()
# ---------------------------------------------------------------------------


def test_worker_activate_decorator_placement_and_timing_tag():
    import inspect
    from orchestrator.function_worker_host import FunctionWorkerHost

    wake_fn = FunctionWorkerHost._schedule_peer_telegram_wake
    activate_fn = FunctionWorkerHost.activate

    # _schedule_peer_telegram_wake must NOT be wrapped by @timed(...)
    assert not hasattr(wake_fn, "__wrapped__"), "_schedule_peer_telegram_wake must not carry @timed('worker.activate')"
    # activate() MUST be an async coroutine wrapped by @timed('worker.activate')
    assert hasattr(activate_fn, "__wrapped__"), "activate() must retain @timed('worker.activate')"
    assert inspect.iscoroutinefunction(activate_fn)
    closure_values = [
        cell.cell_contents
        for cell in (activate_fn.__closure__ or ())
    ]
    assert "worker.activate" in closure_values, f"Expected 'worker.activate' timing label, got {closure_values}"
