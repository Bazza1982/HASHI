"""Telegram command for HASHI-native xAI OAuth status (no Hermes)."""

from __future__ import annotations

import html
from typing import Any

from orchestrator.command_registry import RuntimeCommand
from orchestrator.command_ui import card_title


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
        lines = [
            card_title("🔐", "xAI authorization"),
            "",
            f"<b>Current</b> · <b>{'SIGNED IN' if logged_in else 'SIGNED OUT'}</b>",
            f"<b>Relogin required</b> · <code>{'YES' if relogin_required else 'NO'}</code>",
            f"<b>Client configured</b> · <code>{'YES' if client_configured else 'NO'}</code>",
            f"<b>Credential store</b> · <code>{html.escape(str(status.get('auth_store') or 'unknown'))}</code>",
            "",
            html.escape(str(status.get("message") or "No authorization detail available.")),
            "",
            "Login is completed on the host shell; this card is read-only.",
            "",
            "<b>Use</b>",
            "<code>python hashi.py auth xai login</code> · sign in from the host shell",
            "<code>/backend claw-cli grok-4.5</code> · select xAI after login",
        ]
        await _send(runtime, update, "\n".join(lines))
        return

    await _send(
        runtime,
        update,
        f"{card_title('🔐', 'xAI authorization')}\n\n"
        "<b>Current</b> · invalid option\n\n"
        "Use <code>/xaiauth status</code>. Device-code login must be completed on the host shell with "
        "<code>python hashi.py auth xai login</code>.",
    )


COMMANDS = [
    RuntimeCommand(
        name="xaiauth",
        description="HASHI-native xAI OAuth status (login via shell)",
        callback=xaiauth_command,
    ),
]
