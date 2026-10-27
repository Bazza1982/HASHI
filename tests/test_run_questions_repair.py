"""Regression scenarios for the MSI invisible-card and hour-long-wait incident."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from adapters.codex_run_questions import NativeRunQuestions
from orchestrator.run_questions import QuestionError, UnansweredQuestion
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


@pytest.mark.asyncio
async def test_native_batch_expiry_preserves_authenticated_partial_answers(tmp_path):
    service, session, run, _ = running(tmp_path)
    now = [1000.0]
    service.clock = lambda: now[0]
    native = NativeRunQuestions(
        service,
        request_id=run.request_id,
        agent_id="zelda",
        interval=.001,
    )
    task = asyncio.create_task(native.answer(43, {
        "itemId": "partial-expiry",
        "questions": [
            {"id": "reference", "question": "Which reference?", "options": []},
            {"id": "colour", "question": "Which colour?", "options": [
                {"label": "Blue", "description": "Recommended"}]},
        ],
    }))
    try:
        colour = reference = None
        for _ in range(100):
            rows = service.list(
                session_id=session["session_id"], owner_id="user:7"
            )
            colour = next(
                (item for item in rows if item["question"] == "Which colour?"),
                None,
            )
            reference = next(
                (item for item in rows if item["question"] == "Which reference?"),
                None,
            )
            if colour is not None and reference is not None:
                break
            await asyncio.sleep(.005)
        assert colour is not None and reference is not None
        service.answer(
            session_id=session["session_id"],
            owner_id="user:7",
            question_id=colour["question_id"],
            payload={
                "idempotency_key": "partial-colour",
                "option_id": "option_0",
            },
        )
        for _ in range(100):
            rows = service.list(
                session_id=session["session_id"], owner_id="user:7"
            )
            if next(
                item for item in rows if item["question_id"] == colour["question_id"]
            )["state"] == "consumed":
                break
            await asyncio.sleep(.005)
        now[0] += 301
        with pytest.raises(UnansweredQuestion) as error:
            await asyncio.wait_for(task, 1)
        assert error.value.resolution["partial_answers"] == {
            "colour": {"answers": ["Blue"]}
        }
        rows = service.list(
            session_id=session["session_id"], owner_id="user:7"
        )
        saved = next(
            item for item in rows if item["question_id"] == colour["question_id"]
        )
        assert saved["state"] == "consumed"
        assert saved["answer"]["author"] == "authenticated_user"
        assert next(
            item for item in rows if item["question_id"] == reference["question_id"]
        )["state"] == "expired"
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
    cfg, _, secrets = manager.load(bootstrap_local_api_credential=True)
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

def test_config_reads_do_not_repeat_local_api_credential_maintenance(tmp_path, monkeypatch):
    from orchestrator import workbench_auth
    from orchestrator.config import ConfigManager

    config, secret_path = _write_base_files(tmp_path, _minimal_flex_agent())
    secret_path.write_text(
        json.dumps({
            "hashi_remote_shared_token": "fixture",
            "workbench_admin_token": "existing-local-key",
        }),
        encoding="utf-8",
    )
    maintenance_calls = []

    def observe_maintenance(document, *, profile, remote_runtime_sids=None):
        maintenance_calls.append((profile, remote_runtime_sids))
        return document

    monkeypatch.setattr(
        workbench_auth,
        "ensure_local_api_credential",
        observe_maintenance,
    )
    manager = ConfigManager(config, secret_path, bridge_home=tmp_path)
    manager.load()
    manager.load()
    assert maintenance_calls == []
    manager.load(bootstrap_local_api_credential=True)
    assert maintenance_calls == [("personal", None)]


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


def test_confirmed_delivery_failure_releases_question_without_a_default_answer(tmp_path):
    service, session, run, question = running(tmp_path)
    with service.store._connection() as connection:
        connection.execute("UPDATE connector_delivery_tasks SET state='failed' WHERE run_id=?",
                           (run.run_id,))
    resolution = service.get_answer(request_id=run.request_id, agent_id="zelda",
                                    question_id=question["question_id"])
    assert resolution["state"] == "cancelled"
    assert resolution["resolution_reason"] == "delivery_failed"
    assert resolution["answer"] is None
    assert service.store.get_run(run.run_id)["state"] == "running"


def test_unknown_question_delivery_waits_without_cancellation_or_duplicate_send(tmp_path):
    from orchestrator.run_questions import RunQuestions
    from orchestrator.session_store import SessionStore

    service, session, run, question = running(tmp_path)
    with service.store._connection() as connection:
        task = connection.execute(
            "SELECT d.task_id,d.event_id,d.attempt_count "
            "FROM connector_delivery_tasks AS d "
            "JOIN run_events AS e ON e.event_id=d.event_id "
            "WHERE d.run_id=? AND d.connector_id='telegram' "
            "AND e.kind='run.question.created' "
            "AND json_extract(e.detail_json,'$.question_id')=?",
            (run.run_id, question["question_id"]),
        ).fetchone()
        connection.execute(
            "UPDATE connector_delivery_tasks SET state='unknown' WHERE task_id=?",
            (task["task_id"],),
        )

    reopened_store = SessionStore(
        service.store.db_path,
        instance_id=service.store.instance_id,
    )
    reopened = RunQuestions(reopened_store, clock=service.clock)
    unresolved = reopened.get_answer(
        request_id=run.request_id,
        agent_id="zelda",
        question_id=question["question_id"],
    )
    assert unresolved["state"] == "pending"
    assert unresolved.get("resolution_reason") is None
    assert reopened.pending_telegram_deliveries(agent_id="zelda") == []
    assert reopened_store.claim_delivery_outbox(
        session_id=session["session_id"],
        owner_id="user:7",
        worker_id="question-retry-must-not-run",
        event_id=task["event_id"],
        connector_id="telegram",
        limit=1,
    ) == []
    with reopened_store._connection() as connection:
        current = connection.execute(
            "SELECT state,attempt_count FROM connector_delivery_tasks WHERE task_id=?",
            (task["task_id"],),
        ).fetchone()
    assert dict(current) == {"state": "unknown", "attempt_count": task["attempt_count"]}

    from datetime import datetime
    created = datetime.fromisoformat(
        question["created_at"].replace("Z", "+00:00")
    ).timestamp()
    reopened.clock = lambda: created + 301
    expired = reopened.get_answer(
        request_id=run.request_id,
        agent_id="zelda",
        question_id=question["question_id"],
    )
    assert expired["state"] == "expired"
    assert expired.get("resolution_reason") is None


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


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL contract")
def test_bootstrap_preserves_and_repairs_the_registered_remote_runtime_acl(tmp_path):
    from orchestrator.config_json import read_config_json
    from orchestrator.workbench_auth import ensure_local_api_credential
    from tools.private_files import protect_private_file

    def acl() -> str:
        return subprocess.run(
            ["icacls.exe", str(path)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    path = tmp_path / "secrets.json"
    path.write_text(
        json.dumps({"hashi_remote_shared_token": "fixture"}),
        encoding="utf-8",
    )
    first = ensure_local_api_credential(
        read_config_json(path),
        profile="personal",
        remote_runtime_sids=("S-1-5-19",),
    )
    saved = path.read_bytes()
    assert first["workbench_admin_token"]
    assert "NT AUTHORITY\\LOCAL SERVICE:(F)" in acl()

    # Reproduce the incident: a later private publication removes the
    # separately supervised Remote principal while leaving bytes intact.
    protect_private_file(path)
    assert "NT AUTHORITY\\LOCAL SERVICE:(F)" not in acl()

    repaired = ensure_local_api_credential(
        read_config_json(path),
        profile="personal",
        remote_runtime_sids=("S-1-5-19",),
    )
    assert repaired["workbench_admin_token"] == first["workbench_admin_token"]
    assert path.read_bytes() == saved
    assert "NT AUTHORITY\\LOCAL SERVICE:(F)" in acl()


def test_configured_owner_direct_chat_bootstraps_after_worker_restart(tmp_path):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.frontend_delivery import (
        freeze_run_delivery_route,
        route_destination,
    )
    from orchestrator.session_store import SessionStore

    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="MSI")
    store.ensure_default_session(owner_id="user:8872", agent_id="hashiko")
    runtime = SimpleNamespace(
        name="hashiko",
        global_config=SimpleNamespace(authorized_id=8872),
        session_store=store,
        _authorized_telegram_ids=(7666, 8872),
        _active_chat_ids={},
    )
    runtime._is_authorized_user = lambda user_id: user_id in runtime._authorized_telegram_ids

    assert (
        store.latest_channel_binding(
            owner_id="user:8872",
            agent_id="hashiko",
            surface="telegram",
            actor_id=8872,
        )
        is None
    )
    chat_id = FlexibleAgentRuntime._primary_chat_id(runtime)
    assert chat_id == 8872

    workbench_route = freeze_run_delivery_route(
        message_source_id="api",
        session_surface="workbench",
        session_channel_key="workbench:primary",
        chat_id=chat_id,
        telegram_requested=True,
    )
    assert route_destination(workbench_route, "telegram") == {
        "surface": "telegram",
        "channel_key": "8872",
    }

    binding = store.latest_channel_binding(
        owner_id="user:8872",
        agent_id="hashiko",
        surface="telegram",
        actor_id=8872,
    )
    assert binding is not None
    assert binding["channel_key"] == "8872"
    assert binding["actor_role"] == "owner"

    restarted = SimpleNamespace(
        name="hashiko",
        global_config=SimpleNamespace(authorized_id=8872),
        session_store=SessionStore(store.db_path, instance_id="MSI"),
        _authorized_telegram_ids=(7666, 8872),
        _active_chat_ids={},
    )
    assert FlexibleAgentRuntime._primary_chat_id(restarted) == 8872


def test_owner_direct_chat_bootstrap_revalidates_authorization_and_egress(tmp_path):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.session_store import SessionStore

    revoked_store = SessionStore(tmp_path / "revoked.sqlite3", instance_id="MSI")
    revoked_store.ensure_default_session(owner_id="user:8872", agent_id="hashiko")
    revoked = SimpleNamespace(
        name="hashiko",
        global_config=SimpleNamespace(authorized_id=8872),
        session_store=revoked_store,
        _authorized_telegram_ids=(7666,),
        _active_chat_ids={},
    )
    assert FlexibleAgentRuntime._primary_chat_id(revoked) == 0

    denied_store = SessionStore(tmp_path / "denied.sqlite3", instance_id="MSI")
    denied_store.ensure_default_session(owner_id="user:8872", agent_id="hashiko")
    denied_gate = SimpleNamespace(
        check_egress=lambda *_args, **_kwargs: SimpleNamespace(
            allowed=False, reason="channel_disabled"
        )
    )
    denied = SimpleNamespace(
        name="hashiko",
        global_config=SimpleNamespace(authorized_id=8872),
        session_store=denied_store,
        _authorized_telegram_ids=(8872,),
        _active_chat_ids={},
        _channel_gate=denied_gate,
    )
    assert FlexibleAgentRuntime._primary_chat_id(denied) == 0

    assert (
        denied_store.latest_channel_binding(
            owner_id="user:8872",
            agent_id="hashiko",
            surface="telegram",
            actor_id=8872,
        )
        is None
    )


def test_owner_telegram_chat_survives_restart_and_is_revalidated_after_revocation(tmp_path):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.session_store import SessionStore

    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="MSI")
    store.ensure_default_session(owner_id="user:8872", agent_id="hashiko")
    runtime = SimpleNamespace(
        name="hashiko",
        global_config=SimpleNamespace(authorized_id=8872),
        session_store=store,
        _authorized_telegram_ids=(8872, 7666),
        _active_chat_ids={},
    )
    runtime._is_authorized_user = lambda user_id: user_id in runtime._authorized_telegram_ids
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=8872),
        effective_chat=SimpleNamespace(id=8872),
    )
    FlexibleAgentRuntime._record_active_chat(runtime, update)
    assert FlexibleAgentRuntime._primary_chat_id(runtime) == 8872

    restarted = SimpleNamespace(
        name="hashiko",
        global_config=SimpleNamespace(authorized_id=8872),
        session_store=SessionStore(store.db_path, instance_id="MSI"),
        _authorized_telegram_ids=(8872, 7666),
        _active_chat_ids={},
    )
    assert FlexibleAgentRuntime._primary_chat_id(restarted) == 8872

    revoked = SimpleNamespace(
        name="hashiko",
        global_config=SimpleNamespace(authorized_id=8872),
        session_store=SessionStore(store.db_path, instance_id="MSI"),
        _authorized_telegram_ids=(7666,),
        _active_chat_ids={},
    )
    assert FlexibleAgentRuntime._primary_chat_id(revoked) == 0

    denied_gate = SimpleNamespace(
        check_egress=lambda *_args, **_kwargs: SimpleNamespace(
            allowed=False, reason="channel_disabled"
        )
    )
    channel_revoked = SimpleNamespace(
        name="hashiko",
        global_config=SimpleNamespace(authorized_id=8872),
        session_store=SessionStore(store.db_path, instance_id="MSI"),
        _authorized_telegram_ids=(8872, 7666),
        _active_chat_ids={},
        _channel_gate=denied_gate,
    )
    assert FlexibleAgentRuntime._primary_chat_id(channel_revoked) == 0


def test_secondary_telegram_user_cannot_replace_owner_proactive_route(tmp_path):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.session_store import SessionStore

    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="MSI")
    store.ensure_default_session(owner_id="user:8872", agent_id="hashiko")
    runtime = SimpleNamespace(
        name="hashiko",
        global_config=SimpleNamespace(authorized_id=8872),
        session_store=store,
        _authorized_telegram_ids=(8872, 7666),
        _active_chat_ids={},
    )
    runtime._is_authorized_user = lambda user_id: user_id in runtime._authorized_telegram_ids
    for actor in (8872, 7666):
        FlexibleAgentRuntime._record_active_chat(
            runtime,
            SimpleNamespace(
                effective_user=SimpleNamespace(id=actor),
                effective_chat=SimpleNamespace(id=actor),
            ),
        )
    assert FlexibleAgentRuntime._primary_chat_id(runtime) == 8872
    # A secondary actor in the same group chat cannot erase the owner's
    # authenticated route evidence for that shared channel key.
    FlexibleAgentRuntime._record_active_chat(
        runtime,
        SimpleNamespace(
            effective_user=SimpleNamespace(id=8872),
            effective_chat=SimpleNamespace(id=-1008872),
        ),
    )
    # Select the owner's direct chat again after the older group binding.
    FlexibleAgentRuntime._record_active_chat(
        runtime,
        SimpleNamespace(
            effective_user=SimpleNamespace(id=8872),
            effective_chat=SimpleNamespace(id=8872),
        ),
    )
    FlexibleAgentRuntime._record_active_chat(
        runtime,
        SimpleNamespace(
            effective_user=SimpleNamespace(id=7666),
            effective_chat=SimpleNamespace(id=-1008872),
        ),
    )
    with store._connection() as connection:
        same_chat = connection.execute(
            "SELECT channel_key,actor_id,actor_role FROM channel_bindings "
            "WHERE instance_id='MSI' AND owner_id='user:8872' "
            "AND agent_id='hashiko' AND surface='telegram' "
            "AND channel_key='-1008872'"
        ).fetchone()
    assert same_chat is not None
    assert same_chat["actor_id"] == "8872"
    assert same_chat["actor_role"] == "owner"
    assert same_chat["channel_key"] == "-1008872"
    assert FlexibleAgentRuntime._primary_chat_id(runtime) == 8872

    only_secondary = SessionStore(
        tmp_path / "secondary.sqlite3", instance_id="MSI"
    )
    only_secondary.ensure_default_session(owner_id="user:8872", agent_id="hashiko")
    secondary_runtime = SimpleNamespace(
        name="hashiko",
        global_config=SimpleNamespace(authorized_id=8872),
        session_store=only_secondary,
        _authorized_telegram_ids=(8872, 7666),
        _active_chat_ids={},
    )
    secondary_runtime._is_authorized_user = (
        lambda user_id: user_id in secondary_runtime._authorized_telegram_ids
    )
    FlexibleAgentRuntime._record_active_chat(
        secondary_runtime,
        SimpleNamespace(
            effective_user=SimpleNamespace(id=7666),
            effective_chat=SimpleNamespace(id=7666),
        ),
    )
    assert FlexibleAgentRuntime._primary_chat_id(secondary_runtime) == 8872


def test_owner_group_binding_never_becomes_proactive_telegram_route(tmp_path):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.session_store import SessionStore

    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="MSI")
    store.ensure_default_session(owner_id="user:8872", agent_id="hashiko")
    runtime = SimpleNamespace(
        name="hashiko",
        global_config=SimpleNamespace(authorized_id=8872),
        session_store=store,
        _authorized_telegram_ids=(8872,),
        _active_chat_ids={},
    )
    runtime._is_authorized_user = lambda user_id: user_id == 8872
    for chat_id in (8872, -1008872):
        FlexibleAgentRuntime._record_active_chat(
            runtime,
            SimpleNamespace(
                effective_user=SimpleNamespace(id=8872),
                effective_chat=SimpleNamespace(id=chat_id),
            ),
        )

    assert store.latest_channel_binding(
        owner_id="user:8872",
        agent_id="hashiko",
        surface="telegram",
        actor_id=8872,
    )["channel_key"] == "-1008872"
    assert FlexibleAgentRuntime._primary_chat_id(runtime) == 8872


def test_configured_replacement_owner_promotes_existing_direct_route(tmp_path):
    from orchestrator import runtime_session
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.frontend_delivery import (
        freeze_run_delivery_route,
        route_destination,
    )
    from orchestrator.session_store import SessionStore

    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="MSI")
    stable_owner = "user:8872"
    store.ensure_default_session(owner_id=stable_owner, agent_id="hashiko")
    before = SimpleNamespace(
        name="hashiko",
        global_config=SimpleNamespace(
            authorized_id=8872,
            owner_scope_id=stable_owner,
        ),
        session_store=store,
        _authorized_telegram_ids=(8872, 7666),
        _active_chat_ids={},
    )
    before._is_authorized_user = (
        lambda user_id: user_id in before._authorized_telegram_ids
    )
    FlexibleAgentRuntime._record_active_chat(
        before,
        SimpleNamespace(
            effective_user=SimpleNamespace(id=7666),
            effective_chat=SimpleNamespace(id=7666),
        ),
    )
    secondary = store.latest_channel_binding(
        owner_id=stable_owner,
        agent_id="hashiko",
        surface="telegram",
        actor_id=7666,
    )
    assert secondary is not None
    assert secondary["actor_role"] == "secondary"

    promoted = SimpleNamespace(
        name="hashiko",
        global_config=SimpleNamespace(
            authorized_id=7666,
            owner_scope_id=stable_owner,
        ),
        session_store=SessionStore(store.db_path, instance_id="MSI"),
        _authorized_telegram_ids=(7666,),
        _active_chat_ids={},
    )
    assert runtime_session.owner_id(promoted) == stable_owner
    assert FlexibleAgentRuntime._primary_chat_id(promoted) == 7666

    owner_binding = promoted.session_store.latest_channel_binding(
        owner_id=stable_owner,
        agent_id="hashiko",
        surface="telegram",
        actor_id=7666,
    )
    assert owner_binding is not None
    assert owner_binding["actor_role"] == "owner"
    route = freeze_run_delivery_route(
        message_source_id="api",
        session_surface="workbench",
        session_channel_key="default",
        chat_id=FlexibleAgentRuntime._primary_chat_id(promoted),
        telegram_requested=True,
    )
    assert route_destination(route, "telegram") == {
        "surface": "telegram",
        "channel_key": "7666",
    }


def test_telegram_actor_binding_schema_upgrades_from_version_27(tmp_path):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.session_store import SessionStore

    db_path = tmp_path / "sessions.sqlite3"
    original = SessionStore(db_path, instance_id="MSI")
    session = original.ensure_default_session(
        owner_id="user:8872", agent_id="hashiko"
    )
    original.bind_channel(
        owner_id="user:8872",
        agent_id="hashiko",
        surface="telegram",
        channel_key="8872",
        session_id=session["session_id"],
    )
    with original._connection() as connection:
        connection.executescript(
            """
            ALTER TABLE channel_bindings RENAME TO channel_bindings_current;
            CREATE TABLE channel_bindings (
                instance_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                surface TEXT NOT NULL,
                channel_key TEXT NOT NULL,
                session_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(instance_id, owner_id, agent_id, surface, channel_key),
                FOREIGN KEY(session_id) REFERENCES sessions(session_id)
            );
            INSERT INTO channel_bindings(
                instance_id, owner_id, agent_id, surface, channel_key,
                session_id, updated_at
            )
            SELECT instance_id, owner_id, agent_id, surface, channel_key,
                   session_id, updated_at
            FROM channel_bindings_current;
            DROP TABLE channel_bindings_current;
            UPDATE schema_metadata SET value='27' WHERE key='schema_version';
            """
        )

    upgraded = SessionStore(db_path, instance_id="MSI")
    with upgraded._connection() as connection:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(channel_bindings)"
            ).fetchall()
        }
    assert {"actor_id", "actor_role"} <= columns

    runtime = SimpleNamespace(
        name="hashiko",
        global_config=SimpleNamespace(authorized_id=8872),
        session_store=upgraded,
        _authorized_telegram_ids=(8872,),
        _active_chat_ids={},
    )
    runtime._is_authorized_user = lambda user_id: user_id == 8872
    FlexibleAgentRuntime._record_active_chat(
        runtime,
        SimpleNamespace(
            effective_user=SimpleNamespace(id=8872),
            effective_chat=SimpleNamespace(id=8872),
        ),
    )
    binding = upgraded.latest_channel_binding(
        owner_id="user:8872",
        agent_id="hashiko",
        surface="telegram",
        actor_id=8872,
    )
    assert binding is not None
    assert binding["actor_role"] == "owner"
    assert FlexibleAgentRuntime._primary_chat_id(runtime) == 8872

@pytest.mark.skipif(os.name != "nt", reason="Windows task principal contract")
def test_remote_runtime_sid_comes_from_the_registered_instance_task(tmp_path, monkeypatch):
    from orchestrator import workbench_auth
    from remote import supervisor_identity

    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="SID:S-1-5-19\n", stderr="")

    monkeypatch.setattr(
        supervisor_identity,
        "resolve_supervisor_identity",
        lambda _root: SimpleNamespace(windows_task_name="HashiRemote-MSI"),
    )
    monkeypatch.setattr(workbench_auth.subprocess, "run", fake_run)

    assert workbench_auth._windows_remote_runtime_sids(tmp_path) == ("S-1-5-19",)
    assert captured["env"]["HASHI_REMOTE_PRINCIPAL_QUERY_TASK"] == "HashiRemote-MSI"
    assert "HashiRemote-MSI" not in captured["argv"]


def test_legacy_owner_telegram_endpoint_migrates_without_allowlist_order(tmp_path):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.frontend_connector_registry import endpoint_id_for
    from orchestrator.session_store import SessionStore

    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="MSI")
    owner = "user:8872"
    session = store.ensure_default_session(owner_id=owner, agent_id="hashiko")
    runtime = SimpleNamespace(
        name="hashiko",
        global_config=SimpleNamespace(authorized_id=8872),
        session_store=store,
        _authorized_telegram_ids=(7666, 8872),
        _active_chat_ids={},
    )

    # Multiple authorized identities are an allowlist, not a destination.
    assert (
        store.latest_channel_binding(
            owner_id=owner,
            agent_id="hashiko",
            surface="telegram",
            actor_id=8872,
        )
        is None
    )

    endpoint_id = endpoint_id_for(
        "telegram",
        ingress_transport="photo",
        channel_key="8872",
    )
    store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="hashiko",
        request_id="route-migration",
        text="authorized photo ingress",
        source="photo",
        idempotency_key="route-migration",
        message_context={
            "frontend_ingress": {
                "connector": {
                    "id": "telegram",
                    "endpoint_id": endpoint_id,
                },
            },
            "ingress_transport": "photo",
        },
    )

    restarted = SimpleNamespace(
        name="hashiko",
        global_config=SimpleNamespace(authorized_id=8872),
        session_store=SessionStore(store.db_path, instance_id="MSI"),
        _authorized_telegram_ids=(7666, 8872),
        _active_chat_ids={},
    )
    assert FlexibleAgentRuntime._primary_chat_id(restarted) == 8872
    binding = restarted.session_store.latest_channel_binding(
        owner_id=owner,
        agent_id="hashiko",
        surface="telegram",
    )
    assert binding is not None
    assert binding["channel_key"] == "8872"
    assert binding["actor_id"] == "8872"
    assert binding["actor_role"] == "owner"
