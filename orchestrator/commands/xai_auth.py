"""Telegram command for HASHI-native xAI OAuth status (no Hermes)."""

from __future__ import annotations

import html
from typing import Any

from orchestrator.command_registry import RuntimeCommand
from orchestrator.command_ui import card_title
from orchestrator import ui_language


def _is_authorized(runtime: Any, update: Any) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    if callable(checker):
        return bool(checker(user_id))
    global_config = getattr(runtime, "global_config", None)
    authorized_id = getattr(global_config, "authorized_id", None)
    return authorized_id is None or user_id == authorized_id


async def _send(runtime: Any, update: Any, text: str) -> None:
    if hasattr(runtime, "_reply_text"):
        await runtime._reply_text(update, text, parse_mode="HTML")
        return
    message = getattr(update, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text, parse_mode="HTML")
        return
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is not None and hasattr(runtime, "send_long_message"):
        await runtime.send_long_message(chat_id, text, request_id="xai-auth-command", purpose="command")


async def xaiauth_command(runtime: Any, update: Any, context: Any) -> None:
    if not _is_authorized(runtime, update):
        return

    from adapters.hashi_xai_oauth import oauth_status

    args = [str(arg).strip().lower() for arg in (getattr(context, "args", None) or []) if str(arg).strip()]
    action = args[0] if args else "status"
    global_config = getattr(runtime, "global_config", None)

    if action in {"status", "show"}:
        status = oauth_status(global_config=global_config)
        logged_in = bool(status.get("logged_in"))
        relogin_required = bool(status.get("relogin_required"))
        client_configured = bool(status.get("client_id_configured"))
        if relogin_required:
            detail = ui_language.tr("xai.detail.relogin")
        elif logged_in:
            detail = ui_language.tr("xai.detail.logged_in")
        elif status.get("has_refresh_token") or status.get("has_access_token"):
            detail = ui_language.tr("xai.detail.token_missing")
        else:
            detail = ui_language.tr("xai.detail.not_logged_in")
        lines = [
            card_title("🔐", "xAI authorization"),
            "",
            f"<b>{html.escape(ui_language.tr('common.current'))}</b> · <b>{html.escape(ui_language.tr('xai.signed_in') if logged_in else ui_language.tr('xai.signed_out'))}</b>",
            f"<b>{html.escape(ui_language.tr('xai.relogin_required'))}</b> · <code>{html.escape(ui_language.tr('common.yes') if relogin_required else ui_language.tr('common.no'))}</code>",
            f"<b>{html.escape(ui_language.tr('xai.client_configured'))}</b> · <code>{html.escape(ui_language.tr('common.yes') if client_configured else ui_language.tr('common.no'))}</code>",
            f"<b>{html.escape(ui_language.tr('xai.credential_store'))}</b> · <code>{html.escape(str(status.get('auth_store') or ui_language.tr('common.unknown')))}</code>",
            "",
            detail,
            "",
            ui_language.tr("xai.read_only"),
            "",
            f"<b>{html.escape(ui_language.tr('common.use'))}</b>",
            f"<code>python hashi.py auth xai login</code> · {html.escape(ui_language.tr('xai.use.login'))}",
            f"<code>/backend her grok-4.5</code> · {html.escape(ui_language.tr('xai.use.backend'))}",
        ]
        await _send(runtime, update, "\n".join(lines))
        return

    await _send(
        runtime,
        update,
        f"{card_title('🔐', 'xAI authorization')}\n\n"
        f"<b>{html.escape(ui_language.tr('common.current'))}</b> · {html.escape(ui_language.tr('xai.invalid'))}\n\n"
        f"{ui_language.tr('xai.invalid_help')}",
    )


COMMANDS = [
    RuntimeCommand(
        name="xaiauth",
        description="HASHI-native xAI OAuth status (login via shell)",
        callback=xaiauth_command,
    ),
]
