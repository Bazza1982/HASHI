from __future__ import annotations

from html import escape
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import ui_language
from orchestrator.command_ui import card_title, selected_label
from orchestrator.config import RETIRED_AGENT_MODES, SUPPORTED_AGENT_MODES
from orchestrator.memory_plus_mode import (
    ensure_memory_plus_notepad,
    ensure_memory_plus_observer,
    is_memory_plus_enabled,
    set_memory_plus_enabled,
)


def _mode_key(mode: str) -> str:
    return "dual_brain" if mode == "dual-brain" else mode.replace("+", "_plus")


def _mode_label(mode: str) -> str:
    key = f"mode.label.{_mode_key(mode)}"
    translated = ui_language.tr(key)
    return mode if translated == key else translated


def _choice_line(mode: str) -> str:
    label = _mode_label(mode)
    description = ui_language.tr(f"mode.description.{_mode_key(mode)}")
    if ui_language.current_locale() == ui_language.DEFAULT_LOCALE:
        name = mode
    else:
        name = f"{escape(label)} <code>{escape(mode)}</code>"
    return f"• <b>{name}</b> — {escape(description)}"


def activate_flex_mode(runtime: Any) -> str:
    """Persist Flex mode and apply its session behavior without rendering UI."""
    previous = runtime.backend_manager.agent_mode
    if previous == "memory+":
        set_memory_plus_enabled(runtime.workspace_dir, True)
    runtime.backend_manager.agent_mode = "flex"
    runtime.backend_manager._save_state()
    backend = runtime.backend_manager.current_backend
    if hasattr(backend, "set_session_mode"):
        backend.set_session_mode(False)
    return previous


def mode_keyboard(current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(selected_label(_mode_label("fixed"), current == "fixed"), callback_data="tgl:mode:fixed"),
                InlineKeyboardButton(selected_label(_mode_label("flex"), current == "flex"), callback_data="tgl:mode:flex"),
            ],
        ]
    )


async def cmd_mode(runtime: Any, update: Any, context: Any) -> None:
    """Switch between the supported Fixed and Flex working modes."""
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    args = (context.args[0].lower() if context.args else "").strip()
    if args in {"dualbrain", "brain"}:
        args = "dual-brain"
    if args in {"memoryplus", "memory-plus", "mem+", "notepad"}:
        args = "memory+"
    current = runtime.backend_manager.agent_mode

    if args == "memory+":
        set_memory_plus_enabled(runtime.workspace_dir, True)
        ensure_memory_plus_observer(runtime.workspace_dir)
        ensure_memory_plus_notepad(runtime.workspace_dir)
        runtime.reload_post_turn_observers()
        await runtime._reply_text(
            update,
            ui_language.tr("mode.memory.enabled", mode=current),
            parse_mode="Markdown",
        )
        return

    if args in RETIRED_AGENT_MODES:
        await runtime._reply_text(update, ui_language.tr("mode.retired"))
        return

    if not args or args not in SUPPORTED_AGENT_MODES:
        continuity = "ON" if is_memory_plus_enabled(runtime.workspace_dir) else "OFF"
        await runtime._reply_text(
            update,
            f"{card_title('🧭', 'Hashi mode')}\n\n"
            f"<b>{escape(ui_language.tr('common.current'))}</b> · <code>{escape(str(current))}</code>\n"
            f"<b>Memory+</b> · <code>{escape(continuity)}</code> · "
            f"{escape(ui_language.tr('mode.memory_independent'))}\n\n"
            f"<b>{escape(ui_language.tr('mode.choose'))}</b>\n"
            f"{_choice_line('fixed')}\n"
            f"{_choice_line('flex')}\n\n"
            f"{ui_language.tr('mode.effect')}",
            parse_mode="HTML",
            reply_markup=mode_keyboard(current),
        )
        return

    if args == current:
        await runtime._reply_text(
            update,
            ui_language.tr("mode.already", mode=current),
            parse_mode="Markdown",
        )
        return

    await switch_mode_from_command(runtime, update, args)


async def switch_mode_from_command(runtime: Any, update: Any, target_mode: str) -> None:
    if target_mode not in SUPPORTED_AGENT_MODES:
        await runtime._reply_text(update, ui_language.tr("mode.retired"))
        return

    backend = runtime.backend_manager.current_backend
    if target_mode == "fixed":
        capabilities = getattr(backend, "capabilities", None)
        if capabilities is not None and not getattr(capabilities, "supports_sessions", False):
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "mode.fixed.requires_session",
                    backend=runtime.config.active_backend,
                ),
                parse_mode="Markdown",
            )
            return
        runtime.backend_manager.agent_mode = target_mode
        runtime.backend_manager._save_state()
        if hasattr(backend, "set_session_mode"):
            backend.set_session_mode(True)
        await runtime._reply_text(
            update,
            ui_language.tr("mode.fixed.switched"),
            parse_mode="Markdown",
        )
        return

    if target_mode == "flex":
        activate_flex_mode(runtime)
        await runtime._reply_text(
            update,
            ui_language.tr("mode.flex.switched"),
            parse_mode="Markdown",
        )
        return


async def callback_mode_toggle(runtime: Any, query: Any, value: str) -> None:
    current = runtime.backend_manager.agent_mode
    if value == "memory+":
        set_memory_plus_enabled(runtime.workspace_dir, True)
        ensure_memory_plus_observer(runtime.workspace_dir)
        ensure_memory_plus_notepad(runtime.workspace_dir)
        runtime.reload_post_turn_observers()
        await query.edit_message_text(
            ui_language.tr("mode.callback.memory", mode=escape(str(current))),
            parse_mode="HTML",
            reply_markup=mode_keyboard(current),
        )
        await query.answer(ui_language.tr("mode.callback.memory_enabled"))
        return
    if value not in SUPPORTED_AGENT_MODES:
        await query.answer(ui_language.tr("mode.retired"), show_alert=True)
        return
    if value == current:
        await query.answer(ui_language.tr("mode.callback.already", mode=current))
        return

    backend = runtime.backend_manager.current_backend
    if value == "fixed":
        capabilities = getattr(backend, "capabilities", None)
        if capabilities is not None and not getattr(capabilities, "supports_sessions", False):
            await query.answer(
                ui_language.tr("mode.callback.fixed_requires_session"),
                show_alert=True,
            )
            return
        runtime.backend_manager.agent_mode = value
        runtime.backend_manager._save_state()
        if hasattr(backend, "set_session_mode"):
            backend.set_session_mode(True)
        detail = ui_language.tr("mode.callback.detail.fixed")
    else:
        activate_flex_mode(runtime)
        detail = ui_language.tr("mode.callback.detail.flex")

    await query.edit_message_text(
        ui_language.tr(
            "mode.callback.result",
            mode=escape(str(value)),
            detail=detail,
        ),
        parse_mode="HTML",
        reply_markup=mode_keyboard(value),
    )
    await query.answer(ui_language.tr("mode.callback.switched", mode=value))
