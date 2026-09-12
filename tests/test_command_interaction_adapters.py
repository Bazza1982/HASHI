"""Isolated adapter tests. Runtime/registry/identity inputs are explicit fixtures.

Exercises the production ingress and dispatcher, not a real HASHI process.
The separately provided notify integration test uses HASHI's real executor.
"""
from __future__ import annotations
import json
import sys
import types
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import orchestrator
import pytest
from orchestrator import command_interaction_bridge as bridge


def module(name, **values):
    result = types.ModuleType(name)
    result.__dict__.update(values)
    return result


@pytest.mark.asyncio
class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.metadata = {'actor_id': 7, 'instance_id': 'test', 'session_id': 'canonical',
                         'context_generation': 1, 'connection_binding': 'bindingabcdefghijk',
                         'session_surface': 'workbench', 'session_channel_key': 'default'}
        self.allowed = True
        self.executions, self.actions, self.wrapped = [], [], []
        self.runtime = NS(name='agent', global_config=NS(authorized_id=7),
                          get_runtime_metadata=lambda: {'model': 'test-model'},
                          _is_authorized_user=lambda actor: actor == 7,
                          _is_command_allowed=lambda name: self.allowed)
        async def original_send(*args, **kwargs):
            raise AssertionError('The real delivery channel must not be called')
        self.runtime._send_text = original_send
        self.original_send = original_send
        async def callback(update, context):
            self.actions.append(update.callback_query.data)
            self.assertEqual(update._hashi_session_id, 'canonical')
            await update.callback_query.edit_message_text('Next page', reply_markup=self.keyboard())
            await update.callback_query.answer('Done')
        self.runtime.callback_example = callback
        def wrap(name, callback):
            async def wrapped(update, context):
                self.wrapped.append(name)
                await callback(update, context)
            return wrapped
        self.runtime._wrap_callback = wrap
        async def execute(runtime, command, *, chat_id, source_channel, session_metadata, capture_store):
            self.executions.append((command, source_channel, session_metadata))
            await capture_store.capture_reply('Menu page', parse_mode='HTML', reply_markup=self.keyboard())
            return {'ok': True, 'messages': capture_store.messages}
        @contextmanager
        def scope(runtime, capture):
            try: yield
            finally: capture.active = False
        class FakeUpdate:
            def __init__(self, user, chat, capture, text, session_metadata):
                self.effective_user = NS(id=user)
                self.effective_chat = NS(id=chat)
                self._hashi_session_id = session_metadata['session_id']
        class Audit:
            def __init__(_self, **kwargs): pass
            def fail(_self, error): pass
            def finish(_self): pass
        language = module('orchestrator.ui_language', normalize_locale=lambda x: 'en' if x != 'zh-CN' else x,
                          language_scope=lambda *args, **kwargs: nullcontext())
        self.modules = {
            'orchestrator.ui_language': language,
            'orchestrator.admin_local_testing': module('orchestrator.admin_local_testing',
                supported_commands=lambda runtime: ['example', 'restart'],
                _FakeUpdate=FakeUpdate, _capture_local_output=scope,
                execute_local_command=execute, _runtime_audit_path=lambda runtime: Path('/unused'),
                _runtime_agent_name=lambda runtime: runtime.name,
                _split_command=lambda line: (line.lstrip('/').split()[0], [])),
            'orchestrator.slash_command_audit': module('orchestrator.slash_command_audit',
                SlashCommandAuditSession=Audit, bind_slash_command_audit_session=lambda x: nullcontext()),
            'orchestrator.runtime_command_binding': module('orchestrator.runtime_command_binding',
                CALLBACK_BINDINGS=[NS(pattern='^example:', method_name='callback_example')],
                get_flexible_bot_commands=lambda runtime, locale: [NS(command='example', description='Example from the registry'),
                                                                  NS(command='restart', description='Human-only')]),
            'orchestrator.command_registry': module('orchestrator.command_registry',
                load_runtime_callbacks=lambda: [], runtime_command_map=lambda: {}),
            'orchestrator.command_specs': module('orchestrator.command_specs',
                COMMAND_SPECS=[NS(name='example', guide=NS(usage='/example [value]'))]),
        }
        self.patcher = patch.dict(sys.modules, self.modules)
        self.patcher.start(); self.addCleanup(self.patcher.stop)
        self.language_patch = patch.object(orchestrator, 'ui_language', language, create=True)
        self.language_patch.start(); self.addCleanup(self.language_patch.stop)

    @staticmethod
    def keyboard():
        return {'inline_keyboard': [[{'text': 'Next', 'callback_data': 'example:next'}]]}

    def payload(self, op='open', **extra):
        return {'version': 1, 'op': op, 'client_id': 'clientabcdefghijk',
                'request_id': 'requestabcdefghijkl', 'ui_locale': 'en', 'command': '/example', **extra}

    async def dispatch(self, value=None, metadata=None):
        return await bridge.dispatch_command_interaction(self.runtime, value or self.payload(),
                                                          self.metadata if metadata is None else metadata)

    async def test_open_and_native_wrapped_callback_preserve_binding_and_inplace_identity(self):
        opened = await self.dispatch()
        self.assertTrue(opened['ok'])
        self.assertFalse(opened['refresh_required'])
        menu = opened['messages'][0]['command_ui']
        result = await self.dispatch(self.payload('act', request_id='actionabcdefghijkl',
            menu_id=menu['menu_id'], revision=menu['revision'], button_id=menu['rows'][0][0]['button_id']))
        self.assertTrue(result['ok'])
        self.assertEqual(result['messages'][0]['message_ref'], opened['messages'][0]['message_ref'])
        self.assertEqual(result['messages'][0]['text'], 'Next page')
        self.assertEqual(self.wrapped, ['callback_example'])
        self.assertEqual(len(self.executions), 1)
        self.assertIs(self.runtime._send_text, self.original_send)

    async def test_catalogue_is_registry_derived_and_policy_disabled_is_not_available(self):
        self.allowed = False
        result = await self.dispatch(self.payload('catalogue'))
        self.assertEqual(result['commands'][0]['usage'], '/example [value]')
        self.assertEqual(result['commands'][0]['description'], 'Example from the registry')
        self.assertTrue(all(not row['enabled'] for row in result['commands']))
        self.assertEqual(self.executions, [])

    async def test_owner_and_shape_validation_precede_execution(self):
        for field, value in [('version', True), ('op', {}), ('client_id', 'bad'), ('request_id', None)]:
            with self.subTest(field=field):
                result = await self.dispatch(self.payload(**{field: value}))
                self.assertFalse(result['ok'])
        result = await self.dispatch(metadata={**self.metadata, 'actor_id': 8})
        self.assertEqual(result['http_status'], 403)
        result = await self.dispatch(metadata={**self.metadata, 'context_generation': True})
        self.assertFalse(result['ok'])
        self.assertEqual(self.executions, [])

    async def test_close_is_projection_only_and_later_action_cannot_execute(self):
        opened = await self.dispatch()
        menu = opened['messages'][0]['command_ui']
        result = await self.dispatch(self.payload('close', request_id='closerequestabcdefgh',
                                               menu_id=menu['menu_id'], revision=menu['revision']))
        self.assertTrue(result['messages'][0]['command_ui']['closed'])
        result = await self.dispatch(self.payload('act', request_id='actionrequestabcdef',
            menu_id=menu['menu_id'], revision=menu['revision'], button_id=menu['rows'][0][0]['button_id']))
        self.assertFalse(result['ok'])
        self.assertEqual(self.actions, [])

    async def test_dynamic_registry_callback_uses_runtime_update_context_signature(self):
        received = []
        async def dynamic(runtime, update, context):
            received.append(runtime)
            await update.callback_query.edit_message_text('Dynamic result')
        self.modules['orchestrator.runtime_command_binding'].CALLBACK_BINDINGS = []
        self.modules['orchestrator.command_registry'].load_runtime_callbacks = lambda: [NS(pattern='^example:', callback=dynamic)]
        opened = await self.dispatch()
        menu = opened['messages'][0]['command_ui']
        result = await self.dispatch(self.payload('act', request_id='dynamicrequestabcdef',
            menu_id=menu['menu_id'], revision=menu['revision'], button_id=menu['rows'][0][0]['button_id']))
        self.assertTrue(result['ok'])
        self.assertEqual(received, [self.runtime])
        self.assertEqual(result['messages'][0]['text'], 'Dynamic result')
        self.assertTrue(result['messages'][0]['command_ui']['closed'])

    async def test_failed_callback_is_not_replayed_and_error_details_are_not_exposed(self):
        async def broken(update, context):
            self.actions.append('begun')
            raise RuntimeError('sensitive-path-secret-must-stay-private')
        self.runtime.callback_example = broken
        opened = await self.dispatch()
        menu = opened['messages'][0]['command_ui']
        operation = self.payload('act', request_id='failedrequestabcdef', menu_id=menu['menu_id'],
                                 revision=menu['revision'], button_id=menu['rows'][0][0]['button_id'])
        first = await self.dispatch(operation)
        second = await self.dispatch(operation)
        self.assertEqual(first, second)
        self.assertEqual(self.actions, ['begun'])
        self.assertEqual(first['error_code'], 'command_menu_outcome_unknown')
        self.assertNotIn('sensitive-path', json.dumps(first))
        self.assertIs(self.runtime._send_text, self.original_send)


if __name__ == '__main__':
    unittest.main()
