"""Real SQLite contention regressions: API/Worker loops and durable questions."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
import time
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.run_questions import RunQuestions, issue_tool_token
from orchestrator.session_store import SessionStore
from orchestrator.workbench_api import WorkbenchApiServer
from tests.test_run_questions import SECRET, active, spec


@contextmanager
def writer_lock(store, seconds=0.9):
    """An independent connection releases without needing the tested loop."""
    ready = threading.Event()
    release = threading.Event()

    def hold():
        with sqlite3.connect(store.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            ready.set()
            release.wait(seconds)
            connection.rollback()

    thread = threading.Thread(target=hold)
    thread.start()
    assert ready.wait(2)
    try:
        yield
    finally:
        release.set()
        thread.join(3)
        assert not thread.is_alive()


def make_server(tmp_path):
    cfg = SimpleNamespace(bridge_home=tmp_path, project_root=tmp_path,
                          instance_id="HASHI1", authorized_id=7,
                          workbench_port=18800, api_gateway_port=18801,
                          deployment_profile="personal", persistent_session_v1=True)
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"global": {}, "agents": [{"name": "lily"}]}))
    return WorkbenchApiServer(path, cfg, secrets={"workbench_admin_token": SECRET},
                              reconcile_session_runs=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["initialize", "list", "create", "get", "answer"])
async def test_locked_question_routes_keep_real_health_and_read_api_responsive(tmp_path, operation):
    server = make_server(tmp_path)
    session, run = active(server.session_store)
    service = None
    question = None
    if operation != "initialize":
        service = server._questions()
        question = service.create(request_id=run.request_id, agent_id="lily", payload=spec())
    if operation == "list":
        service.clock = lambda: time.time() + 400  # A real expiry must still commit.
    token = issue_tool_token(SECRET, instance_id="HASHI1", agent_id="lily", request_id=run.request_id)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    headers = {"X-Workbench-Token": SECRET}
    session_url = "/api/v1/sessions/" + session["session_id"] + "/questions"

    def probe():
        time.sleep(0.12)  # Run independently while the question route waits.
        def read(path):
            started = time.monotonic()
            req = urllib.request.Request(str(client.make_url(path)), headers=headers)
            with urllib.request.urlopen(req, timeout=5) as response:
                assert response.status == 200
                response.read()
            return path, round((time.monotonic() - started) * 1000, 1)
        paths = ("/api/health", "/api/v1/capabilities",
                 "/api/v1/sessions/" + session["session_id"])
        with ThreadPoolExecutor(max_workers=len(paths)) as workers:
            readings = dict(workers.map(read, paths))

        return readings

    async def request():
        if operation == "list":
            return await client.get(session_url, headers=headers)
        elif operation == "answer":
            return await client.post(session_url + "/" + question["question_id"] + "/answer",
                headers=headers, json={"idempotency_key": "answer", "option_id": "blue"})
        else:
            payload = {"operation": "create" if operation == "initialize" else operation,
                       **(spec("second") if operation in {"create", "initialize"}
                        else {"question_id": question["question_id"]})}
            return await client.post("/api/v1/run-questions/tool",
                headers={"X-Hashi-Run-Question-Token": token}, json=payload)

    try:
        baseline = await asyncio.to_thread(probe)
        with writer_lock(server.session_store):
            probes = asyncio.create_task(asyncio.to_thread(probe))
            response = await request()
            assert response.status == 503, await response.text()
            assert (await response.json())["error_code"] == "question_storage_busy"
            readings = await probes
        response = await request()
        assert response.status == 200, await response.text()
        print(json.dumps({"operation": operation, "baseline_ms": baseline, "locked_ms": readings}))
        assert max(readings.values()) < 300, readings
        if operation == "list":
            with server.session_store._connection() as c:
                assert c.execute("SELECT state FROM run_questions").fetchone()[0] == "expired"
        if operation == "answer":
            saved = service.get_answer(request_id=run.request_id, agent_id="lily",
                                       question_id=question["question_id"])
            assert saved["answer"]["option_id"] == "blue"
            assert saved["answer"]["grants_permissions"] is False
    finally:
        await client.close()


@pytest.mark.parametrize("operation", ["refresh", "pending", "list", "construct"])
def test_idle_question_reads_do_not_reserve_a_writer(tmp_path, operation):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    session, _ = active(store)
    service = RunQuestions(store)
    connect = store._connect

    def impatient():
        connection = connect()
        connection.execute("PRAGMA busy_timeout=0")
        return connection

    store._connect = impatient
    with writer_lock(store):
        if operation == "refresh":
            assert service.refresh_pending(agent_id="lily") == []
        elif operation == "pending":
            assert service.pending_telegram_deliveries(agent_id="lily") == []
        elif operation == "list":
            assert service.list(session_id=session["session_id"], owner_id="user:7") == []
        else:
            # Repeated construction must reuse the completed schema for this store.
            observed = []
            def trace_connect():
                c = impatient()
                c.set_trace_callback(observed.append)
                return c
            store._connect = trace_connect
            RunQuestions(store)
            assert not any("CREATE " in sql.upper() for sql in observed), observed
# HASHI-Fingerprint: hfp_f14c0a317d394d8582c516ec62b58c75


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["scan", "claim", "rendition"])
async def test_telegram_database_wait_keeps_serialized_worker_topology_responsive(tmp_path, phase):
    import multiprocessing
    from orchestrator.frontend_database import DatabaseWorkBusy
    from orchestrator.frontend_incremental_delivery import dispatch_pending_telegram_deliverables
    from orchestrator.function_worker_host import FunctionWorkerHost
    from orchestrator.function_worker_protocol import JsonConnectionPeer
    from tests.test_frontend_attachment_tool import _ToolRuntime
    from tests.test_run_question_connectors import running

    service, session, run, question = running(tmp_path)
    runtime = _ToolRuntime(tmp_path, service.store)
    runtime.name = "zelda"
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.facade = None
    host.phase = "ACTIVE"
    # Isolate metadata construction; the real request handler and JSON pipe run.
    host.metadata = lambda: {"agent_name": runtime.name, "worker_phase": "ACTIVE"}
    left, right = multiprocessing.Pipe()
    caller = JsonConnectionPeer(left, label="test-core")
    worker = JsonConnectionPeer(right, label="test-worker", request_handler=host.handle_request)
    caller.start()
    worker.start()
    loop = asyncio.get_running_loop()
    probe_started = threading.Event()
    database_locked = threading.Event()

    def probe():
        probe_started.set()
        assert database_locked.wait(3)
        time.sleep(0.04)
        started = time.monotonic()
        response = asyncio.run_coroutine_threadsafe(
            caller.request("worker.topology", {}), loop).result(timeout=4)
        assert response["agent_name"] == "zelda"
        ping = asyncio.run_coroutine_threadsafe(
            caller.request("worker.ping", {}), loop).result(timeout=4)
        assert ping["phase"] == "ACTIVE"
        return round((time.monotonic() - started) * 1000, 1)

    if phase == "scan":
        with service.store._connection() as c:
            c.execute("UPDATE run_questions SET expires_epoch=0")
    held = None
    if phase == "rendition":
        original_send = runtime.app.bot.send_message
        async def send(**kwargs):
            nonlocal held
            result = await original_send(**kwargs)
            held = writer_lock(service.store)
            held.__enter__()  # SQLite blocks only persistence AFTER network acceptance.
            database_locked.set()
            return result
        runtime.app.bot.send_message = send
    try:
        probes = asyncio.create_task(asyncio.to_thread(probe))
        for _ in range(200):
            if probe_started.is_set():
                break
            await asyncio.sleep(0.001)
        assert probe_started.is_set()
        if phase != "rendition":
            with writer_lock(service.store):
                database_locked.set()
                try:
                    dispatched = await dispatch_pending_telegram_deliverables(runtime)
                except DatabaseWorkBusy:
                    assert phase == "scan"
                    dispatched = 0
        else:
            dispatched = await dispatch_pending_telegram_deliverables(runtime)
        elapsed = await probes
        print(json.dumps({"worker_phase": phase, "topology_ms": elapsed}))
        assert elapsed < 300
        assert service.list(session_id=session["session_id"], owner_id="user:7")[0]["state"] == (
            "expired" if phase == "scan" else "pending")
        await dispatch_pending_telegram_deliverables(runtime)
        assert len(runtime.app.bot.messages) == (0 if phase == "scan" else 1)
        if phase != "scan":
            assert dispatched == 1
    finally:
        if held is not None:
            held.__exit__(None, None, None)
        await asyncio.gather(caller.close(), worker.close())


@pytest.mark.asyncio
async def test_database_admission_and_cancellation_hold_capacity_until_settlement():
    from orchestrator.frontend_database import DatabaseWorkBusy, run_database_work

    release = threading.Event()
    active_count = 0
    maximum = 0
    calls = []
    lock = threading.Lock()

    def write(index):
        nonlocal active_count, maximum
        with lock:
            calls.append(index)
            active_count += 1
            maximum = max(maximum, active_count)
        assert release.wait(3)
        with lock:
            active_count -= 1
        return index

    tasks = [asyncio.create_task(run_database_work(write, index)) for index in range(8)]
    timer = threading.Timer(2, release.set)
    timer.start()
    try:
        for _ in range(100):
            if len(calls) == 2:
                break
            await asyncio.sleep(0.005)
        assert len(calls) == 2
        with pytest.raises(DatabaseWorkBusy):
            await run_database_work(lambda: "ninth must not enqueue")
        tasks[0].cancel()
        await asyncio.sleep(0.02)
        assert not tasks[0].done()  # Its write is still live; no released admission slot.
        with pytest.raises(DatabaseWorkBusy):
            await run_database_work(lambda: "cancelled write still owns its slot")
    finally:
        release.set()
        timer.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1:] == list(range(1, 8))
    assert maximum == 2 and sorted(calls) == list(range(8))
    assert await run_database_work(lambda: "capacity recovered") == "capacity recovered"


def test_refresh_upgrade_rechecks_a_timely_answer_and_changed_generation(tmp_path, monkeypatch):
    from orchestrator.run_questions import QuestionError
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    first = RunQuestions(store, clock=lambda: 1000)
    session, run = active(store)
    question = first.create(request_id=run.request_id, agent_id="lily",
                            payload={**spec(), "expires_seconds": 30})
    second = RunQuestions(SessionStore(store.db_path, instance_id="HASHI1"), clock=lambda: 1001)
    first.clock = lambda: 1040
    transaction = first._transaction

    @contextmanager
    def answer_between_read_and_write(**kwargs):
        second.answer(session_id=session["session_id"], owner_id="user:7",
                      question_id=question["question_id"],
                      payload={"idempotency_key": "on-time", "option_id": "blue"})
        with transaction(**kwargs) as connection:
            yield connection

    monkeypatch.setattr(first, "_transaction", answer_between_read_and_write)
    listed = first.list(session_id=session["session_id"], owner_id="user:7")
    assert listed[0]["state"] == "answered"
    assert listed[0]["answer"]["option_id"] == "blue"
    with store._connection() as c:
        assert c.execute("SELECT COUNT(*) FROM run_events WHERE kind='run.question.expired'").fetchone()[0] == 0
    # Existing owner/Run/generation fences still reject late writes after reset.
    with store._connection() as c:
        c.execute("UPDATE sessions SET context_generation=context_generation+1 WHERE session_id=?",
                  (session["session_id"],))
    with pytest.raises(QuestionError):
        second.answer(session_id=session["session_id"], owner_id="user:7",
                      question_id=question["question_id"],
                      payload={"idempotency_key": "late", "option_id": "red"})


def test_question_schema_initialization_retries_failure_and_is_shared_per_store(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from orchestrator.frontend_run_questions import QuestionRenditions

    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    original = store._connection
    attempts = []

    @contextmanager
    def fail_once():
        if not attempts:
            attempts.append("failed")
            raise sqlite3.OperationalError("synthetic schema failure")
        with original() as connection:
            yield connection

    monkeypatch.setattr(store, "_connection", fail_once)
    with pytest.raises(sqlite3.OperationalError):
        RunQuestions(store)
    assert not getattr(store, "_run_questions_schema_ready", False)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(list(pool.map(lambda _: RunQuestions(store), range(4)))) == 4
    QuestionRenditions(store)
    observed = []
    connect = store._connect

    def traced():
        connection = connect()
        connection.set_trace_callback(observed.append)
        return connection

    monkeypatch.setattr(store, "_connect", traced)
    RunQuestions(store)
    QuestionRenditions(store)
    assert not any("CREATE " in sql.upper() for sql in observed)


@pytest.mark.asyncio
async def test_busy_scan_backs_off_without_treating_busy_as_a_transport_retry(tmp_path, monkeypatch):
    import logging
    from orchestrator import frontend_incremental_delivery as delivery
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="TEST")
    with writer_lock(store):
        with store._connection() as c:
            c.execute("PRAGMA busy_timeout=0")
            with pytest.raises(sqlite3.OperationalError) as raised:
                c.execute("BEGIN IMMEDIATE")
    runtime = SimpleNamespace(is_shutting_down=False,
                              global_config=SimpleNamespace(instance_id="TEST"),
                              logger=logging.getLogger("test.busy-scan"))
    attempts, delays = [], []

    async def busy(_runtime):
        attempts.append("discovery")
        raise raised.value

    async def pause(delay):
        delays.append(delay)
        if len(delays) == 3:
            runtime.is_shutting_down = True

    monkeypatch.setattr(delivery, "dispatch_pending_telegram_deliverables", busy)
    monkeypatch.setattr(delivery.random, "uniform", lambda low, high: high)
    monkeypatch.setattr(delivery.asyncio, "sleep", pause)
    await delivery.incremental_telegram_delivery_loop(runtime)
    assert attempts == ["discovery"] * 3
    assert 2 <= delays[0] < delays[1] < delays[2] <= 30


def test_slow_question_transactions_log_safe_correlation_and_wait(tmp_path, caplog):
    import logging
    from orchestrator.frontend_database import DatabaseWorkBusy
    from tests.test_run_question_connectors import running
    service, _, _, question = running(tmp_path)
    with service.store._connection() as c:
        c.execute("UPDATE run_questions SET expires_epoch=0")
    with caplog.at_level(logging.INFO):
        with writer_lock(service.store):
            with pytest.raises(DatabaseWorkBusy):
                service.refresh_pending(agent_id="zelda", busy_timeout_ms=120)
    message = next(r.message for r in caplog.records if "run_questions.database" in r.message)
    assert all(field in message for field in ("operation=refresh_pending", "instance=TEST",
               "pid=", "role=", "correlation=", "lock_wait_ms=", "transaction_ms=", "acquired=False"))
    assert question["question"] not in message and "user:7" not in message


@pytest.mark.asyncio
async def test_multiple_question_polls_bound_admission_and_do_not_duplicate_expiry(tmp_path):
    server = make_server(tmp_path)
    session, run = active(server.session_store)
    service = server._questions()
    service.create(request_id=run.request_id, agent_id="lily", payload=spec())
    service.clock = lambda: time.time() + 400
    client = TestClient(TestServer(server.app))
    await client.start_server()
    headers = {"X-Workbench-Token": SECRET}
    url = "/api/v1/sessions/" + session["session_id"] + "/questions"
    responses = []
    try:
        with writer_lock(server.session_store, seconds=3.0):
            tasks = [asyncio.create_task(client.get(url, headers=headers)) for _ in range(12)]
            await asyncio.sleep(0.1)
            started = time.monotonic()
            health = await client.get("/api/health", headers=headers)
            assert health.status == 200
            assert time.monotonic() - started < 0.3
            responses = await asyncio.gather(*tasks)
        statuses = [response.status for response in responses]
        assert set(statuses) == {503}
        for response in responses:
            assert (await response.json())["error_code"] == "question_storage_busy"
        for _ in range(2):
            response = await client.get(url, headers=headers)
            assert response.status == 200
            assert (await response.json())["questions"][0]["state"] == "expired"
        with service.store._connection() as c:
            assert c.execute("SELECT COUNT(*) FROM run_events WHERE kind='run.question.expired'").fetchone()[0] == 1
    finally:
        for response in responses:
            response.release()
        await client.close()


def test_live_pending_question_reads_do_not_reserve_a_writer(tmp_path):
    from tests.test_run_question_connectors import running
    service, session, _, question = running(tmp_path)
    connect = service.store._connect
    def impatient():
        connection = connect()
        connection.execute("PRAGMA busy_timeout=0")
        return connection
    service.store._connect = impatient
    with writer_lock(service.store):
        assert service.refresh_pending(agent_id="zelda")[0]["state"] == "pending"
        assert service.pending_telegram_deliveries(agent_id="zelda")[0]["question_id"] == question["question_id"]
        assert service.list(session_id=session["session_id"], owner_id="user:7")[0]["state"] == "pending"
