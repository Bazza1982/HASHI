from __future__ import annotations

import html
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import ui_language
from orchestrator.command_ui import back_label, card_title, refresh_label, selected_label
from orchestrator.api_gateway_config import (
    available_api_models,
    load_api_gateway_config,
    normalize_api_model,
    save_api_gateway_config,
)
from orchestrator.command_registry import RuntimeCallback


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
                InlineKeyboardButton(
                    selected_label(ui_language.tr("api.button.on"), running),
                    callback_data="api:on",
                ),
                InlineKeyboardButton(
                    selected_label(ui_language.tr("api.button.off"), not running),
                    callback_data="api:off",
                ),
            ],
            [InlineKeyboardButton(ui_language.tr("api.button.default_model"), callback_data="api:model")],
            [InlineKeyboardButton(refresh_label(), callback_data="api:status")],
        ]
    )


def _model_keyboard(runtime: Any) -> InlineKeyboardMarkup:
    current = load_api_gateway_config(runtime.global_config)["default_model"]
    rows: list[list[InlineKeyboardButton]] = []
    for model in available_api_models():
        label = selected_label(model, model == current)
        rows.append([InlineKeyboardButton(label, callback_data=f"api:model:{model}")])
    rows.append([InlineKeyboardButton(back_label(), callback_data="api:status")])
    return InlineKeyboardMarkup(rows)


def _model_text(runtime: Any) -> str:
    current = load_api_gateway_config(runtime.global_config)["default_model"]
    return (
        f"{card_title('🔌', 'API default model')}\n\n"
        f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
        f"<code>{html.escape(current)}</code>\n\n"
        f"{ui_language.tr('api.model_effect')}\n\n"
        f"{ui_language.tr('api.model_action')}"
    )


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
            card_title("🔌", "Hashi API gateway"),
            "",
            f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
            f"<b>{html.escape(ui_language.tr('common.on' if running else 'common.off'))}</b>",
            f"<b>{html.escape(ui_language.tr('api.starts_after_reboot'))}</b> · "
            f"<code>{html.escape(ui_language.tr('api.yes' if configured else 'api.no'))}</code>",
            f"<b>{html.escape(ui_language.tr('api.default_model'))}</b> · "
            f"<code>{html.escape(cfg['default_model'])}</code>",
            "",
            f"<b>{html.escape(ui_language.tr('api.address'))}</b> · <code>{html.escape(address)}</code>",
            f"{html.escape(ui_language.tr('api.endpoint.models'))} · <code>{html.escape(address)}/v1/models</code>",
            f"{html.escape(ui_language.tr('api.endpoint.chat'))} · <code>{html.escape(address)}/v1/chat/completions</code>",
            f"{html.escape(ui_language.tr('api.endpoint.images'))} · <code>{html.escape(address)}/v1/images/generations</code>",
            f"{html.escape(ui_language.tr('api.endpoint.videos'))} · <code>{html.escape(address)}/v1/videos/generations</code>",
            "",
            ui_language.tr("api.override_effect"),
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
        await _send(runtime, update, ui_language.tr("api.usage"), reply_markup=_keyboard(runtime))
        return

    if sub == "on":
        save_api_gateway_config(runtime.global_config, enabled=True, updated_by=_updated_by(update))
        manager = _service_manager(runtime)
        ok, message = (False, ui_language.tr("api.runtime_manager_unavailable"))
        if manager is not None:
            ok, message = await manager.set_api_gateway_enabled(True)
        prefix = (
            ui_language.tr("api.enabled")
            if ok
            else ui_language.tr("api.failed", reason=message)
        )
        await _send(
            runtime,
            update,
            _status_text(runtime, prefix=prefix),
            reply_markup=_keyboard(runtime),
        )
        return

    if sub == "off":
        save_api_gateway_config(runtime.global_config, enabled=False, updated_by=_updated_by(update))
        manager = _service_manager(runtime)
        ok, message = (False, ui_language.tr("api.runtime_manager_unavailable"))
        if manager is not None:
            ok, message = await manager.set_api_gateway_enabled(False)
        prefix = (
            ui_language.tr("api.disabled")
            if ok
            else ui_language.tr("api.failed", reason=message)
        )
        await _send(
            runtime,
            update,
            _status_text(runtime, prefix=prefix),
            reply_markup=_keyboard(runtime),
        )
        return

    if sub == "model":
        if len(args) >= 2:
            model = normalize_api_model(args[1])
            if model is None:
                await _send(
                    runtime,
                    update,
                    ui_language.tr(
                        "api.unknown_model",
                        model=f"<code>{html.escape(args[1])}</code>",
                    ),
                    reply_markup=_model_keyboard(runtime),
                )
                return
            save_api_gateway_config(runtime.global_config, default_model=model, updated_by=_updated_by(update))
            await _send(
                runtime,
                update,
                _status_text(
                    runtime,
                    prefix=ui_language.tr("api.model_set", model=model),
                ),
                reply_markup=_keyboard(runtime),
            )
            return
        await _send(runtime, update, _model_text(runtime), reply_markup=_model_keyboard(runtime))
        return

    if sub not in {"status", "show"}:
        await _send(runtime, update, ui_language.tr("api.usage"), reply_markup=_keyboard(runtime))
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
            ok, message = (False, ui_language.tr("api.runtime_manager_unavailable"))
            if manager is not None:
                ok, message = await manager.set_api_gateway_enabled(True)
            prefix = (
                ui_language.tr("api.enabled")
                if ok
                else ui_language.tr("api.failed", reason=message)
            )
            await query.edit_message_text(
                _status_text(runtime, prefix=prefix),
                parse_mode="HTML",
                reply_markup=_keyboard(runtime),
            )
        elif data == "api:off":
            save_api_gateway_config(runtime.global_config, enabled=False, updated_by=_updated_by(update))
            manager = _service_manager(runtime)
            ok, message = (False, ui_language.tr("api.runtime_manager_unavailable"))
            if manager is not None:
                ok, message = await manager.set_api_gateway_enabled(False)
            prefix = (
                ui_language.tr("api.disabled")
                if ok
                else ui_language.tr("api.failed", reason=message)
            )
            await query.edit_message_text(
                _status_text(runtime, prefix=prefix),
                parse_mode="HTML",
                reply_markup=_keyboard(runtime),
            )
        elif data == "api:model":
            await query.edit_message_text(
                _model_text(runtime),
                parse_mode="HTML",
                reply_markup=_model_keyboard(runtime),
            )
        elif data.startswith("api:model:"):
            model = data.split(":", 2)[2]
            normalized = normalize_api_model(model)
            if normalized is None:
                await query.answer(
                    ui_language.tr("api.unknown_model", model=model), show_alert=True
                )
                answered = True
                return
            save_api_gateway_config(runtime.global_config, default_model=normalized, updated_by=_updated_by(update))
            await query.edit_message_text(
                _status_text(
                    runtime,
                    prefix=ui_language.tr("api.model_set", model=normalized),
                ),
                parse_mode="HTML",
                reply_markup=_keyboard(runtime),
            )
        else:
            await query.edit_message_text(_status_text(runtime), parse_mode="HTML", reply_markup=_keyboard(runtime))
    finally:
        if not answered:
            await query.answer()


# The canonical /api command lives in api_restart.py alongside /restart. Keep
# this legacy callback registered so buttons in older Telegram messages that
# use the former ``api:`` callback prefix continue to work.
COMMANDS = []

CALLBACKS = [
    RuntimeCallback(pattern=r"^api:", callback=api_callback),
]
