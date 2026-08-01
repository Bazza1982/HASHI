from __future__ import annotations

from html import escape
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator.command_ui import REFRESH_LABEL, card_title, selected_label
from orchestrator.flexible_backend_registry import get_supported_privacy_levels
from orchestrator.privacy_levels import (
    PrivacyLevel,
    PrivacyPolicyError,
    require_backend_compatibility,
    require_level_available,
)


LEVEL_NAMES = {
    PrivacyLevel.OFF: "Privacy Off",
    PrivacyLevel.PROVIDER_TRUST: "Provider Trust",
    PrivacyLevel.BASIC_REDACTION: "Basic Redaction",
}

PLANNED_LEVELS = {
    2: "Basic Redaction — one local PII filter; HASHI-controlled API backends only",
    3: "Strict Redaction — two local filters; online agent harnesses prohibited",
    4: "Private Controlled — verified private or single-tenant deployment only",
    5: "Local Sovereign — models, tools, and data remain inside the local environment",
}


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
        filter_status = "<b>PII filter</b> · <code>OFF</code>"
        risk_title = "Privacy framework bypassed"
        risk_text = (
            "HASHI performs no privacy checks or local redaction. "
            "All configured backends are allowed."
        )
    elif level is PrivacyLevel.PROVIDER_TRUST:
        filter_status = "<b>PII filter</b> · <code>OFF</code>"
        risk_title = "Provider trust mode"
        risk_text = (
            "Raw task context may be sent to the model provider. "
            "Protection depends on that provider's privacy policy."
        )
    else:
        filter_status = "<b>PII filter</b> · <code>ON</code>"
        risk_title = "Local redaction required"
        risk_text = (
            "Every supported API request must pass the local PII gate. "
            "A filter failure blocks transmission."
        )

    lines = [
        card_title("🛡️", "Hashi privacy"),
        "",
        f"<b>Current</b> · <b>LEVEL {int(level)}</b> · {LEVEL_NAMES[level]}",
        f"<b>Backend</b> · <code>{escape(backend)}</code>",
        f"<b>Compatibility</b> · <code>{'SUPPORTED' if compatible else 'BLOCKED'}</code>",
        filter_status,
        "",
        f"⚠️ <b>{risk_title}</b>",
        risk_text,
        "",
        "<b>CHOOSE A PROTECTION LEVEL</b>",
        "0  Off · privacy controls disabled",
        "1  Trust · no local redaction",
        "🔒 2  Basic · one filter · API only",
        "🔒 3  Strict · two filters · no online harness",
        "🔒 4  Private · controlled deployment",
        "🔒 5  Local · nothing leaves the environment",
        "",
        "Available now: <b>Level 0 and Level 1</b>",
        "Default: Level 1 — Provider Trust",
        "Tap below or use <code>/privacy 0</code> / <code>/privacy 1</code>",
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
                        f"⚠️ Confirm Level {confirm_downgrade}",
                        callback_data=f"privacy:confirm:{confirm_downgrade}",
                    ),
                ],
                [InlineKeyboardButton("← Keep current level", callback_data="privacy:menu")],
            ]
        )

    current = current_privacy_level(runtime)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    selected_label("0 · Off", current is PrivacyLevel.OFF),
                    callback_data="privacy:set:0",
                ),
                InlineKeyboardButton(
                    selected_label("1 · Trust", current is PrivacyLevel.PROVIDER_TRUST),
                    callback_data="privacy:set:1",
                ),
            ],
            [
                InlineKeyboardButton("🔒 2 · Basic", callback_data="privacy:planned:2"),
                InlineKeyboardButton("🔒 3 · Strict", callback_data="privacy:planned:3"),
            ],
            [
                InlineKeyboardButton("🔒 4 · Private", callback_data="privacy:planned:4"),
                InlineKeyboardButton("🔒 5 · Local", callback_data="privacy:planned:5"),
            ],
            [InlineKeyboardButton(REFRESH_LABEL, callback_data="privacy:menu")],
        ]
    )


def _busy(runtime: Any) -> bool:
    checker = getattr(runtime, "_backend_busy", None)
    return bool(checker()) if callable(checker) else False


def _set_level(runtime: Any, requested: int, *, confirmed: bool = False) -> str:
    current = current_privacy_level(runtime)
    target = PrivacyLevel(requested)
    if _busy(runtime):
        raise PrivacyPolicyError(
            "Privacy level cannot change while a request is running or queued."
        )
    if target < current and not confirmed:
        raise PrivacyPolicyError("privacy downgrade confirmation required")
    require_level_available(target)
    require_backend_compatibility(runtime.config.active_backend, target)
    runtime.backend_manager.set_privacy_level(target)
    if target is PrivacyLevel.OFF:
        return "Privacy changed to Level 0. Privacy controls are fully OFF."
    if target is PrivacyLevel.PROVIDER_TRUST:
        return "Privacy changed to Level 1. Provider Trust is active; local PII redaction is OFF."
    return "Privacy changed to Level 2. Basic local PII redaction is ON."


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
            "Usage: /privacy | /privacy 0 | /privacy 1 | /privacy 2–5",
            reply_markup=privacy_keyboard(runtime),
        )
        return

    requested = int(args[0])
    if requested >= 3:
        await runtime._reply_text(
            update,
            f"Privacy Level {requested} is reserved in the framework but is not available yet.\n\n"
            f"{PLANNED_LEVELS[requested]}.",
            reply_markup=privacy_keyboard(runtime),
        )
        return
    current = current_privacy_level(runtime)
    if requested < current:
        await runtime._reply_text(
            update,
            f"Lowering from Level {int(current)} to Level {requested} weakens privacy protection. "
            "Confirm this privacy downgrade.",
            reply_markup=privacy_keyboard(runtime, confirm_downgrade=requested),
        )
        return

    try:
        notice = _set_level(runtime, requested)
    except PrivacyPolicyError as exc:
        await runtime._reply_text(
            update,
            f"Privacy setting not changed: {exc}",
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
        await query.answer("Invalid privacy action.", show_alert=True)
        return
    requested = int(parts[2])
    if parts[1] == "planned" or requested >= 3:
        description = PLANNED_LEVELS.get(
            requested,
            "This protection level is not available until its enforcement is installed and verified",
        )
        await query.answer(
            f"Level {requested} is not available yet. {description}.",
            show_alert=True,
        )
        return
    confirmed = parts[1] == "confirm"
    current = current_privacy_level(runtime)
    if requested < current and not confirmed:
        await query.edit_message_text(
            f"Lowering from Level {int(current)} to Level {requested} weakens privacy protection. "
            "Confirm this privacy downgrade.",
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
    await query.answer(f"Privacy Level {requested}")
