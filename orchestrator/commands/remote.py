"""`/remote token` - owner-facing Shared Token status, verification and rotation.

This module is *not* a command module. The single `/remote` command stays in
`orchestrator.runtime_remote.cmd_remote`; that handler forwards the `token`
subcommand here, so every existing `/remote` lifecycle subcommand keeps its
own dispatcher and no duplicate registration can shadow it.

The credential value never appears in a reply, a log line or an error. Whether
the running Remote service accepts it is proven with one real HMAC-signed
request against an existing protected endpoint - never by comparing two
configuration flags.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from telegram import ForceReply

from orchestrator import remote_token, ui_language

PROMPT_TTL_SECONDS = 300
PENDING_ATTRIBUTE = "_remote_token_pending"
CANCEL_WORDS = frozenset({"cancel", "取消", "取消设置"})
REDACTED_INLINE_VALUE = "[redacted]"


def inline_token_attempt(command_name: str, args: list[str] | None) -> bool:
    """True when ingress carries `/remote token set <value>` inline."""

    if str(command_name or "").casefold() != "remote":
        return False
    words = [str(value) for value in (args or [])]
    if len(words) < 3:
        return False
    if words[0].casefold() != "token" or words[1].casefold() != "set":
        return False
    return any(value.strip() for value in words[2:])


def redact_inline_token_args(command_name: str, args: list[str] | None) -> list[str]:
    """Return command args that are safe to persist.

    The credential is replaced before the audit record, frontend projection or
    command invocation payload is written, rather than after the handler has
    already refused it.
    """

    words = [str(value) for value in (args or [])]
    if not inline_token_attempt(command_name, words):
        return words
    return words[:2] + [REDACTED_INLINE_VALUE]


def is_pending_prompt_reply(runtime: Any, update: Any) -> bool:
    """Is this update an answer to *our* open token prompt?

    This is an identity check only: it never consumes, clears or deletes
    anything, so ingress can route the reply before ordinary message
    persistence without changing the prompt lifecycle.
    """

    pending = getattr(runtime, PENDING_ATTRIBUTE, None)
    if not isinstance(pending, dict):
        return False
    message = getattr(update, "message", None)
    if message is None:
        return False
    reply_to = getattr(message, "reply_to_message", None)
    prompt_id = getattr(reply_to, "message_id", None)
    # Only a reply to *this* prompt is a credential. An unrelated message
    # arriving while the prompt is open must keep its normal ingress path.
    if prompt_id is None or prompt_id != pending.get("prompt_message_id"):
        return False
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    if pending.get("user_id") is not None and user_id != pending.get("user_id"):
        return False
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    if pending.get("chat_id") is not None and chat_id != pending.get("chat_id"):
        return False
    return True


def _paths(runtime: Any) -> tuple[Path, Path]:
    config = getattr(runtime, "global_config", None)
    secrets = Path(str(getattr(config, "secrets_path")))
    return secrets.parent, secrets


def _endpoint(runtime: Any, root: Path) -> tuple[int, bool, str]:
    """Where to prove the credential: loopback port, TLS flag, sender name."""
    cfg: dict[str, Any] = {}
    snapshot = getattr(runtime, "_remote_config_snapshot", None)
    if callable(snapshot):
        try:
            cfg = snapshot() or {}
        except Exception:
            cfg = {}
    port = int(cfg.get("port") or 0)
    if port <= 0:
        try:
            from orchestrator.remote_lifecycle import load_settings

            port = int(load_settings(root).port)
        except Exception:
            port = 0
    config = getattr(runtime, "global_config", None)
    instance = str(getattr(config, "instance_id", "") or "HASHI")
    return port, bool(cfg.get("use_tls", True)), instance


def _probe(runtime: Any, root: Path, secrets: Path) -> dict:
    """One signed request against the running service, using the real credential."""
    _source, token = remote_token.effective_token(root, secrets_path=secrets)
    port, use_tls, instance = _endpoint(runtime, root)
    result = remote_token.verify_credential(
        token, port=port, use_tls=use_tls, from_instance=instance
    )
    # A service that answers on the other scheme is still a service; only a
    # genuinely silent port may be reported as unavailable.
    if result.get("outcome") == "unavailable" and port:
        alternate = remote_token.verify_credential(
            token, port=port, use_tls=not use_tls, from_instance=instance
        )
        if alternate.get("outcome") != "unavailable":
            return alternate
    return result


def _read_status(root: Path, secrets: Path, verification: dict | None = None):
    return remote_token.load_status(
        root, secrets_path=secrets, verification=verification or {}
    )


def _source_text(source: str) -> str:
    key = f"remote.token.source.{source}"
    translated = ui_language.tr(key)
    return translated if translated != key else source


def _verification_text(verification: dict) -> str:
    outcome = str(verification.get("outcome") or "")
    status = int(verification.get("status") or 0)
    if outcome == "verified":
        return ui_language.tr("remote.token.verified", status=status)
    if outcome == "rejected":
        return ui_language.tr("remote.token.rejected", status=status)
    if outcome == "unavailable":
        return ui_language.tr("remote.token.unavailable")
    if outcome == "missing":
        return ui_language.tr("remote.token.not_checked_missing")
    if outcome == "failed":
        return ui_language.tr("remote.token.check_failed", status=status)
    return ui_language.tr("remote.token.not_checked")


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
    lines.append(_verification_text(status.verification or {}))
    lines.append("")
    lines.append(ui_language.tr("remote.token.hint"))
    return "\n".join(lines)


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
        remote_token.replace_token(root, value, secrets_path=secrets)
    except remote_token.RemoteTokenError as error:
        await runtime._reply_text(update, _failure_text(error))
        return
    await runtime._reply_text(
        update,
        f"{ui_language.tr('remote.token.saved')}\n\n"
        f"{render_status(_read_status(root, secrets, _probe(runtime, root, secrets)))}",
    )


async def _start_prompt(runtime: Any, update: Any) -> None:
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
    setattr(runtime, PENDING_ATTRIBUTE, {
        "prompt_message_id": prompt_id,
        "user_id": getattr(getattr(update, "effective_user", None), "id", None),
        "chat_id": getattr(getattr(update, "effective_chat", None), "id", None),
        "expires_at": _now() + PROMPT_TTL_SECONDS,
    })


def _now() -> float:
    import time

    return time.time()


async def run(runtime: Any, update: Any, args: list[str]) -> None:
    """Handle `/remote token ...`; `args` starts after the `token` subcommand."""
    if not _authorized(runtime, update):
        await runtime._reply_text(update, ui_language.tr("remote.token.denied"))
        return
    action = str(args[0]).lower() if args else "status"
    root, secrets = _paths(runtime)

    if action == "status":
        await runtime._reply_text(update, render_status(_read_status(root, secrets)))
        return

    if action == "verify":
        await runtime._reply_text(
            update, render_status(_read_status(root, secrets, _probe(runtime, root, secrets)))
        )
        return

    if action == "clear":
        try:
            remote_token.clear_token(root, secrets_path=secrets)
        except remote_token.RemoteTokenError as error:
            await runtime._reply_text(update, _failure_text(error))
            return
        await runtime._reply_text(
            update,
            f"{ui_language.tr('remote.token.cleared')}\n\n"
            f"{render_status(_read_status(root, secrets, _probe(runtime, root, secrets)))}",
        )
        return

    if action == "cancel":
        _clear_pending(runtime)
        await runtime._reply_text(update, ui_language.tr("remote.token.cancelled"))
        return

    if action == "set":
        if len(args) > 1:
            # A typed credential is audited, echoed and kept in history. The
            # two-step prompt exists precisely so that never happens.
            await runtime._reply_text(update, ui_language.tr("remote.token.inline_removed"))
            return
        await _start_prompt(runtime, update)
        return

    await runtime._reply_text(update, ui_language.tr("remote.token.usage"))


async def handle_pending_reply(runtime: Any, update: Any) -> bool:
    """Consume the reply to a `set` prompt. Returns True when it was handled.

    Runs before any history, queue or agent dispatch, so the credential never
    becomes a normal chat message or an agent task.
    """
    if not is_pending_prompt_reply(runtime, update):
        return False
    pending = getattr(runtime, PENDING_ATTRIBUTE, None)
    message = getattr(update, "message", None)

    if float(pending.get("expires_at") or 0) < _now():
        _clear_pending(runtime)
        await runtime._reply_text(update, ui_language.tr("remote.token.expired"))
        return True

    text = str(getattr(message, "text", "") or "").strip()
    _clear_pending(runtime)
    # Best effort: do not leave the secret sitting in the chat.
    try:
        await message.delete()
    except Exception:
        pass

    if not text or text.lower() in CANCEL_WORDS:
        await runtime._reply_text(update, ui_language.tr("remote.token.cancelled"))
        return True

    await _apply_token(runtime, update, text)
    return True
