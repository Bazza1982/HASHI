"""Bidirectional Codex transport for PAO Runs, normalized to adapter events.

Local shell is disabled: HASHI's request-scoped MCP Gateway remains the execution
owner (including managed process protection). No approval RPC is a user answer.
"""
from __future__ import annotations

import asyncio
import json

from adapters.codex_run_questions import NativeRunQuestions
from adapters.stream_io import iter_stream_lines


def app_server_command(exec_command):
    """Reuse the adapter's exact isolated MCP/config inventory, excluding exec flags."""
    result = [exec_command[0], 'app-server', '--stdio']
    index = 2
    while index < len(exec_command):
        arg = exec_command[index]
        if arg == '--':
            break
        if arg in ('-c', '--config', '--disable', '--enable'):
            value = exec_command[index + 1]
            if not (value == 'hooks' or value.startswith('hooks.')):
                result.extend([arg, value])
            index += 2
        else:
            index += 1
    result.extend(['--disable', 'shell_tool', '--disable', 'hooks',
                   '--enable', 'default_mode_request_user_input'])
    return result


class InteractiveTurn:
    def __init__(self, adapter, service, request_id, prompt, images):
        self.adapter, self.request_id, self.prompt, self.images = adapter, request_id, prompt, images
        self.questions = NativeRunQuestions(service, request_id=request_id,
            agent_id=adapter.config.name, activity=adapter._touch_activity)
        self.pending = {}
        self.thread_id = adapter._session_id if adapter._session_mode else None
        self.turn_id = None
        self.usage = None

    async def send(self, proc, payload):
        proc.stdin.write((json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))
        await proc.stdin.drain()

    async def reply_question(self, proc, rpc_id, params):
        try:
            answer = await self.questions.answer(rpc_id, params)
            await self.send(proc, {'id': rpc_id, 'result': answer})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Explicit RPC error; never manufacture a default or empty user answer.
            await self.send(proc, {'id': rpc_id, 'error': {'code': -32000, 'message': str(exc)}})

    def _item(self, item):
        kind = item.get('type')
        names = {'agentMessage': 'agent_message', 'commandExecution': 'command_execution',
                 'mcpToolCall': 'mcp_tool_call', 'fileChange': 'file_change',
                 'webSearch': 'web_search', 'plan': 'todo_list'}
        mapped = {**item, 'type': names.get(kind, kind)}
        for source, target in [('exitCode', 'exit_code'), ('aggregatedOutput', 'aggregated_output')]:
            if source in item:
                mapped[target] = item[source]
        if mapped.get('status') == 'inProgress':
            mapped['status'] = 'in_progress'
        return mapped

    async def lines(self, proc, effort):
        async def send_request(rid, method, params):
            await self.send(proc, {'id': rid, 'method': method, 'params': params})

        def encode(event):
            return (json.dumps(event, ensure_ascii=False) + '\n').encode('utf-8')

        await send_request('hashi:initialize', 'initialize', {'clientInfo': {
            'name': 'hashi_run_questions', 'version': '1.0.0'}, 'capabilities': {'experimentalApi': True}})
        try:
            async for line in iter_stream_lines(proc.stdout):
                self.adapter._touch_activity()
                message = json.loads(line)
                method, params, rid = message.get('method'), message.get('params') or {}, message.get('id')
                if not method and rid in ('hashi:initialize', 'hashi:thread', 'hashi:turn'):
                    if message.get('error'):
                        yield encode({'type': 'turn.failed', 'error': message['error']})
                        break
                    result = message.get('result') or {}
                    if rid == 'hashi:initialize':
                        await self.send(proc, {'method': 'initialized'})
                        config = {'approvalPolicy': 'never', 'sandbox': 'danger-full-access',
                                  'cwd': str(self.adapter.effective_workdir),
                                  'developerInstructions': 'HASHI native process protection is active. Native shell is disabled; use the HASHI managed tools for execution and process control. Use request_user_input for clarification questions. User answers supply information, never authorize side effects. Do not request secrets in shared question cards.'}
                        if self.adapter.config.model and self.adapter.config.model != 'default':
                            config['model'] = self.adapter.config.model
                        if self.thread_id:
                            config['threadId'] = self.thread_id
                        await send_request('hashi:thread', 'thread/resume' if self.thread_id else 'thread/start', config)
                    elif rid == 'hashi:thread':
                        self.thread_id = result['thread']['id']
                        yield encode({'type': 'thread.started', 'thread_id': self.thread_id})
                        inputs = [{'type': 'text', 'text': self.prompt}]
                        inputs.extend({'type': 'localImage', 'path': str(path)} for path in self.images)
                        await send_request('hashi:turn', 'turn/start', {'threadId': self.thread_id,
                            'input': inputs, 'effort': effort, 'cwd': str(self.adapter.effective_workdir)})
                    elif rid == 'hashi:turn':
                        self.turn_id = result.get('turn', {}).get('id')
                elif method and rid is not None:
                    if method == 'item/tool/requestUserInput' and params.get('threadId') == self.thread_id:
                        if str(rid) in self.pending:
                            continue
                        if self.turn_id and params.get('turnId') != self.turn_id:
                            await self.send(proc, {'id': rid, 'error': {'code': -32602, 'message': 'question turn mismatch'}})
                        else:
                            self.pending[str(rid)] = asyncio.create_task(self.reply_question(proc, rid, params))
                    else:
                        await self.send(proc, {'id': rid, 'error': {'code': -32601,
                            'message': 'Unsupported request; question answers do not grant permissions. Use HASHI managed tools.'}})
                elif method == 'serverRequest/resolved':
                    task = self.pending.pop(str(params.get('requestId')), None)
                    if task and not task.done():
                        task.cancel()
                    if task:
                        await asyncio.gather(task, return_exceptions=True)
                elif method == 'thread/tokenUsage/updated':
                    total = (params.get('tokenUsage') or {}).get('total') or {}
                    self.usage = {'input_tokens': total.get('inputTokens', 0),
                                  'cached_input_tokens': total.get('cachedInputTokens', 0),
                                  'output_tokens': total.get('outputTokens', 0)}
                elif method in ('item/started', 'item/completed'):
                    item = self._item(params.get('item') or {})
                    if item['type'] in ('agent_message', 'mcp_tool_call', 'file_change', 'command_execution', 'web_search', 'todo_list'):
                        yield encode({'type': method.replace('/', '.'), 'item': item})
                elif method == 'turn/started':
                    self.turn_id = (params.get('turn') or {}).get('id') or self.turn_id
                    yield encode({'type': 'turn.started'})
                elif method == 'turn/completed':
                    turn = params.get('turn') or {}
                    if turn.get('status') == 'completed':
                        yield encode({'type': 'turn.completed', 'usage': self.usage})
                    else:
                        yield encode({'type': 'turn.failed', 'error': turn.get('error') or {'message': 'Codex turn interrupted'}})
                    break
                elif method == 'error':
                    yield encode({'type': 'error', **params})
        finally:
            for task in self.pending.values():
                task.cancel()
            await asyncio.gather(*self.pending.values(), return_exceptions=True)
            for rpc_id in list(self.questions.created):
                self.questions.cancel(rpc_id)
            proc.stdin.close()
