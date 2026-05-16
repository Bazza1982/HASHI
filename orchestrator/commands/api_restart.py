from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator.command_registry import RuntimeCallback, RuntimeCommand
from tools import remote_rescue
from remote.security.shared_token import load_shared_token

logger = logging.getLogger("BridgeU.RuntimeCommands.ApiRestart")

WATCHTOWER_INSTANCE = "WATCHTOWER"


def _service_manager(runtime: Any):
    orchestrator = getattr(runtime, "orchestrator", None)
    return getattr(orchestrator, "service_manager", None)


def _authorized(runtime: Any, update: Any) -> bool:
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    return bool(user_id is not None and runtime._is_authorized_user(user_id))


def _gateway_status_text(runtime: Any) -> str:
    service_manager = _service_manager(runtime)
    if service_manager is None:
        return "API Gateway control is unavailable."
    snapshot = service_manager.api_gateway_state_snapshot()
    state_icon = "🟢" if snapshot["running"] else ("🟡" if snapshot["enabled"] else "⚪")
    base_url = snapshot.get("base_url") or "http://127.0.0.1:18801"
    return "\n".join(
        [
            f"{state_icon} <b>API Gateway</b>",
            f"Address: <code>{html.escape(base_url)}</code>",
            f"Chat endpoint: <code>{html.escape(base_url)}/v1/chat/completions</code>",
            f"Models endpoint: <code>{html.escape(base_url)}/v1/models</code>",
            f"Runtime: <code>{'running' if snapshot['running'] else 'stopped'}</code>",
            f"Enabled on restart: <code>{'yes' if snapshot['enabled'] else 'no'}</code>",
            f"Default model: <code>{html.escape(snapshot['default_model'])}</code>",
        ]
    )


def _gateway_status_keyboard(runtime: Any) -> InlineKeyboardMarkup:
    snapshot = _service_manager(runtime).api_gateway_state_snapshot()
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ ON" if snapshot["enabled"] else "ON", callback_data="apigw:on"),
                InlineKeyboardButton("✅ OFF" if not snapshot["enabled"] else "OFF", callback_data="apigw:off"),
            ],
            [
                InlineKeyboardButton("Default Model", callback_data="apigw:menu:model"),
                InlineKeyboardButton("Refresh", callback_data="apigw:refresh"),
            ],
        ]
    )


def _gateway_model_keyboard(runtime: Any) -> InlineKeyboardMarkup:
    snapshot = _service_manager(runtime).api_gateway_state_snapshot()
    current = snapshot["default_model"]
    groups = [
        [model for model in snapshot["available_models"] if model.startswith("gpt-")],
        [model for model in snapshot["available_models"] if model.startswith("claude-")],
        [model for model in snapshot["available_models"] if model.startswith("gemini-")],
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for group in groups:
        if not group:
            continue
        row: list[InlineKeyboardButton] = []
        for model in group[:]:
            label = f"✅ {model}" if model == current else model
            row.append(InlineKeyboardButton(label, callback_data=f"apigw:model:{model}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    rows.append([InlineKeyboardButton("Back", callback_data="apigw:menu:status")])
    return InlineKeyboardMarkup(rows)


async def api_command(runtime: Any, update: Any, context: Any) -> None:
    if not _authorized(runtime, update):
        return
    service_manager = _service_manager(runtime)
    if service_manager is None:
        await runtime._reply_text(update, "API Gateway control is unavailable.")
        return
    args = [str(arg).strip() for arg in (getattr(context, "args", []) or []) if str(arg).strip()]
    action = (args[0].lower() if args else "status")
    if action in {"status", "help"}:
        await runtime._reply_text(update, _gateway_status_text(runtime), parse_mode="HTML", reply_markup=_gateway_status_keyboard(runtime))
        return
    if action == "on":
        ok, message = await service_manager.start_api_gateway_runtime()
        await runtime._reply_text(
            update,
            f"{message}\n\n{_gateway_status_text(runtime)}",
            parse_mode="HTML",
            reply_markup=_gateway_status_keyboard(runtime),
        )
        if not ok:
            return
        return
    if action == "off":
        ok, message = await service_manager.stop_api_gateway_runtime()
        await runtime._reply_text(
            update,
            f"{message}\n\n{_gateway_status_text(runtime)}",
            parse_mode="HTML",
            reply_markup=_gateway_status_keyboard(runtime),
        )
        if not ok:
            return
        return
    if action == "model":
        if len(args) > 1:
            ok, message = service_manager.set_api_gateway_default_model(args[1])
            await runtime._reply_text(
                update,
                f"{message}\n\n{_gateway_status_text(runtime)}",
                parse_mode="HTML",
                reply_markup=_gateway_status_keyboard(runtime),
            )
            return
        await runtime._reply_text(
            update,
            _gateway_status_text(runtime),
            parse_mode="HTML",
            reply_markup=_gateway_model_keyboard(runtime),
        )
        return
    await runtime._reply_text(update, "Usage: /api [status|on|off|model <name>]")


def _restart_auth_kwargs() -> dict[str, str | None]:
    return {
        "shared_token": load_shared_token(remote_rescue.ROOT),
        "from_instance": remote_rescue._default_instance_id(),
    }


def _watchtower_address() -> str:
    try:
        return remote_rescue._candidate_base_urls(WATCHTOWER_INSTANCE)[0]
    except Exception:
        return "unresolved"


def _restart_status_text(payload: dict[str, Any] | None = None, *, error: str | None = None) -> str:
    lines = [
        "🛠️ <b>Hard Restart</b>",
        f"Controller: <code>{WATCHTOWER_INSTANCE}</code>",
        f"WatchTower API: <code>{html.escape(_watchtower_address())}</code>",
    ]
    if error:
        lines.append(f"Status: <code>{html.escape(error)}</code>")
        return "\n".join(lines)
    if payload:
        lines.append(f"Controlled state: <code>{html.escape(str(payload.get('state') or 'unknown'))}</code>")
        workbench_url = payload.get("workbench_url")
        if workbench_url:
            lines.append(f"Controlled workbench: <code>{html.escape(str(workbench_url))}</code>")
        if payload.get("pid"):
            lines.append(f"PID: <code>{int(payload['pid'])}</code>")
    else:
        lines.append("Controlled state: <code>unknown</code>")
    return "\n".join(lines)


def _restart_status_keyboard(confirm: bool = False, *, available: bool = True) -> InlineKeyboardMarkup:
    if confirm:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Confirm Hard Restart", callback_data="hardrestart:confirm"),
                    InlineKeyboardButton("Cancel", callback_data="hardrestart:cancel"),
                ]
            ]
        )
    if not available:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Refresh", callback_data="hardrestart:refresh")]])
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Hard Restart", callback_data="hardrestart:arm"),
                InlineKeyboardButton("Refresh", callback_data="hardrestart:refresh"),
            ]
        ]
    )


async def restart_command(runtime: Any, update: Any, context: Any) -> None:
    if not _authorized(runtime, update):
        return
    restart_available = False
    try:
        code, payload = await asyncio.to_thread(
            remote_rescue.rescue_status,
            WATCHTOWER_INSTANCE,
            **_restart_auth_kwargs(),
        )
        restart_available = code == 0
        text = _restart_status_text(payload, error=None if restart_available else payload.get("error") or "remote error")
    except Exception as exc:
        text = _restart_status_text(error=str(exc))
    await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=_restart_status_keyboard(confirm=False, available=restart_available))


async def _watchtower_restart_available() -> tuple[bool, str | None, dict[str, Any] | None]:
    try:
        code, payload = await asyncio.to_thread(
            remote_rescue.rescue_status,
            WATCHTOWER_INSTANCE,
            **_restart_auth_kwargs(),
        )
    except Exception as exc:
        return False, str(exc), None
    if code != 0:
        return False, payload.get("error") or payload.get("detail") or "WatchTower status check failed", payload
    return True, None, payload


async def _dispatch_watchtower_restart(runtime: Any, chat_id: int) -> None:
    try:
        try:
            code, payload = await asyncio.to_thread(
                remote_rescue.rescue_restart,
                WATCHTOWER_INSTANCE,
                reason="telegram /restart hard restart",
                timeout=15,
                **_restart_auth_kwargs(),
            )
        except Exception as exc:
            logger.warning("WatchTower restart HTTP call failed or timed out: %s", exc)
            await runtime._send_text(chat_id, f"WatchTower restart request failed: {exc}")
            return
        if code != 0:
            detail = payload.get("error") or payload.get("detail") or "remote error"
            logger.warning("WatchTower hard restart rejected: %s", detail)
            await runtime._send_text(chat_id, f"WatchTower restart request failed: {detail}")
    finally:
        setattr(runtime, "_watchtower_restart_inflight", False)


async def api_callback(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    data = query.data or ""
    service_manager = _service_manager(runtime)
    if service_manager is None:
        await query.answer("API Gateway control unavailable.", show_alert=True)
        return
    try:
        if data == "apigw:on":
            await service_manager.start_api_gateway_runtime()
            await query.edit_message_text(_gateway_status_text(runtime), parse_mode="HTML", reply_markup=_gateway_status_keyboard(runtime))
        elif data == "apigw:off":
            await service_manager.stop_api_gateway_runtime()
            await query.edit_message_text(_gateway_status_text(runtime), parse_mode="HTML", reply_markup=_gateway_status_keyboard(runtime))
        elif data == "apigw:refresh" or data == "apigw:menu:status":
            await query.edit_message_text(_gateway_status_text(runtime), parse_mode="HTML", reply_markup=_gateway_status_keyboard(runtime))
        elif data == "apigw:menu:model":
            await query.edit_message_text(_gateway_status_text(runtime), parse_mode="HTML", reply_markup=_gateway_model_keyboard(runtime))
        elif data.startswith("apigw:model:"):
            model = data.split(":", 2)[2]
            ok, message = service_manager.set_api_gateway_default_model(model)
            if not ok:
                await query.answer(message, show_alert=True)
                return
            await query.edit_message_text(
                f"{message}\n\n{_gateway_status_text(runtime)}",
                parse_mode="HTML",
                reply_markup=_gateway_model_keyboard(runtime),
            )
        else:
            await query.answer("Unknown API control.", show_alert=True)
            return
    except Exception as exc:
        logger.exception("API gateway callback failed")
        await query.answer(f"Error: {exc}", show_alert=True)
        return
    await query.answer()


async def restart_callback(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    data = query.data or ""
    if data == "hardrestart:cancel":
        await query.edit_message_text("Hard restart cancelled.")
        await query.answer()
        return
    if data == "hardrestart:arm":
        available, error, _payload = await _watchtower_restart_available()
        if not available:
            await query.edit_message_text(
                _restart_status_text(error=error or "WatchTower unavailable"),
                parse_mode="HTML",
                reply_markup=_restart_status_keyboard(confirm=False, available=False),
            )
            await query.answer("WatchTower unavailable.", show_alert=True)
            return
        await query.edit_message_text(
            "⚠️ <b>Confirm hard restart</b>\nWatchTower will stop this HASHI process, restart it, and verify health.",
            parse_mode="HTML",
            reply_markup=_restart_status_keyboard(confirm=True),
        )
        await query.answer()
        return
    if data == "hardrestart:refresh":
        restart_available = False
        try:
            code, payload = await asyncio.to_thread(
                remote_rescue.rescue_status,
                WATCHTOWER_INSTANCE,
                **_restart_auth_kwargs(),
            )
            restart_available = code == 0
            text = _restart_status_text(payload, error=None if restart_available else payload.get("error") or "remote error")
        except Exception as exc:
            text = _restart_status_text(error=str(exc))
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=_restart_status_keyboard(confirm=False, available=restart_available))
        await query.answer()
        return
    if data == "hardrestart:confirm":
        if getattr(runtime, "_watchtower_restart_inflight", False):
            await query.answer("Restart is already in progress.", show_alert=True)
            return
        available, error, _payload = await _watchtower_restart_available()
        if not available:
            await query.edit_message_text(
                _restart_status_text(error=error or "WatchTower unavailable"),
                parse_mode="HTML",
                reply_markup=_restart_status_keyboard(confirm=False, available=False),
            )
            await query.answer("WatchTower unavailable.", show_alert=True)
            return
        setattr(runtime, "_watchtower_restart_inflight", True)
        await query.edit_message_text(
            "🔁 WatchTower hard restart requested.\nThis bot may go quiet briefly while HASHI stops and comes back.",
            reply_markup=None,
        )
        asyncio.create_task(_dispatch_watchtower_restart(runtime, query.message.chat_id))
        await query.answer("Restart requested.")
        return
    await query.answer("Unknown restart control.", show_alert=True)


COMMANDS = [
    RuntimeCommand(name="api", description="Control API Gateway [on|off|model|status]", callback=api_command),
    RuntimeCommand(name="restart", description="Hard restart via WatchTower", callback=restart_command),
]


CALLBACKS = [
    RuntimeCallback(pattern=r"^apigw:", callback=api_callback),
    RuntimeCallback(pattern=r"^hardrestart:", callback=restart_callback),
]
