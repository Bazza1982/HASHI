from __future__ import annotations

import html
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator.api_gateway_config import (
    available_api_models,
    load_api_gateway_config,
    normalize_api_model,
    save_api_gateway_config,
)
from orchestrator.command_registry import RuntimeCallback, RuntimeCommand


USAGE = "Usage: /api [on|off|model <model>]"


def _is_authorized(runtime: Any, update: Any) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    if callable(checker):
        return bool(checker(user_id))
    global_config = getattr(runtime, "global_config", None)
    authorized_id = getattr(global_config, "authorized_id", None)
    return authorized_id is None or user_id == authorized_id


def _updated_by(update: Any) -> str:
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    return f"telegram:{user_id}" if user_id is not None else "telegram:unknown"


def _service_manager(runtime: Any):
    orchestrator = getattr(runtime, "orchestrator", None)
    return getattr(orchestrator, "service_manager", None) if orchestrator is not None else None


def _status(runtime: Any) -> dict[str, Any]:
    manager = _service_manager(runtime)
    if manager is not None and hasattr(manager, "api_gateway_status"):
        return manager.api_gateway_status()
    global_config = getattr(runtime, "global_config", None)
    return {
        "running": False,
        "enabled_flag": False,
        "bind_host": None,
        "port": getattr(global_config, "api_gateway_port", None) if global_config is not None else None,
    }


def _api_address(runtime: Any) -> str:
    global_config = getattr(runtime, "global_config", None)
    status = _status(runtime)
    host = status.get("bind_host") or getattr(global_config, "api_host", None) or "127.0.0.1"
    port = status.get("port") or getattr(global_config, "api_gateway_port", None) or 18801
    return f"http://{host}:{port}"


def _keyboard(runtime: Any) -> InlineKeyboardMarkup:
    running = bool(_status(runtime).get("running"))
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🟢 On" if running else "Turn On", callback_data="api:on"),
                InlineKeyboardButton("🔴 Off" if not running else "Turn Off", callback_data="api:off"),
            ],
            [InlineKeyboardButton("Default Model", callback_data="api:model")],
            [InlineKeyboardButton("Refresh", callback_data="api:status")],
        ]
    )


def _model_keyboard(runtime: Any) -> InlineKeyboardMarkup:
    current = load_api_gateway_config(runtime.global_config)["default_model"]
    rows: list[list[InlineKeyboardButton]] = []
    for model in available_api_models():
        label = f">> {model}" if model == current else model
        rows.append([InlineKeyboardButton(label, callback_data=f"api:model:{model}")])
    rows.append([InlineKeyboardButton("Back", callback_data="api:status")])
    return InlineKeyboardMarkup(rows)


def _status_text(runtime: Any, *, prefix: str = "") -> str:
    cfg = load_api_gateway_config(runtime.global_config)
    status = _status(runtime)
    running = bool(status.get("running"))
    configured = bool(cfg.get("enabled"))
    address = _api_address(runtime)
    lines = []
    if prefix:
        lines.append(html.escape(prefix))
        lines.append("")
    lines.extend(
        [
            "<b>Hashi API Gateway</b>",
            f"Status: <b>{'ON' if running else 'OFF'}</b>",
            f"Configured switch: <code>{'on' if configured else 'off'}</code>",
            f"Address: <code>{html.escape(address)}</code>",
            f"Models: <code>{html.escape(address)}/v1/models</code>",
            f"Chat: <code>{html.escape(address)}/v1/chat/completions</code>",
            f"Images: <code>{html.escape(address)}/v1/images/generations</code>",
            f"Videos: <code>{html.escape(address)}/v1/videos/generations</code>",
            f"Default API model: <code>{html.escape(cfg['default_model'])}</code>",
            "",
            "API callers may override this default by sending a request-level <code>model</code>.",
        ]
    )
    return "\n".join(lines)


async def _send(runtime: Any, update: Any, text: str, *, reply_markup=None) -> None:
    if hasattr(runtime, "_reply_text") and getattr(update, "message", None) is not None:
        await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=reply_markup)
        return
    message = getattr(update, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)


async def api_command(runtime: Any, update: Any, context: Any) -> None:
    if not _is_authorized(runtime, update):
        return
    args = [str(arg).strip() for arg in (getattr(context, "args", []) or []) if str(arg).strip()]
    sub = args[0].lower() if args else "status"
    if sub in {"help", "-h", "--help"}:
        await _send(runtime, update, html.escape(USAGE), reply_markup=_keyboard(runtime))
        return

    if sub == "on":
        cfg = save_api_gateway_config(runtime.global_config, enabled=True, updated_by=_updated_by(update))
        manager = _service_manager(runtime)
        ok, message = (False, "Runtime service manager is unavailable.")
        if manager is not None:
            ok, message = await manager.set_api_gateway_enabled(True)
        await _send(
            runtime,
            update,
            _status_text(runtime, prefix=message if ok else f"Failed: {message}"),
            reply_markup=_keyboard(runtime),
        )
        return

    if sub == "off":
        save_api_gateway_config(runtime.global_config, enabled=False, updated_by=_updated_by(update))
        manager = _service_manager(runtime)
        ok, message = (False, "Runtime service manager is unavailable.")
        if manager is not None:
            ok, message = await manager.set_api_gateway_enabled(False)
        await _send(
            runtime,
            update,
            _status_text(runtime, prefix=message if ok else f"Failed: {message}"),
            reply_markup=_keyboard(runtime),
        )
        return

    if sub == "model":
        if len(args) >= 2:
            model = normalize_api_model(args[1])
            if model is None:
                await _send(runtime, update, f"Unknown API model: <code>{html.escape(args[1])}</code>", reply_markup=_model_keyboard(runtime))
                return
            save_api_gateway_config(runtime.global_config, default_model=model, updated_by=_updated_by(update))
            await _send(runtime, update, _status_text(runtime, prefix=f"Default API model set to: {model}"), reply_markup=_keyboard(runtime))
            return
        await _send(runtime, update, "Select default API model:", reply_markup=_model_keyboard(runtime))
        return

    if sub not in {"status", "show"}:
        await _send(runtime, update, html.escape(USAGE), reply_markup=_keyboard(runtime))
        return

    await _send(runtime, update, _status_text(runtime), reply_markup=_keyboard(runtime))


async def api_callback(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not _is_authorized(runtime, update):
        await query.answer()
        return
    data = str(getattr(query, "data", "") or "")
    answered = False
    try:
        if data == "api:on":
            save_api_gateway_config(runtime.global_config, enabled=True, updated_by=_updated_by(update))
            manager = _service_manager(runtime)
            ok, message = (False, "Runtime service manager is unavailable.")
            if manager is not None:
                ok, message = await manager.set_api_gateway_enabled(True)
            await query.edit_message_text(_status_text(runtime, prefix=message if ok else f"Failed: {message}"), parse_mode="HTML", reply_markup=_keyboard(runtime))
        elif data == "api:off":
            save_api_gateway_config(runtime.global_config, enabled=False, updated_by=_updated_by(update))
            manager = _service_manager(runtime)
            ok, message = (False, "Runtime service manager is unavailable.")
            if manager is not None:
                ok, message = await manager.set_api_gateway_enabled(False)
            await query.edit_message_text(_status_text(runtime, prefix=message if ok else f"Failed: {message}"), parse_mode="HTML", reply_markup=_keyboard(runtime))
        elif data == "api:model":
            await query.edit_message_text("Select default API model:", reply_markup=_model_keyboard(runtime))
        elif data.startswith("api:model:"):
            model = data.split(":", 2)[2]
            normalized = normalize_api_model(model)
            if normalized is None:
                await query.answer(f"Unknown API model: {model}", show_alert=True)
                answered = True
                return
            save_api_gateway_config(runtime.global_config, default_model=normalized, updated_by=_updated_by(update))
            await query.edit_message_text(_status_text(runtime, prefix=f"Default API model set to: {normalized}"), parse_mode="HTML", reply_markup=_keyboard(runtime))
        else:
            await query.edit_message_text(_status_text(runtime), parse_mode="HTML", reply_markup=_keyboard(runtime))
    finally:
        if not answered:
            await query.answer()


COMMANDS = [
    RuntimeCommand(
        name="api",
        description="Control API gateway on/off/default model",
        callback=api_command,
    )
]

CALLBACKS = [
    RuntimeCallback(pattern=r"^api:", callback=api_callback),
]
