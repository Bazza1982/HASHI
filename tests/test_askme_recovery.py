from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.frontend_run_questions import QuestionRenditions, refresh_telegram_question_renditions
from orchestrator.workbench_api import WorkbenchApiServer
from tests.test_frontend_attachment_tool import _ToolRuntime
from tests.test_run_question_connectors import running
from tests.test_run_questions import active, spec


@pytest.mark.asyncio
async def test_existing_telegram_card_gets_system_actions_once(tmp_path):
    service, session, run, question = running(tmp_path)
    runtime = _ToolRuntime(tmp_path, service.store)
    runtime.name = 'zelda'
    runtime.global_config.ui_language = 'zh-CN'
    runtime.app.bot.edit_message_text = AsyncMock()
    renditions = QuestionRenditions(service.store)
    renditions.record(question_id=question['question_id'], endpoint_id='telegram:legacy',
                      chat_id='7', transport_message_id='123', owner_id='user:7',
                      agent_id='zelda', request_id=run.request_id)
    with service.store._connection() as connection:
        connection.execute("UPDATE frontend_question_renditions SET presentation_state='pending'")
    await refresh_telegram_question_renditions(runtime, service.store)
    sent = runtime.app.bot.edit_message_text.await_args.kwargs
    assert 'Which colour?' in sent['text'] and 'Cool' in sent['text']
    labels = [button.text for row in sent['reply_markup'].inline_keyboard for button in row]
    assert '按原任务继续' in labels and '🛑 立即停止' in labels
    await refresh_telegram_question_renditions(runtime, service.store)
    assert runtime.app.bot.edit_message_text.await_count == 1


@pytest.mark.asyncio
async def test_telegram_unconfirmed_stop_can_be_retried_without_answering_the_model(tmp_path, monkeypatch):
    from orchestrator.frontend_run_questions import question_blocks, telegram_button
    from orchestrator.runtime_run_questions import handle_callback

    service, session, run, question = running(tmp_path)
    runtime = _ToolRuntime(tmp_path, service.store)
    runtime.name = 'zelda'
    runtime.global_config.ui_language = 'zh-CN'
    runtime._is_authorized_user = lambda user: user == 7
    runtime._telegram_channel_allowed = AsyncMock(return_value=True)
    runtime.app.bot.edit_message_text = AsyncMock()
    QuestionRenditions(service.store).record(
        question_id=question['question_id'], endpoint_id='telegram:test', chat_id='7',
        transport_message_id='123', owner_id='user:7', agent_id='zelda', request_id=run.request_id)
    stop_block = next(block for block in question_blocks(question) if
                      (block.get('payload') or {}).get('system_action') == 'stop')
    button = telegram_button(stop_block, 'zh-CN')
    message = SimpleNamespace(message_id=123)
    query = SimpleNamespace(data=button.callback_data, id='cb-1', message=message,
                            answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7),
                             effective_chat=SimpleNamespace(id=7), effective_message=message)
    calls = []

    async def stop(_runtime, _update, context):
        calls.append(service.stop_claim(question_id=context._hashi_question_stop_id, agent_id='zelda'))
        assert service.get_answer(request_id=run.request_id, agent_id='zelda',
                                  question_id=question['question_id'])['answer'] is None
        if len(calls) == 1:
            return {'ok': False}
        service.store.cancel_run(run.run_id, owner_id='user:7', reason='user_stop')
        return {'ok': True}

    monkeypatch.setattr('orchestrator.runtime_control.cmd_stop', stop)
    await handle_callback(runtime, update, None)
    await refresh_telegram_question_renditions(runtime, service.store)
    sent = runtime.app.bot.edit_message_text.await_args.kwargs
    assert '未确认' in sent['text']
    assert [b.text for row in sent['reply_markup'].inline_keyboard for b in row] == ['🛑 立即停止']
    query.id = 'cb-2'
    await handle_callback(runtime, update, None)
    assert len(calls) == 2
    assert service.store.get_run(run.run_id)['state'] == 'stopped'


@pytest.mark.asyncio
async def test_unconfirmed_stop_remains_retryable_on_workbench(tmp_path):
    cfg = SimpleNamespace(bridge_home=tmp_path, project_root=tmp_path, instance_id='HASHI1',
                          authorized_id=7, workbench_port=18800, api_gateway_port=18801,
                          deployment_profile='personal', ui_language='zh-CN')
    path = tmp_path / 'agents.json'
    path.write_text(json.dumps({'global': {}, 'agents': [{'name': 'lily'}]}))
    server = WorkbenchApiServer(path, cfg, secrets={'workbench_admin_token': 'secret'},
                                reconcile_session_runs=False)
    session, run = active(server.session_store)
    question = server._questions().create(request_id=run.request_id, agent_id='lily', payload=spec())
    calls = []

    class Worker:
        is_function_worker_proxy = True

        async def execute_slash_command(self, line, *, source_channel, chat_id, session_metadata):
            calls.append(line)
            if len(calls) == 1:
                return {'ok': False}
            server._questions().stop_claim(question_id=question['question_id'], agent_id='lily')
            server.session_store.cancel_run(run.run_id, owner_id='user:7', reason='user_stop')
            return {'ok': True, 'result': {'ok': True}}

    server._runtime_map = lambda: {'lily': Worker()}
    client = TestClient(TestServer(server.app))
    await client.start_server()
    headers = {'X-Workbench-Token': 'secret'}
    try:
        base = f"/api/v1/sessions/{session['session_id']}/questions"
        url = f"{base}/{question['question_id']}/answer"
        first = await client.post(url, headers=headers, json={
            'idempotency_key': 'first', 'option_id': '__hashi_stop__'})
        assert first.status == 503
        result = await (await client.get(base, headers=headers)).json()
        shown = result['questions'][0]
        assert shown['state'] == 'pending'
        assert '未确认' in shown['question']
        assert [option['system_action'] for option in shown['options']] == ['stop']
        assert not shown['allow_free_text']
        retried = await client.post(url, headers=headers, json={
            'idempotency_key': 'second', 'option_id': '__hashi_stop__'})
        assert retried.status == 200, await retried.text()
        assert server.session_store.get_run(run.run_id)['state'] == 'stopped'
        assert calls == ['/stop', '/stop']
    finally:
        await client.close()
