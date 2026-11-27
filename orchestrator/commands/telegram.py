from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import ui_language, runtime_session
from orchestrator.connector_delivery_preferences import (
    get_connector_preference,
    set_connector_preference,
)
from orchestrator.command_registry import RuntimeCallback, RuntimeCommand
from orchestrator.command_ui import selected_label, setting_card, status_label

def _is_authorized(runtime: Any, update: Any) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    if callable(checker):
        return bool(checker(user_id))
    global_config = getattr(runtime, "global_config", None)
    authorized_id = getattr(global_config, "authorized_id", None)
    return authorized_id is None or user_id == authorized_id


def _bridge_home(runtime: Any) -> Any:
    global_config = getattr(runtime, "global_config", None)
    return getattr(global_config, "bridge_home", None)


def _owner_id(runtime: Any, update: Any) -> str:
    owner = getattr(update, "_hashi_session_owner_id", None)
    return runtime_session.owner_id(runtime, str(owner).strip() if owner else None)


def _enabled(runtime: Any, update: Any, connector_id: str = "telegram") -> bool:
    home = _bridge_home(runtime)
    if home is None:
        return connector_id == "telegram"
    return get_connector_preference(
        home, _owner_id(runtime, update), connector_id, "mirror",
        default=connector_id == "telegram",
    )


def _global_state(runtime: Any) -> bool | None:
    """Read the instance-wide mirror switch, or None without a store."""

    home = _bridge_home(runtime)
    if home is None:
        return None
    from orchestrator.connector_delivery_preferences import (
        instance_telegram_mirror_enabled,
    )

    return instance_telegram_mirror_enabled(home, default=True)


def _global_state_label(enabled: bool) -> str:
    return ui_language.tr(
        "menu.telegram.global.state.on" if enabled
        else "menu.telegram.global.state.off"
    )


def _instance_agent_counts(runtime: Any) -> tuple[int, int] | None:
    """Count this instance's Agents and those pinned to always on."""

    config_path = getattr(getattr(runtime, "global_config", None), "config_path", None)
    if not config_path:
        return None
    try:
        from orchestrator.config_json import read_config_json

        document = read_config_json(config_path)
        rows = document.get("agents") if isinstance(document, Mapping) else None
        if not isinstance(rows, list):
            return None
        agents = [row for row in rows if isinstance(row, Mapping)]
        always_on = sum(
            1
            for row in agents
            if str(row.get("telegram_mirror_policy") or "").strip().casefold()
            == "always_on"
        )
        return len(agents), always_on
    except (OSError, TypeError, ValueError):
        return None


def _global_scope_line(runtime: Any) -> str:
    counts = _instance_agent_counts(runtime)
    if counts is None:
        return ui_language.tr("menu.telegram.global.scope_unknown")
    agents, always_on = counts
    return ui_language.tr(
        "menu.telegram.global.scope", agents=agents, always_on=always_on
    )


def _global_status_text(runtime: Any) -> str:
    current = _global_state(runtime)
    if current is None:
        return ui_language.tr("menu.telegram.unavailable")
    return "\n\n".join(
        [
            ui_language.tr(
                "menu.telegram.global.status", state=_global_state_label(current)
            ),
            _global_scope_line(runtime),
            ui_language.tr("menu.telegram.global.unaffected"),
            ui_language.tr("menu.telegram.global.usage"),
        ]
    )


async def _global_mirror_command(runtime: Any, update: Any, *, enabled: bool) -> None:
    """Set the instance-wide switch that overrides every Agent's mirror policy."""

    home = _bridge_home(runtime)
    if home is None:
        await _send(runtime, update, ui_language.tr("menu.telegram.unavailable"))
        return
    from orchestrator.connector_delivery_preferences import set_instance_preference

    try:
        set_instance_preference(home, "telegram", "mirror", enabled)
    except (OSError, TypeError, ValueError):
        await _send(runtime, update, ui_language.tr("menu.telegram.global.failed"))
        return
    notice = ui_language.tr(
        "menu.telegram.global.notice.on" if enabled
        else "menu.telegram.global.notice.off"
    )
    await _send(
        runtime,
        update,
        "\n\n".join(
            [
                f"✅ {notice}",
                ui_language.tr(
                    "menu.telegram.global.status",
                    state=_global_state_label(enabled),
                ),
                _global_scope_line(runtime),
                ui_language.tr("menu.telegram.global.unaffected"),
                ui_language.tr(
                    "menu.telegram.global.restore_on" if not enabled
                    else "menu.telegram.global.restore_off"
                ),
            ]
        ),
    )


def _menu_text(runtime: Any, update: Any, *, connector_id: str = "telegram", notice: str | None = None,
               summary: dict[str, Any] | None = None) -> str:
    enabled = _enabled(runtime, update, connector_id)
    prefix = f"menu.{connector_id}"
    facts = [
        f"<b>{ui_language.tr('common.scope')}</b> · "
        f"{ui_language.tr(prefix + '.scope')}"
    ]
    if connector_id == "telegram":
        global_state = _global_state(runtime)
        if global_state is not None:
            facts.append(
                ui_language.tr(
                    "menu.telegram.global.menu_fact",
                    state=_global_state_label(global_state),
                )
            )
        from orchestrator.telegram_status_presentation import summary_facts
        facts.extend(summary_facts(summary))
    if notice:
        facts.insert(0, f"✅ {notice}")
    return setting_card(
        "📡",
        ui_language.tr(prefix + ".title"),
        current=f"<b>{status_label(enabled)}</b>",
        facts=facts,
        consequence=(
            ui_language.tr(prefix + ".enabled")
            if enabled
            else ui_language.tr(prefix + ".disabled")
        ),
        action=ui_language.tr(prefix + ".action"),
    )


def _keyboard(runtime: Any, update: Any, connector_id: str = "telegram") -> InlineKeyboardMarkup:
    enabled = _enabled(runtime, update, connector_id)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    selected_label(ui_language.tr("common.on"), enabled),
                    callback_data=f"{connector_id}:set:on",
                ),
                InlineKeyboardButton(
                    selected_label(ui_language.tr("common.off"), not enabled),
                    callback_data=f"{connector_id}:set:off",
                ),
            ]
        ]
    )


async def _send(runtime: Any, update: Any, text: str, *, reply_markup=None) -> None:
    if hasattr(runtime, "_reply_text"):
        await runtime._reply_text(
            update, text, parse_mode="HTML", reply_markup=reply_markup
        )
        return
    message = getattr(update, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is not None and hasattr(runtime, "send_long_message"):
        await runtime.send_long_message(
            chat_id, text, request_id="telegram-command", purpose="command"
        )


async def _summary(runtime: Any, update: Any, connector_id: str) -> dict[str, Any] | None:
    if connector_id != "telegram":
        return None
    actor = getattr(getattr(update, "effective_user", None), "id", None)
    if type(actor) is not int or actor <= 0:
        return {"ok": False}
    from orchestrator.telegram_status_presentation import read_summary
    return await read_summary(runtime, _owner_id(runtime, update), actor_id=actor)


async def _mirror_command(runtime: Any, update: Any, context: Any, connector_id: str) -> None:
    if not _is_authorized(runtime, update):
        return
    home = _bridge_home(runtime)
    if home is None:
        await _send(runtime, update, ui_language.tr(f"menu.{connector_id}.unavailable"))
        return
    args = [
        str(arg).strip().lower()
        for arg in (getattr(context, "args", None) or [])
        if str(arg).strip()
    ]
    if not args:
        await _send(
            runtime,
            update,
            _menu_text(runtime, update, connector_id=connector_id,
                       summary=await _summary(runtime, update, connector_id)),
            reply_markup=_keyboard(runtime, update, connector_id),
        )
        return
    value = args[0]
    if len(args) == 1 and value in {"on", "off"}:
        set_connector_preference(home, _owner_id(runtime, update), connector_id, "mirror", value == "on")
        await _send(
            runtime,
            update,
            _menu_text(runtime, update, connector_id=connector_id,
                       notice=ui_language.tr(f"menu.{connector_id}.notice.{value}"),
                       summary=await _summary(runtime, update, connector_id)),
            reply_markup=_keyboard(runtime, update, connector_id),
        )
        return
    await _send(runtime, update, ui_language.tr(f"menu.{connector_id}.usage"))


async def _disconnect_command(runtime: Any, update: Any, args: list[str]) -> None:
    """Remove the target Agent's saved Bot token locally and stop its connection.

    The removal is a local deletion: Telegram is never contacted, so an
    invalid or rejected token disconnects exactly like a valid one. The token
    key in the configuration stays, so the Agent can be reconfigured later.
    """
    from orchestrator.telegram_credential import (
        TelegramCredentialError,
        TelegramCredentialService,
    )

    global_config = getattr(runtime, "global_config", None)
    config_path = getattr(global_config, "config_path", None)
    secrets_path = getattr(global_config, "secrets_path", None)
    if not config_path or not secrets_path:
        await _send(
            runtime, update, ui_language.tr("menu.telegram.disconnect.invalid_agent")
        )
        return
    service = TelegramCredentialService(
        config_path=config_path,
        secrets_path=secrets_path,
        state_root=getattr(global_config, "bridge_home", None),
    )
    target = (
        str(args[1]).strip().lower()
        if len(args) > 1
        else str(getattr(runtime, "name", "") or "").strip().lower()
    )
    if not target:
        await _send(
            runtime, update, ui_language.tr("menu.telegram.disconnect.invalid_agent")
        )
        return
    try:
        context = service.context(target)
    except TelegramCredentialError as exc:
        if exc.error_code in {
            "agent_not_found",
            "telegram_credential_config_invalid",
            "invalid_agent_id",
        }:
            await _send(
                runtime, update, ui_language.tr("menu.telegram.disconnect.invalid_agent")
            )
        else:
            await _send(runtime, update, ui_language.tr("menu.telegram.disconnect.failed"))
        return
    if not context.configured:
        await _send(
            runtime, update, ui_language.tr("menu.telegram.disconnect.not_configured")
        )
        return
    canonical = context.agent_id
    try:
        service.disconnect(canonical, expected_revision=context.credential_revision)
    except TelegramCredentialError as exc:
        if exc.error_code == "telegram_credential_shared":
            await _send(runtime, update, ui_language.tr("menu.telegram.disconnect.shared"))
        else:
            await _send(runtime, update, ui_language.tr("menu.telegram.disconnect.failed"))
        return
    kernel_notice = ""
    try:
        orchestrator = getattr(runtime, "orchestrator", None)
        request = getattr(orchestrator, "request_telegram_disconnect", None)
        if callable(request):
            ok, _message = await request(canonical, owner_id=_owner_id(runtime, update))
            if not ok:
                kernel_notice = ui_language.tr("menu.telegram.disconnect.kernel_notice")
    except Exception:
        kernel_notice = ui_language.tr("menu.telegram.disconnect.kernel_notice")
    lines = [f"✅ {ui_language.tr('menu.telegram.disconnect.notice')}"]
    if kernel_notice:
        lines.append(f"⚠️ {kernel_notice}")
    lines.append(ui_language.tr("menu.telegram.disconnect.platform_note"))
    await _send(runtime, update, "\n\n".join(lines))


async def telegram_command(runtime: Any, update: Any, context: Any) -> None:
    if not _is_authorized(runtime, update):
        return
    args = [
        str(arg).strip().lower()
        for arg in (getattr(context, "args", None) or [])
        if str(arg).strip()
    ]
    if args and args[0] == "disconnect":
        await _disconnect_command(runtime, update, args)
        return
    if len(args) == 2 and args[1] == "global" and args[0] in {"on", "off"}:
        await _global_mirror_command(runtime, update, enabled=args[0] == "on")
        return
    if args == ["global"]:
        await _send(runtime, update, _global_status_text(runtime))
        return
    await _mirror_command(runtime, update, context, "telegram")


async def whatsapp_command(runtime: Any, update: Any, context: Any) -> None:
    await _mirror_command(runtime, update, context, "whatsapp")


async def telegram_callback(runtime: Any, update: Any, context: Any) -> None:
    await _mirror_callback(runtime, update, context, "telegram")


async def whatsapp_callback(runtime: Any, update: Any, context: Any) -> None:
    await _mirror_callback(runtime, update, context, "whatsapp")


async def _mirror_callback(runtime: Any, update: Any, context: Any, connector_id: str) -> None:
    query = update.callback_query
    if not _is_authorized(runtime, update):
        await query.answer()
        return
    home = _bridge_home(runtime)
    if home is None:
        await query.answer(ui_language.tr(f"menu.{connector_id}.unavailable"), show_alert=True)
        return
    data = query.data or ""
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else ""
    notice = None
    if action == "set" and value in {"on", "off"}:
        set_connector_preference(
            home, _owner_id(runtime, update), connector_id, "mirror", value == "on"
        )
        notice = ui_language.tr(
            f"menu.{connector_id}.notice.{value}"
        )
    await query.edit_message_text(
        _menu_text(runtime, update, connector_id=connector_id, notice=notice,
                   summary=await _summary(runtime, update, connector_id)),
        parse_mode="HTML",
        reply_markup=_keyboard(runtime, update, connector_id),
    )
    await query.answer()


COMMANDS = [
    RuntimeCommand(
        name="telegram",
        description=(
            "Toggle the Telegram mirror for this owner, for the whole instance, "
            "or disconnect a Bot token "
            "[on|off [global]|disconnect [agent_id]|global]"
        ),
        callback=telegram_command,
    ),
    RuntimeCommand(
        name="whatsapp",
        description="Toggle WhatsApp mirror for other connectors [on|off]",
        callback=whatsapp_command,
    ),
]

CALLBACKS = [
    RuntimeCallback(pattern=r"^telegram:", callback=telegram_callback),
    RuntimeCallback(pattern=r"^whatsapp:", callback=whatsapp_callback),
]
# HASHI-Fingerprint: hfp_6c618b6d6f614e72876c5eb444a9ab88
