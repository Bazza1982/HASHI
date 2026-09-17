from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from orchestrator import admin_local_testing, command_registry, ui_language
from orchestrator.command_interaction_bridge import dispatch_command_interaction
from orchestrator.commands import telegram as telegram_command_module
from orchestrator.command_ui import DIVIDER
from orchestrator import runtime_menu_views
from orchestrator.workbench_telegram_state import mirror_enabled


def test_telegram_menu_text_uses_standard_card_in_english():
    with ui_language.language_scope(None, locale="en"):
        text = runtime_menu_views.telegram_menu_text(enabled=True)
    assert text.startswith("📡 <b>WORKBENCH TELEGRAM MIRROR</b>\n" + DIVIDER)
    assert "<b>Current</b> · <b>ON</b>" in text
    assert "Scope" in text
    assert "mirrored to your Telegram chat" in text
    assert "/telegram on|off" in text


def test_telegram_menu_text_off_state_english():
    with ui_language.language_scope(None, locale="en"):
        text = runtime_menu_views.telegram_menu_text(enabled=False)
    assert "<b>Current</b> · <b>OFF</b>" in text
    assert "stay in the Workbench only" in text


def test_telegram_menu_text_chinese_resolves_no_key_fallback():
    with ui_language.language_scope(None, locale="zh-CN"):
        text = runtime_menu_views.telegram_menu_text(enabled=True)
    assert "工作台 TELEGRAM 镜像" in text
    assert "menu.telegram" not in text
    assert "<b>当前</b>" in text or "<b>Current</b>" in text
    assert "已" not in text or "镜像" in text


def test_telegram_keyboard_callback_data_and_selected_labels():
    with ui_language.language_scope(None, locale="en"):
        markup = runtime_menu_views.telegram_keyboard(enabled=True)
    rows = markup.to_dict()["inline_keyboard"]
    on, off = rows[0]
    assert on["text"] == "✓ ON"
    assert on["callback_data"] == "telegram:set:on"
    assert off["text"] == "OFF"
    assert off["callback_data"] == "telegram:set:off"

    with ui_language.language_scope(None, locale="en"):
        markup_off = runtime_menu_views.telegram_keyboard(enabled=False)
    on_off, off_off = markup_off.to_dict()["inline_keyboard"][0]
    assert on_off["text"] == "ON"
    assert off_off["text"] == "✓ OFF"


class TelegramCommandMenuIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_command_ui_open_and_button_toggles_per_owner_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspaces" / "fixture"
            workspace.mkdir(parents=True)

            runtime = NS(
                name="fixture",
                workspace_dir=workspace,
                global_config=NS(
                    authorized_id=7,
                    bridge_home=root,
                    project_root=root,
                    instance_id="TELEGRAM_MENU_TEST",
                ),
                _is_authorized_user=lambda actor: actor == 7,
                _is_command_allowed=lambda name: name == "telegram",
            )
            metadata = {
                "actor_id": 7,
                "instance_id": "TELEGRAM_MENU_TEST",
                "session_id": "test-session",
                "context_generation": 1,
                "session_surface": "workbench",
                "session_channel_key": "default",
                "connection_binding": "bindingabcdefghijk",
                "owner_id": "owner-a",
            }
            operation = {
                "version": 1,
                "op": "open",
                "command": "/telegram",
                "ui_locale": "en",
                "client_id": "clientabcdefghijkl",
                "request_id": "requestabcdefghijk",
            }
            commands = {command.name: command for command in telegram_command_module.COMMANDS}
            with (
                patch.object(admin_local_testing, "runtime_command_map", return_value=commands),
                patch.object(command_registry, "runtime_command_map", return_value=commands),
                patch.object(
                    command_registry,
                    "load_runtime_callbacks",
                    return_value=telegram_command_module.CALLBACKS,
                ),
            ):
                opened = await dispatch_command_interaction(runtime, operation, metadata)
                self.assertTrue(opened["ok"], opened)
                card = opened["messages"][-1]
                self.assertEqual(card["channel"], "command-ui")
                self.assertIn("WORKBENCH TELEGRAM MIRROR", card["text"])
                rows = card["command_ui"]["rows"]
                self.assertEqual(
                    {row["text"] for row in rows[0]},
                    {"✓ ON", "OFF"},
                )

                menu = runtime._command_interaction_store.menus[card["command_ui"]["menu_id"]]
                button = next(
                    key
                    for key, (data, _) in menu.actions.items()
                    if data == "telegram:set:off"
                )
                action = {
                    **operation,
                    "op": "act",
                    "command": "",
                    "request_id": "actionabcdefghijkl",
                    "menu_id": menu.id,
                    "revision": menu.revision,
                    "button_id": button,
                }
                result = await dispatch_command_interaction(runtime, action, metadata)
                self.assertTrue(result["ok"], result)
                self.assertEqual(mirror_enabled(root, "owner-a"), False)
                self.assertIn("OFF", result["messages"][0]["text"])
                self.assertEqual(
                    result["messages"][0]["message_ref"], card["message_ref"]
                )


if __name__ == "__main__":
    unittest.main()
