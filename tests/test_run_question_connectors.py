from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.frontend_delivery import freeze_run_delivery_route
from orchestrator.frontend_projection import project_frontend_event
from orchestrator.run_questions import QuestionError, RunQuestions
from orchestrator.session_store import SessionStore


def running(tmp_path):
    store = SessionStore(tmp_path / 'sessions.sqlite3', instance_id='test')
    service = RunQuestions(store)
    session = store.ensure_default_session(owner_id='user:7', agent_id='zelda')
    route = freeze_run_delivery_route(message_source_id='workbench', session_surface='workbench',
        session_channel_key='test', chat_id='7', telegram_requested=True)
    run = store.accept_run(session_id=session['session_id'], owner_id='user:7', agent_id='zelda',
        request_id='req-questions', text='test', source='workbench', idempotency_key='q-test', delivery_route=route)
    store.mark_request_running(run.request_id, worker_id='test')
    question = service.create(request_id=run.request_id, agent_id='zelda', payload={
        'question': 'Which colour?', 'options': [{'id': 'blue', 'label': 'Blue', 'description': 'Cool'}],
        'idempotency_key': 'q', 'allow_free_text': True})
    return service, session, run, question


def test_creation_is_durable_deliverable_and_has_answer_actions(tmp_path):
    service, session, run, q = running(tmp_path)
    pending = service.pending_telegram_deliveries(agent_id='zelda')
    assert len(pending) == 1
    event = next(e for e in service.store.events(session['session_id'], owner_id='user:7')
                 if e['kind'] == 'run.question.created')
    projected = project_frontend_event(event)
    assert projected['semantic_kind'] != 'approval'
    assert 'Which colour?' in str(projected['content_blocks'])
    actions = [b for b in projected['content_blocks'] if b['type'] == 'action']
    assert actions[0]['payload']['question_id'] == q['question_id']
    assert actions[0]['payload']['option_id'] == 'blue'
    assert service.pending_telegram_deliveries(agent_id='other') == []


def test_telegram_answer_scope_repeat_and_late_answer(tmp_path):
    service, session, run, q = running(tmp_path)
    for owner, agent, chat in [('user:8', 'zelda', '7'), ('user:7', 'other', '7'), ('user:7', 'zelda', '8')]:
        with pytest.raises((QuestionError, LookupError)):
            service.telegram_question(question_id=q['question_id'], owner_id=owner, agent_id=agent, chat_id=chat)
    got = service.telegram_question(question_id=q['question_id'], owner_id='user:7', agent_id='zelda', chat_id='7')
    assert got['session_id'] == session['session_id']
    payload = {'idempotency_key': 'telegram:7:12', 'option_id': 'blue'}
    service.answer(session_id=session['session_id'], owner_id='user:7', question_id=q['question_id'], payload=payload)
    assert service.answer(session_id=session['session_id'], owner_id='user:7', question_id=q['question_id'], payload=payload)['state'] == 'answered'
    with pytest.raises(QuestionError, match='already_answered'):
        service.answer(session_id=session['session_id'], owner_id='user:7', question_id=q['question_id'], payload={**payload, 'text': 'changed'})
    assert service.get_answer(request_id=run.request_id, agent_id='zelda', question_id=q['question_id'])['answer']['grants_permissions'] is False
    assert service.store.get_run(run.run_id)['state'] == 'running'


def test_native_question_cancel_does_not_end_run(tmp_path):
    service, session, run, q = running(tmp_path)
    service.cancel(request_id=run.request_id, agent_id='zelda', question_id=q['question_id'])
    assert service.pending_telegram_deliveries(agent_id='zelda') == []
    assert service.get_answer(request_id=run.request_id, agent_id='zelda', question_id=q['question_id'])['state'] == 'cancelled'
    assert service.store.get_run(run.run_id)['state'] == 'running'


@pytest.mark.asyncio
async def test_telegram_fc_delivery_buttons_and_authenticated_callback(tmp_path, monkeypatch):
    from tests.test_frontend_attachment_tool import _ToolRuntime
    from orchestrator import telegram_delivery_failover
    from orchestrator.frontend_incremental_delivery import dispatch_pending_telegram_deliverables
    from orchestrator.runtime_run_questions import handle_callback, handle_text_reply
    service, session, run, q = running(tmp_path)
    runtime = _ToolRuntime(tmp_path, service.store)
    runtime.name = 'zelda'
    runtime.global_config.ui_language = 'zh-CN'
    runtime.app.bot.id = 123
    runtime._is_authorized_user = lambda uid: uid == 7
    runtime._telegram_channel_allowed = AsyncMock(return_value=True)
    runtime._reply_text = AsyncMock()
    monkeypatch.setattr(telegram_delivery_failover, 'handle_blocked_send', AsyncMock(return_value=False))
    assert await dispatch_pending_telegram_deliverables(runtime) == 1
    assert await dispatch_pending_telegram_deliverables(runtime) == 0
    message = runtime.app.bot.messages[0]
    assert 'Which colour?' in message['text']
    markup = message['reply_markup']
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert buttons[0].text == 'Blue' and buttons[-1].text == '自行填写'
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
    query = SimpleNamespace(data=buttons[0].callback_data, id='cb-1', answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=8), effective_chat=SimpleNamespace(id=7))
    await handle_callback(runtime, update, None)
    assert service.get_answer(request_id=run.request_id, agent_id='zelda', question_id=q['question_id'])['state'] == 'pending'
    update.effective_user.id = 7
    await handle_callback(runtime, update, None)
    await handle_callback(runtime, update, None)
    answer = service.get_answer(request_id=run.request_id, agent_id='zelda', question_id=q['question_id'])
    assert answer['answer']['option_id'] == 'blue' and answer['state'] == 'consumed'
    # A late reply is handled as a closed question, not as a new user task.
    original = SimpleNamespace(from_user=SimpleNamespace(id=123), reply_markup=markup)
    update.message = SimpleNamespace(reply_to_message=original, text='late answer', message_id=101)
    assert await handle_text_reply(runtime, update) is True
    with service.store._connection() as c:
        assert c.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_free_text_reply_uses_question_scope_and_preserves_unicode(tmp_path):
    from tests.test_frontend_attachment_tool import _ToolRuntime
    from orchestrator.frontend_run_questions import question_blocks, telegram_button
    from orchestrator.runtime_run_questions import handle_text_reply
    service, session, run, q = running(tmp_path)
    runtime = _ToolRuntime(tmp_path, service.store)
    runtime.name = 'zelda'
    runtime.app.bot.id = 123
    runtime._reply_text = AsyncMock()
    button = telegram_button(question_blocks(q)[-1], 'zh-CN')
    original = SimpleNamespace(from_user=SimpleNamespace(id=123), reply_markup=SimpleNamespace(inline_keyboard=[[button]]))
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=7), effective_user=SimpleNamespace(id=7),
        message=SimpleNamespace(reply_to_message=original, text='蓝色，保留中文', message_id=102))
    assert await handle_text_reply(runtime, update) is True
    assert await handle_text_reply(runtime, update) is True
    answer = service.get_answer(request_id=run.request_id, agent_id='zelda', question_id=q['question_id'])
    assert answer['answer']['text'] == '蓝色，保留中文'
    original.from_user.id = 999
    assert await handle_text_reply(runtime, update) is False
