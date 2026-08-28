from __future__ import annotations

from html import escape
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import ui_language
from orchestrator.command_ui import card_title, refresh_label, selected_label
from orchestrator.flexible_backend_registry import get_supported_privacy_levels
from orchestrator.privacy_levels import (
    PrivacyLevel,
    PrivacyPolicyError,
)


LEVEL_NAME_KEYS = {
    PrivacyLevel.OFF: "privacy.level.off",
    PrivacyLevel.PROVIDER_TRUST: "privacy.level.trust",
    PrivacyLevel.BASIC_REDACTION: "privacy.level.basic",
}


def _planned_level(level: int) -> str:
    return ui_language.tr(f"privacy.planned.{level}")


def current_privacy_level(runtime: Any) -> PrivacyLevel:
    value = getattr(runtime.backend_manager, "privacy_level", PrivacyLevel.PROVIDER_TRUST)
    try:
        return PrivacyLevel(int(value))
    except (TypeError, ValueError):
        return PrivacyLevel.PROVIDER_TRUST


def privacy_status_text(runtime: Any, *, notice: str | None = None) -> str:
    level = current_privacy_level(runtime)
    backend = str(getattr(runtime.config, "active_backend", "") or "unknown")
    compatible = int(level) in get_supported_privacy_levels(backend)
    if level is PrivacyLevel.OFF:
        filter_enabled = False
        risk_title = ui_language.tr("privacy.risk.off.title")
        risk_text = ui_language.tr("privacy.risk.off.text")
    elif level is PrivacyLevel.PROVIDER_TRUST:
        filter_enabled = False
        risk_title = ui_language.tr("privacy.risk.trust.title")
        risk_text = ui_language.tr("privacy.risk.trust.text")
    else:
        filter_enabled = True
        risk_title = ui_language.tr("privacy.risk.local.title")
        risk_text = ui_language.tr("privacy.risk.local.text")
    filter_status = (
        f"<b>{escape(ui_language.tr('privacy.filter'))}</b> · "
        f"<code>{escape(ui_language.tr('common.on' if filter_enabled else 'common.off'))}</code>"
    )
    compatibility = ui_language.tr(
        "privacy.compatibility.supported"
        if compatible
        else "privacy.compatibility.blocked"
    )
    level_name = ui_language.tr(LEVEL_NAME_KEYS[level])

    lines = [
        card_title("🛡️", "Hashi privacy"),
        "",
        f"<b>{escape(ui_language.tr('common.current'))}</b> · "
        f"<b>{escape(ui_language.tr('privacy.current_level', level=int(level)))}</b> · "
        f"{escape(level_name)}",
        f"<b>{escape(ui_language.tr('common.backend'))}</b> · <code>{escape(backend)}</code>",
        f"<b>{escape(ui_language.tr('common.compatibility'))}</b> · "
        f"<code>{escape(compatibility)}</code>",
        filter_status,
        "",
        f"⚠️ <b>{risk_title}</b>",
        risk_text,
        "",
        f"<b>{escape(ui_language.tr('privacy.choose'))}</b>",
        ui_language.tr("privacy.choice.0"),
        ui_language.tr("privacy.choice.1"),
        ui_language.tr("privacy.choice.2"),
        ui_language.tr("privacy.choice.3"),
        ui_language.tr("privacy.choice.4"),
        ui_language.tr("privacy.choice.5"),
        "",
        ui_language.tr("privacy.available_now"),
        ui_language.tr("privacy.default"),
        ui_language.tr("privacy.action"),
    ]
    if notice:
        lines = [f"✨ <b>{escape(notice)}</b>", "", *lines]
    return "\n".join(lines)


def privacy_keyboard(
    runtime: Any,
    *,
    confirm_downgrade: int | None = None,
) -> InlineKeyboardMarkup:
    if confirm_downgrade is not None:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        ui_language.tr(
                            "privacy.button.confirm", level=confirm_downgrade
                        ),
                        callback_data=f"privacy:confirm:{confirm_downgrade}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        ui_language.tr("privacy.button.keep"),
                        callback_data="privacy:menu",
                    )
                ],
            ]
        )

    current = current_privacy_level(runtime)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    selected_label(
                        ui_language.tr("privacy.button.off"),
                        current is PrivacyLevel.OFF,
                    ),
                    callback_data="privacy:set:0",
                ),
                InlineKeyboardButton(
                    selected_label(
                        ui_language.tr("privacy.button.trust"),
                        current is PrivacyLevel.PROVIDER_TRUST,
                    ),
                    callback_data="privacy:set:1",
                ),
            ],
            [
                InlineKeyboardButton(ui_language.tr("privacy.button.basic"), callback_data="privacy:planned:2"),
                InlineKeyboardButton(ui_language.tr("privacy.button.strict"), callback_data="privacy:planned:3"),
            ],
            [
                InlineKeyboardButton(ui_language.tr("privacy.button.private"), callback_data="privacy:planned:4"),
                InlineKeyboardButton(ui_language.tr("privacy.button.local"), callback_data="privacy:planned:5"),
            ],
            [InlineKeyboardButton(refresh_label(), callback_data="privacy:menu")],
        ]
    )


def _busy(runtime: Any) -> bool:
    checker = getattr(runtime, "_backend_busy", None)
    return bool(checker()) if callable(checker) else False


def _set_level(runtime: Any, requested: int, *, confirmed: bool = False) -> str:
    current = current_privacy_level(runtime)
    target = PrivacyLevel(requested)
    if _busy(runtime):
        raise PrivacyPolicyError(ui_language.tr("privacy.error.busy"))
    if target < current and not confirmed:
        raise PrivacyPolicyError(ui_language.tr("privacy.error.confirmation"))
    if target not in {PrivacyLevel.OFF, PrivacyLevel.PROVIDER_TRUST}:
        raise PrivacyPolicyError(
            ui_language.tr(
                "privacy.error.level_unavailable", level=int(target)
            )
        )
    supported = get_supported_privacy_levels(runtime.config.active_backend)
    if int(target) not in supported:
        raise PrivacyPolicyError(
            ui_language.tr(
                "privacy.error.backend",
                backend=runtime.config.active_backend,
                level=int(target),
                supported=", ".join(str(item) for item in supported),
            )
        )
    runtime.backend_manager.set_privacy_level(target)
    if target is PrivacyLevel.OFF:
        return ui_language.tr("privacy.changed.0")
    if target is PrivacyLevel.PROVIDER_TRUST:
        return ui_language.tr("privacy.changed.1")
    return ui_language.tr("privacy.changed.2")


async def cmd_privacy(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    args = [str(arg).strip() for arg in (context.args or []) if str(arg).strip()]
    if not args or args[0].lower() in {"status", "menu", "show"}:
        await runtime._reply_text(
            update,
            privacy_status_text(runtime),
            reply_markup=privacy_keyboard(runtime),
            parse_mode="HTML",
        )
        return
    if len(args) != 1 or args[0] not in {"0", "1", "2", "3", "4", "5"}:
        await runtime._reply_text(
            update,
            ui_language.tr("privacy.usage"),
            reply_markup=privacy_keyboard(runtime),
        )
        return

    requested = int(args[0])
    if requested >= 3:
        await runtime._reply_text(
            update,
            ui_language.tr(
                "privacy.reserved",
                level=requested,
                description=_planned_level(requested),
            ),
            reply_markup=privacy_keyboard(runtime),
        )
        return
    current = current_privacy_level(runtime)
    if requested < current:
        await runtime._reply_text(
            update,
            ui_language.tr(
                "privacy.downgrade",
                current=int(current),
                requested=requested,
            ),
            reply_markup=privacy_keyboard(runtime, confirm_downgrade=requested),
        )
        return

    try:
        notice = _set_level(runtime, requested)
    except PrivacyPolicyError as exc:
        await runtime._reply_text(
            update,
            ui_language.tr("privacy.not_changed", reason=exc),
            reply_markup=privacy_keyboard(runtime),
        )
        return
    await runtime._reply_text(
        update,
        privacy_status_text(runtime, notice=notice),
        reply_markup=privacy_keyboard(runtime),
        parse_mode="HTML",
    )


async def callback_privacy(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        await query.answer()
        return
    data = str(query.data or "")
    if data == "privacy:menu":
        await query.edit_message_text(
            privacy_status_text(runtime),
            reply_markup=privacy_keyboard(runtime),
            parse_mode="HTML",
        )
        await query.answer()
        return

    parts = data.split(":")
    if (
        len(parts) != 3
        or parts[1] not in {"set", "confirm", "planned"}
        or parts[2] not in {"0", "1", "2", "3", "4", "5"}
    ):
        await query.answer(
            ui_language.tr("privacy.invalid_action"), show_alert=True
        )
        return
    requested = int(parts[2])
    if parts[1] == "planned" or requested >= 3:
        description = (
            _planned_level(requested)
            if requested in {2, 3, 4, 5}
            else ui_language.tr("privacy.not_available_default")
        )
        await query.answer(
            ui_language.tr(
                "privacy.not_available",
                level=requested,
                description=description,
            ),
            show_alert=True,
        )
        return
    confirmed = parts[1] == "confirm"
    current = current_privacy_level(runtime)
    if requested < current and not confirmed:
        await query.edit_message_text(
            ui_language.tr(
                "privacy.downgrade",
                current=int(current),
                requested=requested,
            ),
            reply_markup=privacy_keyboard(runtime, confirm_downgrade=requested),
        )
        await query.answer()
        return
    try:
        notice = _set_level(runtime, requested, confirmed=confirmed)
    except PrivacyPolicyError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await query.edit_message_text(
        privacy_status_text(runtime, notice=notice),
        reply_markup=privacy_keyboard(runtime),
        parse_mode="HTML",
    )
    await query.answer(
        ui_language.tr("privacy.answer.level", level=requested)
    )
