"""Focused behavior/authority tests; no network, provider or live runtime is used."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import unittest

from orchestrator.command_interactions import (
    Binding, Capture, InteractionError, MenuStore, markup_rows, perform_action, safe_url,
)


class MenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 100.0
        self.store = MenuStore(clock=lambda: self.now, wall_clock=lambda: 1700000000 + self.now)
        self.binding = Binding('instance', 'test-agent', '7', 'canonical-session', 1,
                               'clientabcdefghijkl', 'connectionabcdefghijkl')
        self.calls = []
        self.fingerprint = ('registry', 'example:', 'generation-1')
        self.authorized = True

    def resolve(self, data):
        return (self.fingerprint, self.callback) if data.startswith('example:') else None

    async def callback(self, query):
        self.calls.append(query.data)
        await query.edit_message_text('Updated ✓', parse_mode='HTML', reply_markup=self.keyboard())
        await query.answer('Saved')

    def capture(self):
        capture = Capture(self.store, self.binding, 'example', self.resolve)
        capture.chat_id = 7
        return capture

    @staticmethod
    def keyboard():
        return {'inline_keyboard': [[{'text': '下一页', 'callback_data': 'example:next'},
                                     {'text': '← Back', 'callback_data': 'example:back'}]]}

    async def open(self):
        capture = self.capture()
        message = await capture.capture_reply('Initial', parse_mode='HTML', reply_markup=self.keyboard())
        return capture, message.menu

    def payload(self, menu):
        return {'version': 1, 'op': 'act', 'menu_id': menu.id, 'revision': menu.revision,
                'button_id': menu.rows[0][0]['button_id']}

    async def action(self, menu, *, request_id='requestabcdefghijk', payload=None, binding=None, invoke=None):
        payload = payload or self.payload(menu)
        binding = binding or self.binding
        capture = self.capture()
        async def actual_invoke(resolved, query):
            await resolved[1](query)
        return await self.store.once(binding, request_id, payload, lambda: perform_action(
            self.store, binding, payload, capture, actor_id=7,
            authorize=lambda command: self.authorized, invoke=invoke or actual_invoke))

    async def test_real_capture_action_edits_one_stable_card_and_answers(self):
        initial, menu = await self.open()
        ref = initial.messages[0]['message_ref']
        self.assertLess(menu.message_id, 0)
        self.assertNotIn('example:next', json.dumps(initial.messages))
        result = await self.action(menu)
        self.assertTrue(result['ok'])
        self.assertEqual(self.calls, ['example:next'])
        self.assertEqual(len(result['messages']), 1)
        self.assertEqual(result['messages'][0]['message_ref'], ref)
        self.assertEqual(result['messages'][0]['text'], 'Updated ✓')
        self.assertEqual(result['messages'][0]['command_ui']['revision'], 2)
        self.assertEqual(result['notifications'], [{'text': 'Saved', 'alert': False}])

    async def test_duplicate_concurrent_request_executes_handler_once(self):
        _, menu = await self.open()
        payload = self.payload(menu)
        a, b = await asyncio.gather(self.action(menu, payload=payload), self.action(menu, payload=payload))
        self.assertEqual(a, b)
        self.assertEqual(len(self.calls), 1)
        a['messages'][0]['text'] = 'consumer mutation'
        c = await self.action(menu, payload=payload)
        self.assertEqual(c['messages'][0]['text'], 'Updated ✓')

    async def test_stale_revision_fails_before_second_execution(self):
        _, menu = await self.open()
        await self.action(menu)
        payload = self.payload(menu)
        payload['revision'] = 1
        result = await self.action(menu, request_id='differentrequestabcdefghijkl', payload=payload)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'command_menu_stale')
        self.assertEqual(len(self.calls), 1)

    async def test_request_id_with_different_payload_is_not_reused(self):
        _, menu = await self.open()
        await self.action(menu)
        with self.assertRaisesRegex(InteractionError, 'request_conflict'):
            await self.action(menu)
        self.assertEqual(len(self.calls), 1)

    async def test_binding_cannot_cross_agent_client_session_generation_or_connection(self):
        _, menu = await self.open()
        payload = self.payload(menu)
        for field, value in [('instance', 'other'), ('agent', 'other'), ('actor', '8'),
                             ('client', 'otherclientabcdefghijkl'), ('session', 'other'),
                             ('context_generation', 2), ('connection', 'other')]:
            with self.subTest(field=field):
                result = await self.action(menu, binding=dataclasses.replace(self.binding, **{field: value}), payload=payload)
                self.assertFalse(result['ok'])
                self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'command_menu_expired')
        self.assertEqual(self.calls, [])

    async def test_current_command_policy_is_rechecked(self):
        _, menu = await self.open()
        self.authorized = False
        result = await self.action(menu)
        self.assertFalse(result['ok'])
        self.assertEqual(result['http_status'], 403)
        self.assertEqual(self.calls, [])

    async def test_changed_handler_fingerprint_is_rejected(self):
        _, menu = await self.open()
        self.fingerprint = ('registry', 'example:', 'generation-2')
        result = await self.action(menu)
        self.assertEqual(result['error_code'], 'command_menu_handler_changed')
        self.assertEqual(self.calls, [])

    async def test_issued_button_only_and_invalid_revision_types(self):
        _, menu = await self.open()
        payload = self.payload(menu)
        payload['button_id'] = 'example:next'
        result = await self.action(menu, payload=payload)
        self.assertFalse(result['ok'])
        for revision in (True, '1', None):
            payload = self.payload(menu)
            payload['revision'] = revision
            result = await self.action(menu, request_id='anotherrequest' + str(revision) + 'abcdefgh', payload=payload)
            self.assertFalse(result['ok'], f'Invalid revision {revision!r} accepted')
        self.assertEqual(self.calls, [])

    async def test_expiration_rejects_a_previously_valid_button(self):
        _, menu = await self.open()
        payload = self.payload(menu)
        self.now += 901
        result = await self.action(menu, payload=payload)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'command_menu_expired')
        self.assertEqual(self.calls, [])

    async def test_callback_failure_consumes_revision_and_duplicate_is_uncertain(self):
        _, menu = await self.open()
        payload = self.payload(menu)
        async def fail_after_change(resolved, query):
            self.calls.append('changed')
            await query.edit_message_text('partly changed', reply_markup=self.keyboard())
            raise RuntimeError('never surface private details')
        with self.assertRaises(RuntimeError):
            await self.action(menu, payload=payload, invoke=fail_after_change)
        self.assertTrue(menu.closed)
        self.assertEqual(menu.actions, {})
        result = await self.action(menu, payload=payload, invoke=fail_after_change)
        self.assertEqual(result['error_code'], 'command_menu_outcome_unknown')
        self.assertEqual(self.calls, ['changed'])
        self.assertNotIn('private details', json.dumps(result))

    async def test_cancellation_does_not_allow_duplicate_reexecution(self):
        _, menu = await self.open()
        payload = self.payload(menu)
        begun = asyncio.Event()
        async def waiting(resolved, query):
            self.calls.append('started'); begun.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(self.action(menu, payload=payload, invoke=waiting))
        await begun.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        result = await self.action(menu, payload=payload)
        self.assertEqual(result['error_code'], 'command_menu_outcome_unknown')
        self.assertEqual(self.calls, ['started'])

    async def test_delete_and_markup_only_updates_are_structured(self):
        capture, menu = await self.open()
        message = capture.attach(menu)
        await message.edit_reply_markup(reply_markup={'inline_keyboard': [[{'text': 'Read', 'url': 'https://example.org'}]]})
        self.assertEqual(menu.text, 'Initial')
        self.assertEqual(menu.actions, {})
        await message.delete()
        self.assertTrue(menu.closed)
        self.assertEqual(capture.messages[0]['op'], 'delete')
        self.assertEqual(capture.messages[0]['command_ui']['rows'], [])

    async def test_cross_chat_send_and_late_capture_are_rejected(self):
        capture = self.capture()
        with self.assertRaisesRegex(InteractionError, 'cross_chat'):
            await capture.capture_send(8, 'Other target')
        capture.active = False
        with self.assertRaisesRegex(InteractionError, 'capture_closed'):
            await capture.capture_reply('Late output')
        self.assertEqual(len(self.store.menus), 0)

    async def test_unsupported_keyboard_and_specialty_buttons_fail_closed(self):
        capture = self.capture()
        buttons = [
            {'text': 'Login', 'login_url': {'url': 'https://example.org'}, 'callback_data': 'example:x'},
            {'text': 'Script', 'url': 'javascript:alert(1)'},
            {'text': 'Unknown', 'callback_data': 'missing:x'},
            {'text': 'Web app', 'web_app': {'url': 'https://example.org'}},
            {'text': 'Private URL', 'url': 'https://user:pass@example.org'},
        ]
        msg = await capture.capture_reply('X', reply_markup={'inline_keyboard': [buttons]})
        self.assertTrue(all(button['disabled'] for button in msg.menu.rows[0]))
        self.assertFalse(msg.menu.actions)
        with self.assertRaisesRegex(InteractionError, 'markup_unsupported'):
            markup_rows("InlineKeyboardMarkup(fake)")
        with self.assertRaisesRegex(InteractionError, 'too_large'):
            markup_rows({'inline_keyboard': [[{}] * 101]})

    async def test_to_dict_objects_and_unicode_render_without_runtime_reimplementation(self):
        class Markup:
            def to_dict(_self):
                return self.keyboard()
        capture = self.capture()
        msg = await capture.capture_reply('<b>菜单</b>', parse_mode='HTML', reply_markup=Markup())
        self.assertEqual(msg.menu.rows[0][0]['text'], '下一页')
        self.assertEqual(capture.messages[0]['meta']['parse_mode'], 'HTML')
        json.dumps(capture.result(), ensure_ascii=False)

    async def test_invalidation_affects_only_matching_binding(self):
        _, first = await self.open()
        other = self.store.create(dataclasses.replace(self.binding, client='otherclientabcdefgh'), 'example')
        changed = self.store.invalidate(self.binding)
        self.assertEqual([m.id for m in changed], [first.id])
        self.assertTrue(first.closed)
        self.assertFalse(other.closed)

    async def test_bounded_capacity_never_evicts_an_active_menu(self):
        self.store.max_menus = 1
        _, first = await self.open()
        with self.assertRaisesRegex(InteractionError, 'capacity'):
            self.store.create(self.binding, 'example')
        self.store.invalidate(self.binding)
        new = self.store.create(self.binding, 'example')
        self.assertNotEqual(new.id, first.id)
        self.assertNotIn(first.id, self.store.menus)

    async def test_invalid_request_cannot_run_and_capacity_does_not_drop_dedup_records(self):
        calls = []
        async def run():
            calls.append(1); return {'ok': True}
        with self.assertRaises(InteractionError):
            await self.store.once(self.binding, 'tiny', {}, run)
        self.store.max_requests = 1
        await self.store.once(self.binding, 'requestoneabcdefgh', {}, run)
        with self.assertRaisesRegex(InteractionError, 'capacity'):
            await self.store.once(self.binding, 'requesttwoabcdefgh', {}, run)
        result = await self.store.once(self.binding, 'requestoneabcdefgh', {}, run)
        self.assertTrue(result['ok'])
        self.assertEqual(calls, [1])

    def test_url_boundary(self):
        self.assertEqual(safe_url('https://example.org/docs?q=x'), 'https://example.org/docs?q=x')
        for url in ('//evil.test', 'file:///tmp/x', 'data:text/html,x', 'javascript:alert(1)',
                    'https://a:b@example.org', 'https://example.org\n/x', 'not a url'):
            self.assertIsNone(safe_url(url), url)


if __name__ == '__main__':
    unittest.main()
