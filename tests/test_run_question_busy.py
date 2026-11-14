"""Short interactive writer waits under a sustained independent SQLite writer."""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import sqlite3
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.frontend_database import DatabaseWorkBusy
from orchestrator.frontend_delivery import freeze_run_delivery_route
from orchestrator.frontend_incremental_delivery import dispatch_pending_telegram_deliverables
from orchestrator.frontend_run_questions import question_error_text
from orchestrator.function_worker_host import FunctionWorkerHost
from orchestrator.function_worker_protocol import JsonConnectionPeer
from orchestrator.run_questions import QUESTION_STOP_ID, RunQuestions, issue_tool_token
from tests.test_frontend_attachment_tool import _ToolRuntime
from tests.test_run_question_contention import make_server, writer_lock
from tests.test_run_questions import SECRET, active, spec


def telegram_runtime(tmp_path, store, agent="lily"):
    runtime = _ToolRuntime(tmp_path, store)
    runtime.name = agent
    runtime.global_config.ui_language = "en"
    runtime.app.bot.id = 123
    runtime._is_authorized_user = lambda user: user == 7
    runtime._telegram_channel_allowed = AsyncMock(return_value=True)
    runtime._reply_text = AsyncMock()
    return runtime


def callback(question, callback_id="busy-callback", index=0):
    query = SimpleNamespace(data=f"rq:{question['question_id']}:{index}", id=callback_id,
                            answer=AsyncMock(), edit_message_reply_markup=AsyncMock(),
                            message=SimpleNamespace())
    return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7),
                           effective_chat=SimpleNamespace(id=7))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["initialize", "create", "get", "answer", "stop", "list"])
async def test_interactive_http_writer_returns_typed_busy_before_commit(tmp_path, monkeypatch, operation):
    server = make_server(tmp_path)
    session, run = active(server.session_store)
    service = None if operation == "initialize" else server._questions()
    q = None if service is None else service.create(request_id=run.request_id, agent_id="lily", payload=spec())
    if operation == "list":
        service.clock = lambda: time.time() + 400
    command = AsyncMock(return_value={"ok": True, "result": {"ok": True}})
    monkeypatch.setattr("orchestrator.workbench_api.execute_local_command", command)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    token = issue_tool_token(SECRET, instance_id="HASHI1", agent_id="lily", request_id=run.request_id)
    url = f"/api/v1/sessions/{session['session_id']}/questions"
    async def request():
        if operation == "list":
            return await client.get(url, headers={"X-Workbench-Token": SECRET})
        if operation in {"answer", "stop"}:
            return await client.post(url + "/" + q["question_id"] + "/answer",
                headers={"X-Workbench-Token": SECRET},
                json={"idempotency_key": "answer", "option_id": QUESTION_STOP_ID if operation == "stop" else "blue"})
        return await client.post("/api/v1/run-questions/tool",
            headers={"X-Hashi-Run-Question-Token": token},
            json={"operation": "create" if operation == "initialize" else operation,
                  **(spec("blocked-create") if operation in {"create", "initialize"} else
                     {"question_id": q["question_id"]})})
    try:
        with writer_lock(server.session_store, seconds=1.1):
            started = time.monotonic()
            response = await request()
            elapsed = time.monotonic() - started
            assert response.status == 503, await response.text()
            assert (await response.json())["error_code"] == "question_storage_busy"
            assert elapsed < 0.6, elapsed
            command.assert_not_called()
            with server.session_store._connection() as c:
                assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
                if q is not None:
                    row = c.execute("SELECT state,answer_key FROM run_questions WHERE question_id=?",
                                    (q["question_id"],)).fetchone()
                    assert tuple(row) == ("pending", None)
        response = await request()
        if operation != "stop":
            assert response.status == 200, await response.text()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sustained_contention_preserves_capacity_http_ipc_and_effect_identity(tmp_path):
    from orchestrator.runtime_run_questions import handle_callback
    server = make_server(tmp_path)
    store = server.session_store
    session = store.ensure_default_session(owner_id="user:7", agent_id="lily")
    route = freeze_run_delivery_route(message_source_id="workbench", session_surface="workbench",
        session_channel_key="test", chat_id="7", telegram_requested=True)
    run = store.accept_run(session_id=session["session_id"], owner_id="user:7", agent_id="lily",
        request_id="sustained-run", text="synthetic question", source="workbench",
        idempotency_key="sustained-run", delivery_route=route)
    store.mark_request_running(run.request_id, worker_id="test")
    service = server._questions()
    q = service.create(request_id=run.request_id, agent_id="lily", payload=spec())
    runtime = telegram_runtime(tmp_path, store)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    token = issue_tool_token(SECRET, instance_id="HASHI1", agent_id="lily", request_id=run.request_id)
    tool_headers = {"X-Hashi-Run-Question-Token": token}
    owner_headers = {"X-Workbench-Token": SECRET}
    answer_url = f"/api/v1/sessions/{session['session_id']}/questions/{q['question_id']}/answer"
    answer = {"idempotency_key": "one-http-answer", "option_id": "blue"}
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.phase, host.facade = "ACTIVE", None
    host.metadata = lambda: {"agent_name": "lily"}
    left, right = multiprocessing.Pipe()
    caller = JsonConnectionPeer(left, label="core")
    worker = JsonConnectionPeer(right, label="worker", request_handler=host.handle_request)
    caller.start()
    worker.start()
    readings = []
    stop_probe = asyncio.Event()
    async def probe():
        while not stop_probe.is_set():
            started = time.monotonic()
            response = await client.get("/api/health")
            assert response.status == 200
            assert (await caller.request("worker.ping", {}))["phase"] == "ACTIVE"
            assert (await caller.request("worker.topology", {}))["agent_name"] == "lily"
            readings.append(time.monotonic() - started)
            await asyncio.sleep(0.01)
    task = asyncio.create_task(probe())
    try:
        with writer_lock(store, seconds=4):
            for wave in range(3):
                started = time.monotonic()
                results = await asyncio.gather(
                    client.post("/api/v1/run-questions/tool", headers=tool_headers,
                                json={"operation": "create", **spec("recoverable-create")}),
                    client.post("/api/v1/run-questions/tool", headers=tool_headers,
                                json={"operation": "get", "question_id": q["question_id"]}),
                    client.post(answer_url, headers=owner_headers, json=answer))
                for response in results:
                    assert response.status == 503, await response.text()
                    assert (await response.json())["error_code"] == "question_storage_busy"
                assert time.monotonic() - started < 1.2
                update = callback(q, callback_id=f"blocked-{wave}")
                started = time.monotonic()
                await handle_callback(runtime, update, None)
                assert time.monotonic() - started < 0.6
                assert update.callback_query.answer.call_args.args[0] == question_error_text("question_storage_busy", "en")
            with store._connection() as c:
                assert c.execute("SELECT COUNT(*) FROM run_questions").fetchone()[0] == 1
                assert c.execute("SELECT state,answer_json FROM run_questions").fetchone()["state"] == "pending"
                assert c.execute("SELECT COUNT(*) FROM run_events WHERE kind='run.question.answered'").fetchone()[0] == 0
        created = []
        for _ in range(2):
            response = await client.post("/api/v1/run-questions/tool", headers=tool_headers,
                                        json={"operation": "create", **spec("recoverable-create")})
            assert response.status == 200
            created.append((await response.json())["question"])
        assert created[0]["question_id"] == created[1]["question_id"]
        assert await dispatch_pending_telegram_deliverables(runtime) == 2
        assert await dispatch_pending_telegram_deliverables(runtime) == 0
        assert len(runtime.app.bot.messages) == 2
        for _ in range(2):
            response = await client.post(answer_url, headers=owner_headers, json=answer)
            assert response.status == 200
        tg = callback(created[0], callback_id="one-telegram-answer")
        await handle_callback(runtime, tg, None)
        await handle_callback(runtime, tg, None)
        for item in (q, created[0]):
            for _ in range(2):
                response = await client.post("/api/v1/run-questions/tool", headers=tool_headers,
                    json={"operation": "get", "question_id": item["question_id"]})
                assert response.status == 200
                assert (await response.json())["question"]["state"] == "consumed"
        with store._connection() as c:
            for kind in ("created", "answered", "consumed"):
                assert c.execute("SELECT COUNT(*) FROM run_events WHERE kind=?",
                                 ("run.question." + kind,)).fetchone()[0] == 2
            assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        assert len(readings) >= 5 and max(readings) < 0.3, readings
        print(json.dumps({"contention_probe_count": len(readings), "http_worker_max_ms": round(max(readings)*1000, 1)}))
    finally:
        stop_probe.set()
        await task
        await asyncio.gather(caller.close(), worker.close())
        await client.close()


@pytest.mark.asyncio
async def test_saved_stop_busy_is_unconfirmed_not_a_no_write_busy(tmp_path, monkeypatch):
    server = make_server(tmp_path)
    session, run = active(server.session_store)
    q = server._questions().create(request_id=run.request_id, agent_id="lily", payload=spec())
    server._runtime_map = lambda: {"lily": SimpleNamespace(global_config=server.global_config)}
    async def stop(*args, **kwargs):
        with server.session_store._connection() as c:
            row = c.execute("SELECT state,answer_json FROM run_questions").fetchone()
            assert row["state"] == "answered" and json.loads(row["answer_json"])["system_action"] == "stop"
        raise DatabaseWorkBusy("stop claim is busy after answer commit")
    monkeypatch.setattr("orchestrator.workbench_api.execute_local_command", stop)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        response = await client.post(f"/api/v1/sessions/{session['session_id']}/questions/{q['question_id']}/answer",
            headers={"X-Workbench-Token": SECRET},
            json={"idempotency_key": "stop", "option_id": QUESTION_STOP_ID})
        assert response.status == 503
        assert (await response.json())["error_code"] == "question_stop_unconfirmed"
        assert server.session_store.get_run(run.run_id)["state"] == "running"
    finally:
        await client.close()


def test_busy_after_commit_keeps_unknown_outcome_and_original_idempotency(tmp_path, monkeypatch):
    from tests.test_run_question_connectors import running
    service, session, run, q = running(tmp_path)
    original = service.store._connection
    @contextmanager
    def committed_but_no_receipt():
        with original() as connection:
            yield connection
        error = sqlite3.OperationalError("synthetic post-commit busy")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error
    payload = {"idempotency_key": "one-answer", "option_id": "blue"}
    monkeypatch.setattr(service.store, "_connection", committed_but_no_receipt)
    with pytest.raises(sqlite3.OperationalError):
        service.answer(session_id=session["session_id"], owner_id="user:7", question_id=q["question_id"], payload=payload)
    monkeypatch.setattr(service.store, "_connection", original)
    assert service.answer(session_id=session["session_id"], owner_id="user:7",
                          question_id=q["question_id"], payload=payload)["state"] == "answered"
    with original() as c:
        assert c.execute("SELECT COUNT(*) FROM run_events WHERE kind='run.question.answered'").fetchone()[0] == 1
# HASHI-Fingerprint: hfp_667a243b346440449eb7a0ad8ee75437


@pytest.mark.asyncio
async def test_telegram_saved_stop_busy_stays_unconfirmed(tmp_path, monkeypatch):
    from orchestrator import runtime_control
    from orchestrator.frontend_run_questions import question_text
    from orchestrator.runtime_run_questions import handle_callback
    from tests.test_run_question_connectors import running
    service, session, run, q = running(tmp_path)
    runtime = telegram_runtime(tmp_path, service.store, "zelda")
    index = next(i for i, option in enumerate(q["options"]) if option.get("system_action") == "stop")
    update = callback(q, index=index)
    async def stop(*args):
        with service.store._connection() as c:
            assert c.execute("SELECT state FROM run_questions").fetchone()[0] == "answered"
        raise DatabaseWorkBusy("stop claim busy after commit")
    monkeypatch.setattr(runtime_control, "cmd_stop", stop)
    await handle_callback(runtime, update, None)
    assert update.callback_query.answer.call_args.args[0] == question_text("en")["failed"]
    with service.store._connection() as c:
        assert c.execute("SELECT COUNT(*) FROM run_events WHERE kind='run.question.answered'").fetchone()[0] == 1
    assert service.store.get_run(run.run_id)["state"] == "running"


@pytest.mark.asyncio
async def test_telegram_bound_text_reply_busy_is_not_a_saved_answer_or_new_run(tmp_path):
    from orchestrator.frontend_run_questions import QuestionRenditions
    from orchestrator.runtime_run_questions import handle_text_reply
    from tests.test_run_question_connectors import running
    service, session, run, q = running(tmp_path)
    runtime = telegram_runtime(tmp_path, service.store, "zelda")
    QuestionRenditions(service.store).record(question_id=q["question_id"], endpoint_id="test-endpoint",
        chat_id="7", transport_message_id="42", owner_id="user:7", agent_id="zelda", request_id=run.request_id)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=7), effective_user=SimpleNamespace(id=7),
        message=SimpleNamespace(text="blue", message_id=43,
            reply_to_message=SimpleNamespace(from_user=SimpleNamespace(id=123), message_id=42)))
    with writer_lock(service.store):
        started = time.monotonic()
        assert await handle_text_reply(runtime, update) is True
        assert time.monotonic() - started < 0.6
        assert runtime._reply_text.call_args.args[1] == question_error_text("question_storage_busy", "en")
        with service.store._connection() as c:
            assert c.execute("SELECT state FROM run_questions").fetchone()[0] == "pending"
            assert c.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_rendition_schema_and_write_reservations_are_short_and_retryable(tmp_path, monkeypatch):
    from orchestrator.frontend_run_questions import QuestionRenditions
    from tests.test_run_question_connectors import running
    service, session, run, q = running(tmp_path)
    with writer_lock(service.store):
        started = time.monotonic()
        with pytest.raises(DatabaseWorkBusy):
            QuestionRenditions(service.store)
        assert time.monotonic() - started < 0.6
        assert not getattr(service.store, "_question_renditions_schema_ready", False)
    renditions = QuestionRenditions(service.store)
    # Skip the earlier authenticated read to contend at the rendition INSERT itself.
    monkeypatch.setattr(RunQuestions, "telegram_question", lambda *args, **kwargs: q)
    with writer_lock(service.store):
        started = time.monotonic()
        with pytest.raises(DatabaseWorkBusy):
            renditions.record(question_id=q["question_id"], endpoint_id="test-endpoint", chat_id="7",
                              transport_message_id="42", owner_id="user:7", agent_id="zelda", request_id=run.request_id)
        assert time.monotonic() - started < 0.6
        with service.store._connection() as c:
            assert c.execute("SELECT COUNT(*) FROM frontend_question_renditions").fetchone()[0] == 0
    for _ in range(2):
        renditions.record(question_id=q["question_id"], endpoint_id="test-endpoint", chat_id="7",
                          transport_message_id="42", owner_id="user:7", agent_id="zelda", request_id=run.request_id)
    with service.store._connection() as c:
        assert c.execute("SELECT COUNT(*) FROM frontend_question_renditions").fetchone()[0] == 1
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
