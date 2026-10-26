"""Regression scenarios for the MSI invisible-card and hour-long-wait incident."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from adapters.codex_run_questions import NativeRunQuestions
from orchestrator.run_questions import QuestionError
from orchestrator.workbench_api import WorkbenchApiServer
from tests.test_config import _minimal_flex_agent, _write_base_files
from tests.test_run_question_connectors import running
from tests.test_run_questions import active, spec


@pytest.mark.asyncio
async def test_native_question_expires_after_five_minutes_without_inventing_an_answer(tmp_path):
    service, session, run, _ = running(tmp_path)
    now = [1000.0]
    service.clock = lambda: now[0]
    native = NativeRunQuestions(service, request_id=run.request_id, agent_id="zelda", interval=.001)
    task = asyncio.create_task(native.answer(42, {"itemId": "expiry", "questions": [
        {"id": "reference", "question": "Which reference?", "options": [
            {"label": "Locate it", "description": "Recommended"}]}]}))
    try:
        await asyncio.sleep(.02)
        question = next(q for q in service.list(session_id=session["session_id"], owner_id="user:7")
                        if q["question"] == "Which reference?")
        now[0] += 301
        current = service.get_answer(request_id=run.request_id, agent_id="zelda",
                                     question_id=question["question_id"])
        assert current["state"] == "expired"
        assert current["answer"] is None
        with pytest.raises(QuestionError) as error:
            await asyncio.wait_for(task, 1)
        resolution = error.value.resolution
        assert resolution["questions"][0]["state"] == "expired"
        assert resolution["questions"][0]["answer"] is None
        assert service.store.get_run(run.run_id)["state"] == "running"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_legacy_hour_deadline_is_capped_when_reloaded(tmp_path):
    service, session, run, q = running(tmp_path)
    with service.store._connection() as connection:
        connection.execute("UPDATE run_questions SET expires_epoch=expires_epoch+3600 WHERE question_id=?",
                           (q["question_id"],))
    from datetime import datetime
    created = datetime.fromisoformat(q["created_at"].replace("Z", "+00:00")).timestamp()
    service.clock = lambda: created + 301
    assert service.list(session_id=session["session_id"], owner_id="user:7")[0]["state"] == "expired"


@pytest.mark.asyncio
async def test_authenticated_remote_bridge_can_list_and_answer_without_preconfigured_admin_key(tmp_path, monkeypatch):
    from orchestrator.config import ConfigManager
    from remote.api import server as remote_server

    config, secret_path = _write_base_files(tmp_path, _minimal_flex_agent())
    original = {"authorized_telegram_id": 7, "hashi_remote_shared_token": "isolated-network-fixture",
                "other_credential": "preserve-this-value"}
    secret_path.write_text(json.dumps(original), encoding="utf-8")
    manager = ConfigManager(config, secret_path, bridge_home=tmp_path)
    cfg, _, secrets = manager.load()
    token = secrets.get("workbench_admin_token")
    assert isinstance(token, str) and len(token) >= 32
    saved = json.loads(secret_path.read_text(encoding="utf-8"))
    assert all(saved[key] == value for key, value in original.items())
    assert manager.load()[2]["workbench_admin_token"] == token
    monkeypatch.setattr(remote_server, "_control_hashi_root", str(tmp_path))
    assert remote_server._workbench_admin_token() == token

    server = WorkbenchApiServer(config, cfg, secrets=secrets, reconcile_session_runs=False)
    session, run = active(server.session_store)
    question = server._questions().create(request_id=run.request_id, agent_id="lily", payload=spec())
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        path = f"/api/v1/sessions/{session['session_id']}/questions"
        assert (await client.get(path)).status == 401
        response = await client.get(path, headers={"X-Workbench-Token": token})
        assert response.status == 200, await response.text()
        assert (await response.json())["questions"][0]["question_id"] == question["question_id"]
        reply = await client.post(path + f"/{question['question_id']}/answer",
            headers={"X-Workbench-Token": token},
            json={"idempotency_key": "answer-fixture", "text": "Use the existing reference"})
        assert reply.status == 200, await reply.text()
        answer = server._questions().get_answer(request_id=run.request_id, agent_id="lily",
                                                question_id=question["question_id"])
        assert answer["state"] == "consumed"
        assert answer["answer"]["grants_permissions"] is False
        assert server.session_store.get_run(run.run_id)["state"] == "running"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_telegram_card_has_a_durable_rendition_and_closes_on_expiry(tmp_path, monkeypatch):
    from tests.test_frontend_attachment_tool import _ToolRuntime
    from orchestrator import telegram_delivery_failover
    from orchestrator.frontend_incremental_delivery import dispatch_pending_telegram_deliverables

    service, session, run, question = running(tmp_path)
    runtime = _ToolRuntime(tmp_path, service.store)
    runtime.name = "zelda"
    runtime.global_config.ui_language = "zh-CN"
    runtime.app.bot.id = 123
    runtime.app.bot.edit_message_text = AsyncMock()
    warnings = []
    runtime.logger.warning = lambda *args: warnings.append(args)
    monkeypatch.setattr(telegram_delivery_failover, "handle_blocked_send",
                        AsyncMock(return_value=False))
    assert await dispatch_pending_telegram_deliverables(runtime) == 1
    assert not any("reply-reference persistence failed" in str(w) for w in warnings)
    assert runtime.app.bot.messages[0]["reply_markup"] is not None
    # Restore through an independent store connection before the card expires.
    from orchestrator.session_store import SessionStore
    runtime.session_store = SessionStore(service.store.db_path,
                                         instance_id=service.store.instance_id)

    from datetime import datetime
    created = datetime.fromisoformat(question["created_at"].replace("Z", "+00:00")).timestamp()
    monkeypatch.setattr("orchestrator.run_questions.time.time", lambda: created + 301)
    await dispatch_pending_telegram_deliverables(runtime)
    assert service.list(session_id=session["session_id"], owner_id="user:7")[0]["state"] == "expired"
    update = runtime.app.bot.edit_message_text.call_args.kwargs
    assert update["message_id"] == 101
    assert "过期" in update["text"]
    assert "Which colour?" in update["text"]
    assert len(runtime.app.bot.messages) == 1


@pytest.mark.asyncio
async def test_native_expiry_replies_to_original_rpc_with_a_non_answer(tmp_path):
    from adapters.codex_interactive import InteractiveTurn
    from tests.test_codex_run_questions import RPCProcess

    service, session, run, _ = running(tmp_path)
    clock = [1000.0]
    service.clock = lambda: clock[0]
    adapter = SimpleNamespace(config=SimpleNamespace(name="zelda", model="fixture-model"),
        _session_mode=True, _session_id="thread-1", effective_workdir=tmp_path,
        _touch_activity=lambda: None)
    bridge = InteractiveTurn(adapter, service, run.request_id, "continue original work", [])
    bridge.questions.interval = .001
    proc = RPCProcess()
    async def collect():
        return [json.loads(line) async for line in bridge.lines(proc, "medium")]
    task = asyncio.create_task(collect())
    try:
        for _ in range(100):
            questions = service.list(session_id=session["session_id"], owner_id="user:7")
            if any(q["question"] == "Continue with blue?" for q in questions):
                break
            await asyncio.sleep(.005)
        clock[0] += 301
        events = await asyncio.wait_for(task, 2)
        reply = next(message for message in proc.sent if message.get("id") == 42)
        assert "result" not in reply
        assert reply["error"]["code"] == -32001
        resolution = reply["error"]["data"]
        assert resolution["reason"] == "expired"
        assert resolution["questions"][0]["answer"] is None
        assert sum(message.get("method") == "turn/start" for message in proc.sent) == 1
        assert events[-1]["type"] == "turn.completed"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_bootstrap_retains_existing_key_and_rejects_a_stale_secret_snapshot(tmp_path):
    from orchestrator.config_json import read_config_json, ConfigConflictError
    from orchestrator.workbench_auth import ensure_local_api_credential

    path = tmp_path / "secrets.json"
    value = {"hashi_remote_shared_token": "fixture", "workbench_admin_token": "existing-local-key"}
    original = ("\ufeff" + json.dumps(value) + "\r\n").encode()
    path.write_bytes(original)
    snapshot = read_config_json(path)
    assert ensure_local_api_credential(snapshot, profile="personal")["workbench_admin_token"] == "existing-local-key"
    assert path.read_bytes() == original
    value.pop("workbench_admin_token")
    path.write_text(json.dumps(value), encoding="utf-8")
    snapshot = read_config_json(path)
    value["unrelated"] = "external-winner"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ConfigConflictError):
        ensure_local_api_credential(snapshot, profile="personal")
    assert json.loads(path.read_text()) == value


@pytest.mark.asyncio
async def test_her_question_transport_failure_releases_guard_instead_of_retrying_forever(monkeypatch):
    from adapters.her_v2_provider import _CognitiveControlToolRegistry
    from tests.test_her_v2_cognitive_control import _RunQuestionRegistry, _request
    from orchestrator.her_v2.models import Stage
    from tools.registry import ToolResult

    base = _RunQuestionRegistry()
    registry = _CognitiveControlToolRegistry(base, _request(Stage.EXECUTION))
    await registry.execute("ask_user", {"question": "Critical or optional?"}, "ask-fixture")
    failed_calls = []
    async def fail(name, arguments, tool_call_id=""):
        failed_calls.append(arguments)
        return ToolResult(tool_call_id=tool_call_id, output="Error: unavailable", is_error=True)
    base.execute = fail
    monkeypatch.setattr("adapters.her_v2_provider.asyncio.sleep", AsyncMock())
    result = await registry.wait_before_terminal_response()
    assert result["questions"][0]["answer"] is None
    assert result["questions"][0]["resolution_reason"] == "question_transport_unavailable"
    assert len(failed_calls) <= 4
    assert await registry.wait_before_terminal_response() is None


def test_answer_saved_before_deadline_is_not_lost_when_consumed_after_it(tmp_path):
    service, session, run, _ = running(tmp_path)
    clock = [1000.0]
    service.clock = lambda: clock[0]
    question = service.create(request_id=run.request_id, agent_id="zelda", payload=spec("timely"))
    clock[0] = 1299.0
    service.answer(session_id=session["session_id"], owner_id="user:7",
        question_id=question["question_id"],
        payload={"idempotency_key": "timely-answer", "text": "Use the established plan"})
    clock[0] = 1301.0
    answer = service.get_answer(request_id=run.request_id, agent_id="zelda",
                                question_id=question["question_id"])
    assert answer["state"] == "consumed"
    assert answer["answer"]["text"] == "Use the established plan"


@pytest.mark.asyncio
async def test_telegram_text_reply_can_resolve_a_card_without_quoted_keyboard(tmp_path, monkeypatch):
    from tests.test_frontend_attachment_tool import _ToolRuntime
    from orchestrator import telegram_delivery_failover
    from orchestrator.frontend_incremental_delivery import dispatch_pending_telegram_deliverables
    from orchestrator.runtime_run_questions import handle_text_reply

    service, session, run, question = running(tmp_path)
    runtime = _ToolRuntime(tmp_path, service.store)
    runtime.name = "zelda"
    runtime.app.bot.id = 123
    runtime._reply_text = AsyncMock()
    monkeypatch.setattr(telegram_delivery_failover, "handle_blocked_send",
                        AsyncMock(return_value=False))
    await dispatch_pending_telegram_deliverables(runtime)
    original = SimpleNamespace(from_user=SimpleNamespace(id=123), message_id=101,
                               reply_markup=None)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=7),
        effective_user=SimpleNamespace(id=7),
        message=SimpleNamespace(reply_to_message=original, text="继续现有方案", message_id=102))
    assert await handle_text_reply(runtime, update) is True
    answer = service.get_answer(request_id=run.request_id, agent_id="zelda",
                                question_id=question["question_id"])
    assert answer["answer"]["text"] == "继续现有方案"
    with service.store._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


@pytest.mark.parametrize("outcome", ["failed", "unknown"])
def test_all_delivery_failures_release_question_without_a_default_answer(tmp_path, outcome):
    service, session, run, question = running(tmp_path)
    with service.store._connection() as connection:
        connection.execute("UPDATE connector_delivery_tasks SET state=? WHERE run_id=?",
                           (outcome, run.run_id))
    resolution = service.get_answer(request_id=run.request_id, agent_id="zelda",
                                    question_id=question["question_id"])
    assert resolution["state"] == "cancelled"
    assert resolution["resolution_reason"] == ("delivery_failed" if outcome == "failed" else "delivery_unknown")
    assert resolution["answer"] is None
    assert service.store.get_run(run.run_id)["state"] == "running"


@pytest.mark.asyncio
async def test_reply_to_an_ordinary_bot_message_is_not_mistaken_for_a_question(tmp_path):
    from orchestrator.session_store import SessionStore
    from tests.test_frontend_attachment_tool import _ToolRuntime
    from orchestrator.runtime_run_questions import handle_text_reply
    store = SessionStore(tmp_path / "fresh.sqlite3", instance_id="test")
    runtime = _ToolRuntime(tmp_path, store)
    runtime.app.bot.id = 123
    original = SimpleNamespace(from_user=SimpleNamespace(id=123), message_id=100, reply_markup=None)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=7),
        message=SimpleNamespace(reply_to_message=original, text="ordinary follow-up"))
    assert await handle_text_reply(runtime, update) is False
