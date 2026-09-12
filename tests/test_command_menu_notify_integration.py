"""Run locally in the assembled HASHI environment.

Uses the real /notify handler, Telegram SDK markup, command executor, registry
callback and language/audit helpers. Notification storage is a fixture, so this
never changes a real installation or contacts Telegram. Not run in the sandbox,
where the HASHI dependency environment and complete checkout were unavailable.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from orchestrator import admin_local_testing, command_registry, telegram_notifications
from orchestrator.command_interaction_bridge import dispatch_command_interaction
from orchestrator.commands.notify import COMMANDS, CALLBACKS


class NotifyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_notify_menu_and_callback_work_through_real_local_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspaces' / 'fixture'; workspace.mkdir(parents=True)
            settings = {'mode': 'on'}
            writes = []
            def set_mode(runtime, mode):
                writes.append(mode); settings['mode'] = mode
                return mode
            runtime = NS(name='fixture', workspace_dir=workspace,
                         global_config=NS(authorized_id=7, bridge_home=root, project_root=root,
                                          instance_id='COMMAND_MENU_TEST'),
                         _is_authorized_user=lambda actor: actor == 7,
                         _is_command_allowed=lambda name: name == 'notify')
            metadata = {'actor_id': 7, 'instance_id': 'COMMAND_MENU_TEST',
                        'session_id': 'test-session', 'context_generation': 1,
                        'session_surface': 'workbench', 'session_channel_key': 'default',
                        'connection_binding': 'bindingabcdefghijk'}
            operation = {'version': 1, 'op': 'open', 'command': '/notify', 'ui_locale': 'en',
                         'client_id': 'clientabcdefghijkl', 'request_id': 'requestabcdefghijk'}
            commands = {command.name: command for command in COMMANDS}
            with (patch.object(admin_local_testing, 'runtime_command_map', return_value=commands),
                  patch.object(command_registry, 'runtime_command_map', return_value=commands),
                  patch.object(command_registry, 'load_runtime_callbacks', return_value=CALLBACKS),
                  patch.object(telegram_notifications, 'notification_mode', side_effect=lambda _: settings['mode']),
                  patch.object(telegram_notifications, 'set_notification_mode', side_effect=set_mode)):
                opened = await dispatch_command_interaction(runtime, operation, metadata)
                self.assertTrue(opened['ok'], opened)
                card = opened['messages'][-1]
                menu = runtime._command_interaction_store.menus[card['command_ui']['menu_id']]
                # Select an ISSUED opaque button. Never submit raw callback data.
                button = next(key for key, (data, _) in menu.actions.items() if data == 'notify:quiet')
                action = {**operation, 'op': 'act', 'command': '', 'request_id': 'actionabcdefghijkl',
                          'menu_id': menu.id, 'revision': menu.revision, 'button_id': button}
                result = await dispatch_command_interaction(runtime, action, metadata)
                self.assertTrue(result['ok'], result)
                self.assertEqual(writes, ['quiet'])
                self.assertEqual(result['messages'][0]['message_ref'], card['message_ref'])
                self.assertEqual(result['messages'][0]['command_ui']['revision'], 2)
                self.assertIn('QUIET', result['messages'][0]['text'])
                replay = await dispatch_command_interaction(runtime, action, metadata)
                self.assertEqual(replay, result)
                self.assertEqual(writes, ['quiet'])


if __name__ == '__main__':
    unittest.main()
