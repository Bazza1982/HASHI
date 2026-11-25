from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from orchestrator import admin_local_testing, command_registry, runtime_session, ui_language
from orchestrator.command_interaction_bridge import dispatch_command_interaction
from orchestrator.commands import telegram as telegram_command_module
from orchestrator.command_ui import DIVIDER
from orchestrator import runtime_menu_views
from orchestrator.workbench_telegram_state import mirror_enabled


def test_telegram_menu_text_uses_standard_card_in_english():
    with ui_language.language_scope(None, locale="en"):
        text = runtime_menu_views.telegram_menu_text(enabled=True)
    assert text.startswith("📡 <b>TELEGRAM MIRROR</b>\n" + DIVIDER)
    assert "<b>Current</b> · <b>ON</b>" in text
    assert "Scope" in text
    assert "enabled as a preference" in text
    assert "requires a connected Telegram Bot" in text
    assert "/telegram on|off" in text


def test_telegram_menu_text_off_state_english():
    with ui_language.language_scope(None, locale="en"):
        text = runtime_menu_views.telegram_menu_text(enabled=False)
    assert "<b>Current</b> · <b>OFF</b>" in text
    assert "other sources do not mirror messages to Telegram" in text


def test_telegram_menu_text_chinese_resolves_no_key_fallback():
    with ui_language.language_scope(None, locale="zh-CN"):
        text = runtime_menu_views.telegram_menu_text(enabled=True)
    assert "TELEGRAM 镜像" in text
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
            session = runtime_session.ensure_store(runtime).create_session(
                owner_id="owner-a", agent_id=runtime.name)
            metadata = {
                "actor_id": 7,
                "instance_id": "TELEGRAM_MENU_TEST",
                "session_id": session["session_id"],
                "context_generation": session["context_generation"],
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
                open_invocation = opened["command_invocation"]
                self.assertEqual(open_invocation["type"], "hashi.frontend-command")
                self.assertEqual(open_invocation["version"], 2)
                self.assertEqual(open_invocation["connector_id"], "backend_api")
                self.assertEqual(open_invocation["command"], "telegram")
                self.assertEqual(open_invocation["authorization"]["decision"], "allowed")
                self.assertNotIn("actor_id", open_invocation)
                self.assertNotIn("connection_binding", open_invocation)
                card = opened["messages"][-1]
                self.assertEqual(card["channel"], "command-ui")
                self.assertIn("TELEGRAM MIRROR", card["text"])
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
                action_invocation = result["command_invocation"]
                self.assertEqual(action_invocation["command"], "telegram")
                self.assertEqual(action_invocation["issued_action_id"], button)
                self.assertEqual(action_invocation["revision"], action["revision"])
                self.assertEqual(action_invocation["session_id"], session["session_id"])
                self.assertEqual(action_invocation["context_generation"], 1)
                self.assertTrue(action_invocation["actor_digest"].startswith("sha256:"))
                self.assertEqual(mirror_enabled(root, "owner-a"), False)
                self.assertIn("OFF", result["messages"][0]["text"])
                self.assertEqual(
                    result["messages"][0]["message_ref"], card["message_ref"]
                )


import pytest

from orchestrator.config_json import read_config_json, write_config_json

DISCONNECT_TOKEN = "123456:abcdefghijklmnopqrstuvwxyz_ABCDEFGHI"


def _disconnect_config(root: Path, *, shared: bool = False, configured: bool = True, second_agent: bool = False):
    root.mkdir(parents=True, exist_ok=True)
    token_key = "shared.telegram" if shared else "lily.telegram"
    agents = [
        {
            "name": "lily",
            "workspace_dir": "workspaces/lily",
            "type": "flex",
            "telegram_token_key": token_key,
            "is_active": True,
        }
    ]
    if shared:
        agents.append(
            {
                "name": "momo",
                "workspace_dir": "workspaces/momo",
                "type": "flex",
                "telegram_token_key": token_key,
                "is_active": True,
            }
        )
    if second_agent:
        agents.append(
            {
                "name": "momo",
                "workspace_dir": "workspaces/momo",
                "type": "flex",
                "telegram_token_key": "momo.telegram",
                "is_active": True,
            }
        )
    config_path = root / "agents.json"
    secrets_path = root / "secrets.json"
    write_config_json(
        config_path, {"global": {"authorized_id": 7}, "agents": agents}
    )
    secrets = {"workbench_admin_token": "admin", "provider_api_key": "keep"}
    if configured:
        secrets[token_key] = DISCONNECT_TOKEN
        if second_agent:
            secrets["momo.telegram"] = DISCONNECT_TOKEN
    write_config_json(secrets_path, secrets)
    return config_path, secrets_path


def _disconnect_runtime(root: Path, *, name: str, sent, kernel_calls, authorized: int = 7):
    async def request_telegram_disconnect(agent_name, *, owner_id):
        kernel_calls.append((agent_name, owner_id))
        return True, "telegram disconnected"

    return NS(
        name=name,
        global_config=NS(
            authorized_id=authorized,
            bridge_home=root,
            config_path=str(root / "agents.json"),
            secrets_path=str(root / "secrets.json"),
        ),
        _is_authorized_user=lambda actor: actor == authorized,
        orchestrator=NS(request_telegram_disconnect=request_telegram_disconnect),
        _reply_text=_capture_reply(sent),
    )


def _capture_reply(sent):
    async def reply(update, text, *, parse_mode=None, reply_markup=None):
        sent.append(text)

    return reply


def _disconnect_update(owner="user:42"):
    return NS(
        effective_user=NS(id=7),
        _hashi_session_owner_id=owner,
    )


@pytest.mark.asyncio
async def test_disconnect_command_removes_token_and_stops_kernel(tmp_path):
    config_path, secrets_path = _disconnect_config(tmp_path)
    sent, kernel = [], []
    runtime = _disconnect_runtime(tmp_path, name="lily", sent=sent, kernel_calls=kernel)
    update = _disconnect_update()
    context = NS(args=["disconnect"])

    with ui_language.language_scope(None, locale="en"):
        await telegram_command_module.telegram_command(runtime, update, context)

    assert len(sent) == 1
    assert "removed locally" in sent[0]
    assert "BotFather" in sent[0]
    assert kernel == [("lily", "user:42")]
    stored = read_config_json(secrets_path)
    assert "lily.telegram" not in stored
    assert stored["provider_api_key"] == "keep"


@pytest.mark.asyncio
async def test_disconnect_command_targets_named_agent_and_reports_kernel_notice(
    tmp_path,
):
    config_path, secrets_path = _disconnect_config(tmp_path, second_agent=True)
    sent, kernel = [], []
    runtime = _disconnect_runtime(tmp_path, name="lily", sent=sent, kernel_calls=kernel)

    async def request_fails(agent_name, *, owner_id):
        kernel.append((agent_name, owner_id))
        return False, "stop receipt unconfirmed"

    runtime.orchestrator = NS(request_telegram_disconnect=request_fails)
    update = _disconnect_update()
    context = NS(args=["disconnect", "momo"])

    with ui_language.language_scope(None, locale="en"):
        await telegram_command_module.telegram_command(runtime, update, context)

    assert len(sent) == 1
    assert "removed locally" in sent[0]
    assert "stop receipt" in sent[0]
    assert kernel == [("momo", "user:42")]


@pytest.mark.asyncio
async def test_disconnect_command_reports_not_configured_shared_and_invalid(tmp_path):
    # Not configured: nothing is removed and the kernel is never contacted.
    config_path, secrets_path = _disconnect_config(tmp_path, configured=False)
    sent, kernel = [], []
    runtime = _disconnect_runtime(tmp_path, name="lily", sent=sent, kernel_calls=kernel)
    with ui_language.language_scope(None, locale="en"):
        await telegram_command_module.telegram_command(
            runtime, _disconnect_update(), NS(args=["disconnect"])
        )
    assert len(sent) == 1
    assert "nothing to disconnect" in sent[0]
    assert kernel == []
    assert "lily.telegram" not in read_config_json(secrets_path)

    # Shared credential: refusal with the dedicated message.
    shared_root = tmp_path / "shared"
    _disconnect_config(shared_root, shared=True)
    shared_runtime = _disconnect_runtime(
        shared_root, name="lily", sent=sent, kernel_calls=kernel
    )
    sent.clear()
    with ui_language.language_scope(None, locale="en"):
        await telegram_command_module.telegram_command(
            shared_runtime, _disconnect_update(), NS(args=["disconnect"])
        )
    assert len(sent) == 1
    assert "shared by multiple Agents" in sent[0]
    assert (
        read_config_json(shared_root / "secrets.json")["shared.telegram"]
        == DISCONNECT_TOKEN
    )

    # Unknown Agent: invalid-agent wording, no crash.
    sent.clear()
    with ui_language.language_scope(None, locale="en"):
        await telegram_command_module.telegram_command(
            runtime, _disconnect_update(), NS(args=["disconnect", "ghost"])
        )
    assert len(sent) == 1
    assert "does not exist" in sent[0]


@pytest.mark.asyncio
async def test_disconnect_command_requires_authorization(tmp_path):
    config_path, secrets_path = _disconnect_config(tmp_path)
    sent, kernel = [], []
    runtime = _disconnect_runtime(tmp_path, name="lily", sent=sent, kernel_calls=kernel)
    runtime._is_authorized_user = lambda actor: False
    with ui_language.language_scope(None, locale="en"):
        await telegram_command_module.telegram_command(
            runtime, _disconnect_update(), NS(args=["disconnect"])
        )
    assert sent == []
    assert "lily.telegram" in read_config_json(secrets_path)


if __name__ == "__main__":
    unittest.main()
# HASHI-Fingerprint: hfp_4a45aa3abdee4a8d8f7a43692561cdb4
