from __future__ import annotations

import pytest

from adapters.codex_run_questions import NativeRunQuestions
from orchestrator.run_questions import QuestionError, RunQuestions, UnansweredQuestion
from tests.test_run_question_connectors import running
from tests.test_run_questions import active, spec


def test_switches_persist_and_close_existing_question_without_an_answer(tmp_path):
    service, session, run, question = running(tmp_path)
    assert service.question_policy("zelda") == {
        "instance_enabled": True, "agent_enabled": True, "enabled": True
    }
    service.set_question_policy(scope="agent", agent_id="zelda", enabled=False)
    current = service.get_answer(
        request_id=run.request_id, agent_id="zelda", question_id=question["question_id"]
    )
    assert current["state"] == "cancelled"
    assert current["resolution_reason"] == "question_cards_disabled"
    assert current["answer"] is None
    with pytest.raises(QuestionError, match="question_cards_disabled"):
        service.create(request_id=run.request_id, agent_id="zelda", payload=spec("blocked"))

    _, other = active(service.store, req="other-request", agent="other")
    service.create(request_id=other.request_id, agent_id="other", payload=spec("other"))
    service.set_question_policy(scope="instance", agent_id="zelda", enabled=False)
    with pytest.raises(QuestionError, match="question_cards_disabled"):
        service.create(request_id=other.request_id, agent_id="other", payload=spec("blocked-global"))
    from orchestrator.session_store import SessionStore
    reopened = RunQuestions(SessionStore(service.store.db_path, instance_id="test"))
    reopened.set_question_policy(scope="instance", agent_id="zelda", enabled=True)
    assert reopened.question_policy("zelda") == {
        "instance_enabled": True, "agent_enabled": False, "enabled": False
    }
    assert reopened.question_policy("other")["enabled"] is True


def test_question_card_contains_authoritative_user_actions_and_continue_is_steering(tmp_path):
    service, session, run, question = running(tmp_path)
    ids = [choice["id"] for choice in question["options"]]
    assert "__hashi_continue__" in ids
    assert "__hashi_stop__" in ids
    with pytest.raises(QuestionError, match="question_option_invalid"):
        service.create(request_id=run.request_id, agent_id="zelda", payload={
            **spec("spoof"), "options": [{"id": "__hashi_stop__", "label": "Spoof"}],
        })
    service.answer(
        session_id=session["session_id"], owner_id="user:7", question_id=question["question_id"],
        payload={"idempotency_key": "continue", "option_id": "__hashi_continue__"},
    )
    result = service.get_answer(
        request_id=run.request_id, agent_id="zelda", question_id=question["question_id"]
    )
    assert result["state"] == "consumed"
    assert result["answer"]["grants_permissions"] is False
    assert "original" in result["answer"]["text"].lower()
    assert "scope" in result["answer"]["text"].lower()
    assert service.store.get_run(run.run_id)["state"] == "running"


@pytest.mark.asyncio
async def test_disabled_native_question_is_rejected_with_plain_chat_instruction(tmp_path):
    service, session, run, initial = running(tmp_path)
    service.set_question_policy(scope="agent", agent_id="zelda", enabled=False)
    native = NativeRunQuestions(service, request_id=run.request_id, agent_id="zelda", interval=.01)
    with pytest.raises(UnansweredQuestion) as exc:
        await native.answer(123, {
            "itemId": "native-1",
            "questions": [{"id": "choice", "question": "Which target?"}],
        })
    assert exc.value.resolution["reason"] == "question_cards_disabled"
    assert "plain" in exc.value.resolution["instruction"].lower()
    assert "Which target?" in str(exc.value.resolution)
    assert not any(q["question"] == "Which target?" for q in service.list(
        session_id=session["session_id"], owner_id="user:7"
    ))


@pytest.mark.asyncio
async def test_command_switches_shared_policy_and_callback_buttons(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from orchestrator.command_registry import runtime_command_map, load_runtime_callbacks

    service, session, run, initial = running(tmp_path)
    runtime = SimpleNamespace(
        name="zelda", session_store=service.store,
        _is_authorized_user=lambda user: user == 7,
        _reply_text=AsyncMock(),
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7), effective_chat=SimpleNamespace(id=7))
    command = runtime_command_map()["askme"].callback
    await command(runtime, update, SimpleNamespace(args=["off"]))
    assert service.question_policy("zelda")["agent_enabled"] is False
    text = runtime._reply_text.await_args.args[1]
    assert "Question cards" in text
    markup = runtime._reply_text.await_args.kwargs["reply_markup"]
    assert any(b.callback_data == "askme:instance:off" for row in markup.inline_keyboard for b in row)
    callback = next(item.callback for item in load_runtime_callbacks() if item.pattern.startswith("^askme:"))
    query = SimpleNamespace(data="askme:instance:off", answer=AsyncMock(), edit_message_text=AsyncMock())
    update.callback_query = query
    await callback(runtime, update, None)
    assert service.question_policy("other")["instance_enabled"] is False
    query.data = "askme:instance:on"
    await callback(runtime, update, None)
    assert service.question_policy("zelda")["agent_enabled"] is False
    assert service.question_policy("other")["enabled"] is True


@pytest.mark.asyncio
async def test_stop_option_does_not_become_a_model_answer_and_dispatches_stop(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from aiohttp.test_utils import TestClient, TestServer
    from orchestrator.workbench_api import WorkbenchApiServer

    cfg = SimpleNamespace(bridge_home=tmp_path, project_root=tmp_path, instance_id="HASHI1",
                          authorized_id=7, workbench_port=18800, api_gateway_port=18801,
                          deployment_profile="personal", ui_language="zh-CN")
    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps({"global": {}, "agents": [{"name": "lily"}]}))
    server = WorkbenchApiServer(config_path, cfg, secrets={"workbench_admin_token": "question-test-secret"},
                                reconcile_session_runs=False)
    session, run = active(server.session_store)
    service = server._questions()
    question = service.create(request_id=run.request_id, agent_id="lily", payload=spec())
    calls = []

    class Worker:
        is_function_worker_proxy = True
        async def execute_slash_command(self, line, *, source_channel, chat_id, session_metadata):
            calls.append((line, session_metadata))
            pending = service.get_answer(request_id=run.request_id, agent_id="lily",
                                         question_id=question["question_id"])
            assert pending["state"] == "stopping" and pending["answer"] is None
            server.session_store.cancel_run(run.run_id, owner_id="user:7", reason="user_stop")
            return {"ok": True, "result": {"ok": True}}

    server._runtime_map = lambda: {"lily": Worker()}
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        url = f"/api/v1/sessions/{session['session_id']}/questions/{question['question_id']}/answer"
        response = await client.post(url, headers={"X-Workbench-Token": "question-test-secret"},
                                     json={"idempotency_key": "stop", "option_id": "__hashi_stop__"})
        assert response.status == 200, await response.text()
        assert calls[0][0] == "/stop"
        assert calls[0][1]["_hashi_question_stop_id"] == question["question_id"]
        assert server.session_store.get_run(run.run_id)["state"] == "stopped"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_continue_resolves_a_native_batch_without_waiting_for_other_questions(tmp_path):
    import asyncio

    service, session, run, initial = running(tmp_path)
    native = NativeRunQuestions(service, request_id=run.request_id, agent_id="zelda", interval=.01)
    task = asyncio.create_task(native.answer(77, {"itemId": "batch", "questions": [
        {"id": "a", "question": "First?"}, {"id": "b", "question": "Second?"}
    ]}))
    try:
        for _ in range(100):
            questions = service.list(session_id=session["session_id"], owner_id="user:7")
            first = next((item for item in questions if item["question"] == "First?"), None)
            second = next((item for item in questions if item["question"] == "Second?"), None)
            if first and second:
                break
            await asyncio.sleep(.01)
        service.answer(session_id=session["session_id"], owner_id="user:7", question_id=first["question_id"],
                       payload={"idempotency_key": "continue", "option_id": "__hashi_continue__"})
        with pytest.raises(UnansweredQuestion) as exc:
            await asyncio.wait_for(task, 2)
        assert exc.value.resolution["reason"] == "user_continue"
        assert "original task" in exc.value.resolution["instruction"]
        second = next(item for item in service.list(session_id=session["session_id"], owner_id="user:7")
                      if item["question"] == "Second?")
        assert second["state"] == "cancelled" and second["answer"] is None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_eight_model_options_keep_both_system_actions_in_telegram(tmp_path):
    from orchestrator.frontend_run_questions import question_blocks, telegram_button
    from orchestrator.runtime_run_questions import CALLBACK_PATTERN
    import re

    service, session, run, initial = running(tmp_path)
    question = service.create(request_id=run.request_id, agent_id="zelda", payload={
        "idempotency_key": "many", "question": "Choose",
        "options": [{"id": f"choice_{i}", "label": str(i)} for i in range(8)],
    })
    blocks = [block for block in question_blocks(question) if block["type"] == "action"]
    buttons = [telegram_button(block, "zh-CN") for block in blocks]
    assert any(button.text == "按原任务继续" for button in buttons)
    assert any(button.text == "🛑 立即停止" for button in buttons)
    assert all(re.fullmatch(CALLBACK_PATTERN, button.callback_data) for button in buttons)


@pytest.mark.asyncio
async def test_workbench_command_menu_toggles_actual_shared_policy(tmp_path):
    from types import SimpleNamespace
    from orchestrator.command_interaction_bridge import dispatch_command_interaction

    service, session, run, initial = running(tmp_path)

    async def reply(update, text, **kwargs):
        await update.message.reply_text(text, **kwargs)

    runtime = SimpleNamespace(
        name="zelda", session_store=service.store,
        global_config=SimpleNamespace(authorized_id=7, instance_id="test",
                                      bridge_home=tmp_path, project_root=tmp_path,
                                      deployment_profile="personal"),
        _is_authorized_user=lambda user: user == 7,
        _is_command_allowed=lambda name: name == "askme",
        _reply_text=reply,
    )
    metadata = {"actor_id": 7, "instance_id": "test", "session_id": session["session_id"],
                "context_generation": 1, "session_surface": "workbench",
                "session_channel_key": "default", "connection_binding": "bindingabcdefghijk",
                "owner_id": "user:7"}
    operation = {"version": 1, "op": "open", "command": "/askme", "ui_locale": "zh-CN",
                 "client_id": "clientabcdefghijkl", "request_id": "requestabcdefghijk"}
    opened = await dispatch_command_interaction(runtime, operation, metadata)
    assert opened["ok"], opened
    card = opened["messages"][-1]
    assert "提问卡片" in card["text"]
    menu = runtime._command_interaction_store.menus[card["command_ui"]["menu_id"]]
    button = next(key for key, (data, _) in menu.actions.items() if data == "askme:instance:off")
    action = {**operation, "op": "act", "command": "", "request_id": "actionabcdefghijkl",
              "menu_id": menu.id, "revision": menu.revision, "button_id": button}
    result = await dispatch_command_interaction(runtime, action, metadata)
    assert result["ok"], result
    assert not service.question_policy("other")["enabled"]
    assert service.question_policy("zelda")["agent_enabled"]


@pytest.mark.asyncio
async def test_disabled_tool_returns_explicit_plain_question_instruction(tmp_path):
    import json
    from types import SimpleNamespace
    from aiohttp.test_utils import TestClient, TestServer
    from orchestrator.workbench_api import WorkbenchApiServer
    from tools.registry import ToolRegistry

    cfg = SimpleNamespace(bridge_home=tmp_path, project_root=tmp_path, instance_id="HASHI1",
                          authorized_id=7, workbench_port=18800, api_gateway_port=18801,
                          deployment_profile="personal")
    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps({"global": {}, "agents": [{"name": "lily"}]}))
    server = WorkbenchApiServer(config_path, cfg, secrets={"workbench_admin_token": "question-test-secret"},
                                reconcile_session_runs=False)
    session, run = active(server.session_store)
    server._questions().set_question_policy(scope="agent", agent_id="lily", enabled=False)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        registry = ToolRegistry(allowed_tools=["ask_user"], workspace_dir=tmp_path, access_root=tmp_path,
            secrets={"workbench_admin_token": "question-test-secret"}, audit_context={
                "agent_name": "lily", "request_id": run.request_id, "instance_id": "HASHI1",
                "global_config": cfg, "workbench_api_base_url": str(client.make_url("")).rstrip("/"),
            })
        result = await registry.execute("ask_user", spec(), "call-disabled")
        assert "question_cards_disabled" in result.output
        assert "final plain text" in result.output
        assert "stop this turn" in result.output.lower()
        assert server._questions().list(session_id=session["session_id"], owner_id="user:7") == []
        assert server.session_store.get_run(run.run_id)["state"] == "running"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_telegram_stop_option_uses_real_stop_entrypoint_and_scoped_claim(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from orchestrator.frontend_run_questions import question_blocks, telegram_button
    from orchestrator.runtime_run_questions import handle_callback
    from tests.test_frontend_attachment_tool import _ToolRuntime

    service, session, run, question = running(tmp_path)
    runtime = _ToolRuntime(tmp_path, service.store)
    runtime.name = "zelda"
    runtime._is_authorized_user = lambda user: user == 7
    runtime._telegram_channel_allowed = AsyncMock(return_value=True)
    stop_block = next(block for block in question_blocks(question)
                      if (block.get("payload") or {}).get("option_id") == "__hashi_stop__")
    button = telegram_button(stop_block, "zh-CN")
    message = SimpleNamespace(message_id=123)
    query = SimpleNamespace(data=button.callback_data, id="stop-cb", message=message,
                            answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7),
                             effective_chat=SimpleNamespace(id=7), effective_message=message)
    calls = []

    async def stop(actual_runtime, actual_update, context):
        calls.append((actual_runtime, actual_update, context))
        claim = service.stop_claim(question_id=context._hashi_question_stop_id, agent_id="zelda")
        assert claim["request_id"] == run.request_id
        return {"ok": True}

    monkeypatch.setattr("orchestrator.runtime_control.cmd_stop", stop)
    await handle_callback(runtime, update, None)
    assert calls and calls[0][1].message is message
    assert calls[0][2]._hashi_question_stop_id == question["question_id"]
    query.answer.assert_awaited()


@pytest.mark.asyncio
async def test_session_question_list_has_localized_system_options(tmp_path):
    import json
    from types import SimpleNamespace
    from aiohttp.test_utils import TestClient, TestServer
    from orchestrator.workbench_api import WorkbenchApiServer

    cfg = SimpleNamespace(bridge_home=tmp_path, project_root=tmp_path, instance_id="HASHI1",
                          authorized_id=7, workbench_port=18800, api_gateway_port=18801,
                          deployment_profile="personal", ui_language="zh-CN")
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"global": {}, "agents": [{"name": "lily"}]}))
    server = WorkbenchApiServer(path, cfg, secrets={"workbench_admin_token": "question-test-secret"},
                                reconcile_session_runs=False)
    session, run = active(server.session_store)
    server._questions().create(request_id=run.request_id, agent_id="lily", payload=spec())
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        response = await client.get(f"/api/v1/sessions/{session['session_id']}/questions",
                                    headers={"X-Workbench-Token": "question-test-secret"})
        assert response.status == 200
        question = (await response.json())["questions"][0]
        labels = {item["id"]: item["label"] for item in question["options"]}
        assert labels["blue"] == "Blue"
        assert labels["__hashi_continue__"] == "按原任务继续"
        assert labels["__hashi_stop__"] == "🛑 立即停止"
    finally:
        await client.close()


def test_herv3_receives_disabled_policy_as_runtime_instruction():
    from adapters.openrouter_api import OpenRouterAdapter
    resolution = {"type": "hashi.run_question.resolution", "questions": [{
        "question_id": "que-test", "state": "cancelled", "answer": None,
        "resolution_reason": "question_cards_disabled", "question": "Which target?",
    }]}
    messages = OpenRouterAdapter._run_question_resolution_messages(resolution)
    assert messages[0]["role"] == "system"
    assert "final plain text" in messages[0]["content"]
    assert "Which target?" not in messages[0]["content"]
    assert "Which target?" in messages[1]["content"]


def test_legacy_option_id_collision_cannot_turn_an_ordinary_answer_into_stop(tmp_path):
    import json

    service, session, run, first = running(tmp_path)

    def make_legacy(question):
        with service.store._connection() as connection:
            row = connection.execute("SELECT question_json FROM run_questions WHERE question_id=?",
                                     (question["question_id"],)).fetchone()
            data = json.loads(row["question_json"])
            data["options"] = [{"id": "__hashi_stop__", "label": "Legacy choice", "description": ""}]
            connection.execute("UPDATE run_questions SET question_json=? WHERE question_id=?",
                               (json.dumps(data), question["question_id"]))
        return next(q for q in service.list(session_id=session["session_id"], owner_id="user:7")
                    if q["question_id"] == question["question_id"])

    old = make_legacy(first)
    ids = [option["id"] for option in old["options"]]
    assert len(ids) == len(set(ids))
    service.answer(session_id=session["session_id"], owner_id="user:7", question_id=old["question_id"],
                   payload={"idempotency_key": "legacy", "option_id": "__hashi_stop__"})
    answer = service.get_answer(request_id=run.request_id, agent_id="zelda",
                                question_id=old["question_id"])["answer"]
    assert answer.get("system_action") is None

    new = service.create(request_id=run.request_id, agent_id="zelda", payload=spec("another"))
    old = make_legacy(new)
    stop_id = next(option["id"] for option in old["options"] if option.get("system_action") == "stop")
    assert stop_id != "__hashi_stop__"
    saved = service.answer(session_id=session["session_id"], owner_id="user:7",
                           question_id=old["question_id"],
                           payload={"idempotency_key": "stop", "option_id": stop_id})
    assert saved["answer"]["system_action"] == "stop"


@pytest.mark.asyncio
async def test_disabled_native_rpc_returns_plain_question_without_creating_a_card(tmp_path):
    import json
    from types import SimpleNamespace
    from adapters.codex_interactive import InteractiveTurn
    from tests.test_codex_run_questions import RPCProcess

    service, session, run, initial = running(tmp_path)
    service.set_question_policy(scope="agent", agent_id="zelda", enabled=False)
    adapter = SimpleNamespace(config=SimpleNamespace(name="zelda", model="fixture-model"),
                              _session_mode=True, _session_id="thread-1",
                              effective_workdir=tmp_path, _touch_activity=lambda: None)
    bridge = InteractiveTurn(adapter, service, run.request_id, "test", [])

    class PlainQuestionProc(RPCProcess):
        def write(self, data):
            message = json.loads(data)
            if message.get("id") == 42:
                self.sent.append(message)
                self.emit({"method": "item/completed", "params": {"item": {
                    "type": "agentMessage", "id": "final", "text": "Which target?"
                }}})
                self.emit({"method": "turn/completed", "params": {"turn": {
                    "id": "turn-1", "status": "completed"
                }}})
            else:
                super().write(data)

    proc = PlainQuestionProc()
    events = [json.loads(line) async for line in bridge.lines(proc, "medium")]
    reply = next(message for message in proc.sent if message.get("id") == 42)
    assert reply["error"]["data"]["reason"] == "question_cards_disabled"
    assert "final plain text" in reply["error"]["data"]["instruction"]
    assert "Continue with blue?" in str(reply["error"]["data"]["questions"])
    assert not any(item["question"] == "Continue with blue?" for item in service.list(
        session_id=session["session_id"], owner_id="user:7"
    ))
    start = next(message for message in proc.sent if message.get("method") == "thread/resume")
    assert "final plain text" in start["params"]["developerInstructions"]
    assert any(event.get("item", {}).get("text") == "Which target?" for event in events)
    assert events[-1]["type"] == "turn.completed"
