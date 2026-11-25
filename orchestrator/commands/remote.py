"""`/remote token` - owner-facing Shared Token status, set and clear.

The credential value never appears in a reply, a log line or an error. The
command reports a masked form plus a short fingerprint, and it confirms the
Remote service's loaded credential generation through the loopback health
endpoint instead of claiming a reload happened.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from telegram import ForceReply

from orchestrator import remote_token, ui_language
from orchestrator.command_registry import RuntimeCommand

PROMPT_TTL_SECONDS = 300
PENDING_ATTRIBUTE = "_remote_token_pending"


def _paths(runtime: Any) -> tuple[Path, Path]:
    config = getattr(runtime, "global_config", None)
    secrets = Path(str(getattr(config, "secrets_path")))
    return secrets.parent, secrets


def _remote_port(root: Path) -> int | None:
    try:
        from orchestrator.remote_lifecycle import load_settings

        return int(load_settings(root).port)
    except Exception:
        return None


def _read_status(root: Path, secrets: Path) -> remote_token.RemoteTokenStatus:
    port = _remote_port(root)
    health = (
        remote_token.fetch_remote_credential(port)
        if port
        else {"reachable": False, "reason": "port_unknown"}
    )
    return remote_token.load_status(root, secrets_path=secrets, remote_credential=health)


def _source_text(source: str) -> str:
    key = f"remote.token.source.{source}"
    return ui_language.tr(key) if ui_language.tr(key) != key else source


def _remote_text(status: remote_token.RemoteTokenStatus) -> list[str]:
    remote = status.remote or {}
    if not remote.get("reachable"):
        return [ui_language.tr("remote.token.remote_unreachable")]
    loaded = ui_language.tr(
        "remote.token.loaded" if remote.get("configured") else "remote.token.not_loaded"
    )
    generation = ui_language.tr(
        "remote.token.generation", generation=int(remote.get("generation") or 0)
    )
    if remote.get("last_reload_at"):
        moment = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(remote["last_reload_at"])))
        generation = f"{generation} · {moment}"
    mismatch = bool(status.configured) != bool(remote.get("configured"))
    lines = [loaded, generation]
    lines.append(ui_language.tr("remote.token.reload_pending" if mismatch else "remote.token.reload_ok"))
    return lines


def render_status(status: remote_token.RemoteTokenStatus) -> str:
    lines = [ui_language.tr("remote.token.title"), ""]
    lines.append(ui_language.tr("remote.token.source_line", source=_source_text(status.source)))
    if status.configured:
        lines.append(ui_language.tr("remote.token.masked_line", masked=status.masked))
        lines.append(ui_language.tr("remote.token.fingerprint_line", fingerprint=status.fingerprint))
    lines.append(ui_language.tr("remote.token.revision_line", revision=status.revision or "-"))
    if status.disabled:
        lines.append(ui_language.tr("remote.token.disabled"))
    if status.environment_locked:
        lines.append(ui_language.tr("remote.token.environment_locked"))
    if status.error:
        lines.append(ui_language.tr("remote.token.invalid_state", error=status.error))
    lines.extend(_remote_text(status))
    lines.append("")
    lines.append(ui_language.tr("remote.token.hint"))
    return "\n".join(line for line in lines if line is not None)


def _failure_text(error: remote_token.RemoteTokenError) -> str:
    keys = {
        "remote_token_invalid": "remote.token.invalid",
        "remote_token_environment_locked": "remote.token.environment_locked",
        "remote_token_revision_conflict": "remote.token.revision_conflict",
        "remote_token_write_failed": "remote.token.write_failed",
    }
    key = keys.get(error.error_code, "remote.token.write_failed")
    detail = f" ({error.detail})" if error.detail else ""
    return ui_language.tr(key) + detail


def _authorized(runtime: Any, update: Any) -> bool:
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    checker = getattr(runtime, "_is_authorized_user", None)
    return user_id is not None and callable(checker) and bool(checker(user_id))


def _clear_pending(runtime: Any) -> None:
    if hasattr(runtime, PENDING_ATTRIBUTE):
        try:
            delattr(runtime, PENDING_ATTRIBUTE)
        except AttributeError:
            pass


async def _apply_token(runtime: Any, update: Any, value: str) -> None:
    root, secrets = _paths(runtime)
    try:
        status = remote_token.replace_token(root, value, secrets_path=secrets)
    except remote_token.RemoteTokenError as error:
        await runtime._reply_text(update, _failure_text(error))
        return
    await runtime._reply_text(
        update,
        f"{ui_language.tr('remote.token.saved')}\n\n{render_status(_read_status(root, secrets))}",
    )


async def remote_command(runtime: Any, update: Any, context: Any) -> None:
    if not _authorized(runtime, update):
        await runtime._reply_text(update, ui_language.tr("remote.token.denied"))
        return
    args = list(getattr(context, "args", None) or [])
    action = str(args[0]).lower() if args else "status"
    root, secrets = _paths(runtime)

    if action in {"status", ""}:
        await runtime._reply_text(update, render_status(_read_status(root, secrets)))
        return

    if action == "verify":
        await runtime._reply_text(update, render_status(_read_status(root, secrets)))
        return

    if action == "clear":
        try:
            remote_token.clear_token(root, secrets_path=secrets)
        except remote_token.RemoteTokenError as error:
            await runtime._reply_text(update, _failure_text(error))
            return
        await runtime._reply_text(
            update,
            f"{ui_language.tr('remote.token.cleared')}\n\n{render_status(_read_status(root, secrets))}",
        )
        return

    if action == "set":
        value = " ".join(args[1:]).strip()
        if value:
            await _apply_token(runtime, update, value)
            return
        prompt = await runtime._reply_text(
            update,
            ui_language.tr("remote.token.prompt"),
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder=ui_language.tr("remote.token.prompt_placeholder"),
            ),
        )
        prompt_id = getattr(prompt, "message_id", None)
        if prompt_id is None:
            await runtime._reply_text(update, ui_language.tr("remote.token.prompt_unavailable"))
            return
        user_id = getattr(getattr(update, "effective_user", None), "id", None)
        chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
        setattr(runtime, PENDING_ATTRIBUTE, {
            "prompt_message_id": prompt_id,
            "user_id": user_id,
            "chat_id": chat_id,
            "expires_at": time.time() + PROMPT_TTL_SECONDS,
        })
        return

    await runtime._reply_text(update, ui_language.tr("remote.token.usage"))


async def handle_pending_reply(runtime: Any, update: Any) -> bool:
    """Consume only the ForceReply message created by this command."""
    pending = getattr(runtime, PENDING_ATTRIBUTE, None)
    if not isinstance(pending, dict):
        return False
    message = getattr(update, "message", None)
    reply_to = getattr(message, "reply_to_message", None) if message is not None else None
    if message is None or reply_to is None:
        return False
    if int(getattr(reply_to, "message_id", 0) or 0) != int(pending.get("prompt_message_id") or 0):
        return False
    if int(getattr(getattr(update, "effective_user", None), "id", 0) or 0) != int(pending.get("user_id") or 0):
        return False
    if int(getattr(getattr(update, "effective_chat", None), "id", 0) or 0) != int(pending.get("chat_id") or 0):
        return False

    _clear_pending(runtime)
    if float(pending.get("expires_at") or 0) < time.time():
        await runtime._reply_text(update, ui_language.tr("remote.token.expired"))
        return True

    value = str(getattr(message, "text", "") or "").strip()
    try:
        await message.delete()
    except Exception:
        # Deleting the plaintext message is best effort and depends on the
        # client and bot rights. The value is never echoed either way.
        pass
    if value.lower() in {"/cancel", "cancel", "取消"}:
        await runtime._reply_text(update, ui_language.tr("remote.token.cancelled"))
        return True
    if not value:
        await runtime._reply_text(update, ui_language.tr("remote.token.invalid"))
        return True
    await _apply_token(runtime, update, value)
    return True


COMMANDS = [
    RuntimeCommand(
        name="remote",
        description="Inspect or replace the HASHI Remote shared token",
        callback=remote_command,
    ),
]
# HASHI-Fingerprint: hfp_b418d72a39d449e386fcb40b8b89eee2
