import asyncio
import json
from types import SimpleNamespace

import pytest

from adapters.codex_interactive import InteractiveTurn, app_server_command
from adapters.codex_run_questions import NativeRunQuestions
from tests.test_run_question_connectors import running


@pytest.mark.asyncio
async def test_native_batch_keeps_original_ids_and_consumes_only_explicit_answers(tmp_path):
    service, session, run, initial = running(tmp_path)
    native = NativeRunQuestions(service, request_id=run.request_id, agent_id='zelda', interval=.01)
    task = asyncio.create_task(native.answer(14, {'itemId': 'item-42', 'questions': [
        {'id': 'colour', 'question': 'Colour?', 'options': [{'label': 'Blue', 'description': 'Cool'}]},
        {'id': 'word', 'question': 'Word?', 'options': None}]}))
    await asyncio.sleep(.02)
    qs = service.list(session_id=session['session_id'], owner_id='user:7')
    assert not task.done()
    colour = next(q for q in qs if q['question'] == 'Colour?')
    word = next(q for q in qs if q['question'] == 'Word?')
    for q, answer in [(word, {'text': '你好'}), (colour, {'option_id': 'option_0'})]:
        service.answer(session_id=session['session_id'], owner_id='user:7', question_id=q['question_id'],
                       payload={'idempotency_key': q['question_id'], **answer})
    assert await task == {'answers': {'word': {'answers': ['你好']}, 'colour': {'answers': ['Blue']}}}
    assert service.store.get_run(run.run_id)['state'] == 'running'


@pytest.mark.asyncio
async def test_native_cancel_and_secret_never_become_answers(tmp_path):
    service, session, run, initial = running(tmp_path)
    native = NativeRunQuestions(service, request_id=run.request_id, agent_id='zelda', interval=.01)
    with pytest.raises(ValueError, match='secret'):
        await native.answer(1, {'itemId': 'secret', 'questions': [{'id': 's', 'isSecret': True}]})
    task = asyncio.create_task(native.answer(2, {'itemId': 'cancel', 'questions': [{'id': 'x', 'question': 'Wait?'}]}))
    await asyncio.sleep(.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = service.list(session_id=session['session_id'], owner_id='user:7')
    assert next(q for q in rows if q['question'] == 'Wait?')['state'] == 'cancelled'


def test_interactive_transport_retains_gateway_inventory_and_disables_native_shell():
    cmd = app_server_command(['codex', 'exec', 'resume', 'thread', '--json', '-c', 'mcp_servers.old.enabled=false',
        '-c', 'mcp_servers.hashi={command="py",required=true}', '--enable', 'hooks',
        '--ignore-user-config', '--dangerously-bypass-hook-trust', '-c', 'hooks.PreToolUse=[]', '--', 'prompt'])
    assert cmd[:3] == ['codex', 'app-server', '--stdio']
    assert 'mcp_servers.old.enabled=false' in cmd and 'mcp_servers.hashi={command="py",required=true}' in cmd
    assert '--ignore-user-config' not in cmd and 'hooks.PreToolUse=[]' not in cmd
    assert cmd[cmd.index('shell_tool') - 1] == '--disable'
    assert cmd[cmd.index('hooks') - 1] == '--disable'


class RPCProcess:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stdin = self
        self.sent = []

    def emit(self, payload):
        self.stdout.feed_data((json.dumps(payload) + '\n').encode())

    def write(self, data):
        msg = json.loads(data)
        self.sent.append(msg)
        method = msg.get('method')
        if method == 'initialize':
            self.emit({'id': msg['id'], 'result': {}})
        elif method in ('thread/start', 'thread/resume'):
            self.emit({'id': msg['id'], 'result': {'thread': {'id': 'thread-1'}}})
        elif method == 'turn/start':
            self.emit({'id': msg['id'], 'result': {'turn': {'id': 'turn-1'}}})
            self.emit({'method': 'turn/started', 'params': {'turn': {'id': 'turn-1'}}})
            self.emit({'id': 42, 'method': 'item/tool/requestUserInput', 'params': {
                'threadId': 'thread-1', 'turnId': 'turn-1', 'itemId': 'item-1', 'isBlocking': True,
                'questions': [{'id': 'choice', 'question': 'Continue with blue?',
                    'options': [{'label': 'Blue', 'description': 'Recommended'}]}]}})
        elif msg.get('id') == 42:
            self.emit({'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'id': 'final', 'text': 'Used Blue'}}})
            self.emit({'method': 'thread/tokenUsage/updated', 'params': {'tokenUsage': {'total': {'inputTokens': 10, 'outputTokens': 4}}}})
            self.emit({'method': 'turn/completed', 'params': {'turn': {'id': 'turn-1', 'status': 'completed'}}})

    async def drain(self):
        pass

    def close(self):
        self.stdout.feed_eof()


@pytest.mark.asyncio
async def test_rpc_roundtrip_same_turn_then_final_and_usage(tmp_path):
    service, session, run, initial = running(tmp_path)
    adapter = SimpleNamespace(config=SimpleNamespace(name='zelda', model='fixture-model'),
        _session_mode=True, _session_id='thread-1', effective_workdir=tmp_path, _touch_activity=lambda: None)
    bridge = InteractiveTurn(adapter, service, run.request_id, 'test', [])
    bridge.questions.interval = .01
    proc = RPCProcess()
    async def collect():
        return [json.loads(line) async for line in bridge.lines(proc, 'medium')]
    task = asyncio.create_task(collect())
    for _ in range(100):
        qs = service.list(session_id=session['session_id'], owner_id='user:7')
        q = next((q for q in qs if q['question'] == 'Continue with blue?'), None)
        if q:
            break
        await asyncio.sleep(.01)
    assert q is not None and not task.done()
    service.answer(session_id=session['session_id'], owner_id='user:7', question_id=q['question_id'],
                   payload={'idempotency_key': 'http-submit', 'option_id': 'option_0'})
    events = await asyncio.wait_for(task, 2)
    reply = next(m for m in proc.sent if m.get('id') == 42)
    assert reply == {'id': 42, 'result': {'answers': {'choice': {'answers': ['Blue']}}}}
    assert len([m for m in proc.sent if m.get('method') == 'turn/start']) == 1
    assert events[-1]['type'] == 'turn.completed' and events[-1]['usage']['input_tokens'] == 10
    assert any(e.get('item', {}).get('text') == 'Used Blue' for e in events)
