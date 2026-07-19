"""Telegram command for HASHI-native xAI OAuth status (no Hermes)."""

from __future__ import annotations

from typing import Any

from orchestrator.command_registry import RuntimeCommand


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
        await runtime._reply_text(update, text)
        return
    message = getattr(update, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text)
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
        lines = [
            "HASHI xAI OAuth (native, no Hermes)",
            f"logged_in: {status.get('logged_in')}",
            f"relogin_required: {status.get('relogin_required')}",
            f"client_id_configured: {status.get('client_id_configured')}",
            f"auth_store: {status.get('auth_store')}",
            f"message: {status.get('message')}",
            "",
            "Login from shell: python hashi.py auth xai login",
            "Then on Xishi: /backend claw-cli grok-4.5",
        ]
        await _send(runtime, update, "\n".join(lines))
        return

    await _send(
        runtime,
        update,
        "Usage: /xaiauth [status]\n"
        "Device-code login must be completed on the host shell:\n"
        "  python hashi.py auth xai login",
    )


COMMANDS = [
    RuntimeCommand(
        name="xaiauth",
        description="HASHI-native xAI OAuth status (login via shell)",
        callback=xaiauth_command,
    ),
]
