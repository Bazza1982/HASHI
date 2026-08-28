from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import ui_language
from orchestrator.command_ui import back_label, card_title, refresh_label, selected_label
from orchestrator.command_registry import RuntimeCallback, RuntimeCommand
from orchestrator.human_restart import build_human_restart_proof, human_restart_secret_path, load_human_restart_secret
from tools import remote_rescue
from remote.security.shared_token import load_shared_token

logger = logging.getLogger("BridgeU.RuntimeCommands.ApiRestart")

WATCHTOWER_INSTANCE = "WATCHTOWER"
ALLOWED_HUMAN_RESTART_SOURCES = {"telegram", "whatsapp", "tui"}


def _instance_id(runtime: Any) -> str:
    global_config = getattr(runtime, "global_config", None)
    if global_config is None:
        global_config = getattr(getattr(runtime, "orchestrator", None), "global_cfg", None)
    return str(getattr(global_config, "instance_id", None) or "HASHI")


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
        return ui_language.tr("api.control_unavailable")
    snapshot = service_manager.api_gateway_state_snapshot()
    state_icon = "🟢" if snapshot["running"] else ("🟡" if snapshot["enabled"] else "⚪")
    base_url = snapshot.get("base_url") or "http://127.0.0.1:18801"
    return "\n".join(
        [
            card_title("🔌", "Hashi API gateway"),
            "",
            f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
            f"<b>{html.escape(ui_language.tr('common.on' if snapshot['running'] else 'common.off'))}</b> · {state_icon}",
            f"<b>{html.escape(ui_language.tr('api.starts_after_reboot'))}</b> · "
            f"<code>{html.escape(ui_language.tr('api.yes' if snapshot['enabled'] else 'api.no'))}</code>",
            f"<b>{html.escape(ui_language.tr('api.default_model'))}</b> · "
            f"<code>{html.escape(snapshot['default_model'])}</code>",
            "",
            f"<b>{html.escape(ui_language.tr('api.address'))}</b> · <code>{html.escape(base_url)}</code>",
            f"{html.escape(ui_language.tr('api.endpoint.chat'))} · <code>{html.escape(base_url)}/v1/chat/completions</code>",
            f"{html.escape(ui_language.tr('api.endpoint.images'))} · <code>{html.escape(base_url)}/v1/images/generations</code>",
            f"{html.escape(ui_language.tr('api.endpoint.videos'))} · <code>{html.escape(base_url)}/v1/videos/generations</code>",
            f"{html.escape(ui_language.tr('api.endpoint.models'))} · <code>{html.escape(base_url)}/v1/models</code>",
        ]
    )


def _gateway_status_keyboard(runtime: Any) -> InlineKeyboardMarkup:
    snapshot = _service_manager(runtime).api_gateway_state_snapshot()
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(selected_label(ui_language.tr("api.button.on"), snapshot["enabled"]), callback_data="apigw:on"),
                InlineKeyboardButton(selected_label(ui_language.tr("api.button.off"), not snapshot["enabled"]), callback_data="apigw:off"),
            ],
            [
                InlineKeyboardButton(ui_language.tr("api.button.default_model"), callback_data="apigw:menu:model"),
                InlineKeyboardButton(refresh_label(), callback_data="apigw:refresh"),
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
        [model for model in snapshot["available_models"] if model.startswith("grok-")],
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for group in groups:
        if not group:
            continue
        row: list[InlineKeyboardButton] = []
        for model in group[:]:
            label = selected_label(model, model == current)
            row.append(InlineKeyboardButton(label, callback_data=f"apigw:model:{model}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    rows.append([InlineKeyboardButton(back_label(), callback_data="apigw:menu:status")])
    return InlineKeyboardMarkup(rows)


async def api_command(runtime: Any, update: Any, context: Any) -> None:
    if not _authorized(runtime, update):
        return
    service_manager = _service_manager(runtime)
    if service_manager is None:
        await runtime._reply_text(update, ui_language.tr("api.control_unavailable"))
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
    await runtime._reply_text(
        update,
        ui_language.tr("api.usage"),
        parse_mode="HTML",
    )


def _restart_auth_kwargs() -> dict[str, str | None]:
    return {
        "shared_token": load_shared_token(remote_rescue.ROOT),
        "from_instance": remote_rescue._default_instance_id(),
    }


def _restart_bridge_home(runtime: Any):
    orchestrator = getattr(runtime, "orchestrator", None)
    paths = getattr(orchestrator, "paths", None)
    bridge_home = getattr(paths, "bridge_home", None)
    return bridge_home or remote_rescue.ROOT


def _build_watchtower_restart_payload(runtime: Any, *, human_source: str, reason: str) -> dict[str, Any]:
    source = str(human_source or "").strip().lower()
    if source not in ALLOWED_HUMAN_RESTART_SOURCES:
        raise ValueError(f"unsupported human restart source: {human_source}")
    notify_agent = str(getattr(runtime, "name", "") or "").strip().lower()
    if not notify_agent:
        raise ValueError("runtime name is required for restart notification")
    bridge_home = _restart_bridge_home(runtime)
    secret = load_human_restart_secret(bridge_home, remote_rescue.ROOT)
    if not secret:
        secret_path = human_restart_secret_path(bridge_home)
        raise RuntimeError(
            "human restart secret is not configured; set HASHI_HUMAN_RESTART_SECRET "
            f"or create {secret_path} with the same value configured in WatchTower"
        )
    requester = str(remote_rescue._default_instance_id() or "").strip().upper()
    proof = build_human_restart_proof(
        secret,
        requester=requester,
        reason=reason,
        human_source=source,
        notify_agent=notify_agent,
    )
    return {
        "reason": reason,
        "human_source": source,
        "notify_agent": notify_agent,
        "notify_via": "telegram",
        "human_restart_proof": proof,
    }


def _watchtower_address() -> str:
    try:
        return remote_rescue._candidate_base_urls(WATCHTOWER_INSTANCE)[0]
    except Exception:
        return "unresolved"


def _restart_status_text(payload: dict[str, Any] | None = None, *, error: str | None = None) -> str:
    lines = [
        card_title("🛠️", "Hard restart"),
        "",
        f"<b>{html.escape(ui_language.tr('common.current'))}</b> · {ui_language.tr('restart.current')}",
        f"{ui_language.tr('restart.controller')}: <code>{WATCHTOWER_INSTANCE}</code>",
        f"{ui_language.tr('restart.watchtower_api')}: <code>{html.escape(_watchtower_address())}</code>",
    ]
    if error:
        lines.append(f"Status: <code>{html.escape(error)}</code>")
        return "\n".join(lines)
    if payload:
        lines.append(
            f"{ui_language.tr('restart.controlled_state')}: "
            f"<code>{html.escape(str(payload.get('state') or ui_language.tr('common.unknown')))}</code>"
        )
        workbench_url = payload.get("workbench_url")
        if workbench_url:
            lines.append(
                f"{ui_language.tr('restart.controlled_workbench')}: "
                f"<code>{html.escape(str(workbench_url))}</code>"
            )
        if payload.get("pid"):
            lines.append(f"PID: <code>{int(payload['pid'])}</code>")
    else:
        lines.append(
            f"{ui_language.tr('restart.controlled_state')}: "
            f"<code>{html.escape(ui_language.tr('common.unknown'))}</code>"
        )
    return "\n".join(lines)


def _restart_status_keyboard(confirm: bool = False, *, available: bool = True) -> InlineKeyboardMarkup:
    if confirm:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(ui_language.tr("restart.button.restart"), callback_data="hardrestart:confirm")],
                [InlineKeyboardButton(ui_language.tr("restart.button.keep"), callback_data="hardrestart:cancel")],
            ]
        )
    if not available:
        return InlineKeyboardMarkup([[InlineKeyboardButton(refresh_label(), callback_data="hardrestart:refresh")]])
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(ui_language.tr("restart.button.hard"), callback_data="hardrestart:arm"),
                InlineKeyboardButton(refresh_label(), callback_data="hardrestart:refresh"),
            ]
        ]
    )


async def restart_command(runtime: Any, update: Any, context: Any) -> None:
    if not _authorized(runtime, update):
        return
    if getattr(runtime, "_watchtower_restart_inflight", False):
        await runtime._reply_text(update, ui_language.tr("api.restart.in_progress"))
        return
    available, error, _payload = await _watchtower_restart_available()
    if not available:
        await runtime._reply_text(
            update,
            _restart_status_text(
                error=error or ui_language.tr("api.restart.watchtower_unavailable")
            ),
            parse_mode="HTML",
        )
        return
    try:
        request_payload = _build_watchtower_restart_payload(
            runtime,
            human_source="telegram",
            reason="telegram /restart hard restart",
        )
    except Exception as exc:
        logger.warning("Failed to build Telegram restart payload: %s", exc)
        await runtime._reply_text(
            update,
            ui_language.tr(
                "api.restart.setup_error", reason=html.escape(str(exc))
            ),
            parse_mode="HTML",
        )
        return
    setattr(runtime, "_watchtower_restart_inflight", True)
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    await runtime._reply_text(
        update,
        ui_language.tr("api.restart.requested"),
    )
    asyncio.create_task(_dispatch_watchtower_restart(runtime, chat_id, request_payload))


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


async def _dispatch_watchtower_restart(runtime: Any, chat_id: int | None, request_payload: dict[str, Any]) -> None:
    try:
        try:
            code, payload = await asyncio.to_thread(
                remote_rescue.rescue_restart,
                WATCHTOWER_INSTANCE,
                reason=request_payload.get("reason"),
                extra_payload=request_payload,
                timeout=15,
                **_restart_auth_kwargs(),
            )
        except Exception as exc:
            logger.warning("WatchTower restart HTTP call failed or timed out: %s", exc)
            if chat_id is not None:
                await runtime._send_text(
                    chat_id,
                    ui_language.tr("api.restart.failed", reason=str(exc)),
                )
            return
        if code != 0:
            detail = payload.get("error") or payload.get("detail") or "remote error"
            logger.warning("WatchTower hard restart rejected: %s", detail)
            if chat_id is not None:
                await runtime._send_text(
                    chat_id,
                    ui_language.tr("api.restart.failed", reason=str(detail)),
                )
    finally:
        setattr(runtime, "_watchtower_restart_inflight", False)


async def api_callback(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        await query.answer(ui_language.tr("api.restart.not_authorized"), show_alert=True)
        return
    data = query.data or ""
    service_manager = _service_manager(runtime)
    if service_manager is None:
        await query.answer(ui_language.tr("api.control_unavailable"), show_alert=True)
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
            await query.answer(ui_language.tr("api.control_unknown"), show_alert=True)
            return
    except Exception as exc:
        logger.exception("API gateway callback failed")
        await query.answer(
            ui_language.tr("model.callback_error", reason=str(exc)),
            show_alert=True,
        )
        return
    await query.answer()


async def restart_callback(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        await query.answer(ui_language.tr("api.restart.not_authorized"), show_alert=True)
        return
    data = query.data or ""
    if data == "hardrestart:cancel":
        await query.edit_message_text(ui_language.tr("api.restart.cancelled"))
        await query.answer()
        return
    if data == "hardrestart:arm":
        available, error, _payload = await _watchtower_restart_available()
        if not available:
            await query.edit_message_text(
                _restart_status_text(
                    error=error
                    or ui_language.tr("api.restart.watchtower_unavailable")
                ),
                parse_mode="HTML",
                reply_markup=_restart_status_keyboard(confirm=False, available=False),
            )
            await query.answer(
                ui_language.tr("api.restart.watchtower_unavailable"),
                show_alert=True,
            )
            return
        instance_id = html.escape(_instance_id(runtime))
        await query.edit_message_text(
            f"{card_title('⚠️', 'Confirm hard restart')}\n\n"
            f"<b>{html.escape(ui_language.tr('api.restart.confirm_target'))}</b> · <code>{instance_id}</code>\n\n"
            f"{ui_language.tr('api.restart.confirm_effect', instance=instance_id)}\n\n"
            f"{ui_language.tr('api.restart.confirm_action', instance=instance_id)}",
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
            text = _restart_status_text(
                payload,
                error=(
                    None
                    if restart_available
                    else payload.get("error")
                    or ui_language.tr("api.restart.remote_error")
                ),
            )
        except Exception as exc:
            text = _restart_status_text(error=str(exc))
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=_restart_status_keyboard(confirm=False, available=restart_available))
        await query.answer()
        return
    if data == "hardrestart:confirm":
        if getattr(runtime, "_watchtower_restart_inflight", False):
            await query.answer(
                ui_language.tr("api.restart.in_progress"), show_alert=True
            )
            return
        available, error, _payload = await _watchtower_restart_available()
        if not available:
            await query.edit_message_text(
                _restart_status_text(
                    error=error
                    or ui_language.tr("api.restart.watchtower_unavailable")
                ),
                parse_mode="HTML",
                reply_markup=_restart_status_keyboard(confirm=False, available=False),
            )
            await query.answer(
                ui_language.tr("api.restart.watchtower_unavailable"),
                show_alert=True,
            )
            return
        try:
            request_payload = _build_watchtower_restart_payload(
                runtime,
                human_source="telegram",
                reason="telegram /restart hard restart",
            )
        except Exception as exc:
            logger.warning("Failed to build Telegram restart payload: %s", exc)
            await query.edit_message_text(
                ui_language.tr(
                    "api.restart.setup_error", reason=html.escape(str(exc))
                ),
                parse_mode="HTML",
                reply_markup=_restart_status_keyboard(confirm=False, available=True),
            )
            await query.answer(
                ui_language.tr("api.restart.setup_incomplete"), show_alert=True
            )
            return
        setattr(runtime, "_watchtower_restart_inflight", True)
        await query.edit_message_text(
            ui_language.tr("api.restart.requested"),
            reply_markup=None,
        )
        chat_id = getattr(getattr(query, "message", None), "chat_id", None)
        asyncio.create_task(_dispatch_watchtower_restart(runtime, chat_id, request_payload))
        await query.answer(ui_language.tr("api.restart.requested_short"))
        return
    await query.answer(ui_language.tr("api.restart.unknown"), show_alert=True)


async def request_whatsapp_restart(runtime: Any, *, reason: str = "whatsapp /restart hard restart") -> tuple[bool, str]:
    try:
        request_payload = _build_watchtower_restart_payload(runtime, human_source="whatsapp", reason=reason)
    except Exception as exc:
        logger.warning("Failed to build WhatsApp restart payload: %s", exc)
        return False, str(exc)
    try:
        code, payload = await asyncio.to_thread(
            remote_rescue.rescue_restart,
            WATCHTOWER_INSTANCE,
            reason=request_payload.get("reason"),
            extra_payload=request_payload,
            timeout=15,
            **_restart_auth_kwargs(),
        )
    except Exception as exc:
        logger.warning("WatchTower WhatsApp restart HTTP call failed or timed out: %s", exc)
        return False, str(exc)
    if code != 0:
        detail = payload.get("error") or payload.get("detail") or "remote error"
        logger.warning("WatchTower WhatsApp hard restart rejected: %s", detail)
        return False, str(detail)
    return True, str(payload.get("restart_id") or "restart requested")


COMMANDS = [
    RuntimeCommand(name="api", description="Control API Gateway [on|off|model|status]", callback=api_command),
    RuntimeCommand(name="restart", description="Hard restart via WatchTower", callback=restart_command),
]


CALLBACKS = [
    RuntimeCallback(pattern=r"^apigw:", callback=api_callback),
    RuntimeCallback(pattern=r"^hardrestart:", callback=restart_callback),
]
