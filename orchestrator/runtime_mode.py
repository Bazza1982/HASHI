from __future__ import annotations

from html import escape
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import ui_language
from orchestrator.command_ui import card_title, selected_label
from orchestrator.audit_mode import load_audit_config
from orchestrator.dual_brain_mode import ensure_dual_brain_observer, load_dual_brain_config
from orchestrator.memory_plus_mode import (
    ensure_memory_plus_notepad,
    ensure_memory_plus_observer,
    is_memory_plus_enabled,
    set_memory_plus_enabled,
)
from orchestrator.wrapper_mode import load_wrapper_config


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
            [
                InlineKeyboardButton(selected_label(_mode_label("wrapper"), current == "wrapper"), callback_data="tgl:mode:wrapper"),
                InlineKeyboardButton(selected_label(_mode_label("audit"), current == "audit"), callback_data="tgl:mode:audit"),
            ],
            [
                InlineKeyboardButton(
                    selected_label(_mode_label("dual-brain"), current == "dual-brain"),
                    callback_data="tgl:mode:dual-brain",
                ),
            ],
        ]
    )


async def cmd_mode(runtime: Any, update: Any, context: Any) -> None:
    """Switch between fixed, flex, wrapper, audit, and dual-brain modes."""
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

    if not args or args not in ("fixed", "flex", "wrapper", "audit", "dual-brain"):
        continuity = "ON" if is_memory_plus_enabled(runtime.workspace_dir) else "OFF"
        await runtime._reply_text(
            update,
            f"{card_title('🧭', 'Hashi mode')}\n\n"
            f"<b>{escape(ui_language.tr('common.current'))}</b> · <code>{escape(str(current))}</code>\n"
            f"<b>Memory+</b> · <code>{escape(continuity)}</code> · "
            f"{escape(ui_language.tr('mode.memory_independent'))}\n\n"
            f"<b>{escape(ui_language.tr('mode.choose'))}</b>\n"
            f"{_choice_line('fixed')}\n"
            f"{_choice_line('flex')}\n"
            f"{_choice_line('wrapper')}\n"
            f"{_choice_line('audit')}\n"
            f"{_choice_line('dual-brain')}\n\n"
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

    if hasattr(backend, "set_session_mode"):
        backend.set_session_mode(False)

    if target_mode == "dual-brain":
        current_backend = getattr(runtime.config, "active_backend", "")
        current_model = runtime.get_current_model()
        cfg = load_dual_brain_config(
            runtime.backend_manager.get_state_snapshot(),
            current_backend=current_backend,
            current_model=current_model,
        )
        switch_ok, switch_message = await runtime._activate_wrapper_core_backend(
            update.effective_chat.id,
            backend=cfg.right_backend,
            model=cfg.right_model,
        )
        if not switch_ok:
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "mode.dual_brain.failed",
                    backend=cfg.right_backend,
                    model=cfg.right_model,
                    reason=switch_message,
                ),
                parse_mode="Markdown",
            )
            return
        ensure_dual_brain_observer(runtime.workspace_dir)
        runtime.reload_post_turn_observers()
        runtime.backend_manager.agent_mode = target_mode
        runtime.backend_manager._save_state()
        await runtime._reply_text(
            update,
            ui_language.tr(
                "mode.dual_brain.switched",
                left_backend=cfg.left_backend,
                left_model=cfg.left_model,
                right_backend=cfg.right_backend,
                right_model=cfg.right_model,
                detail=switch_message,
            ),
            parse_mode="Markdown",
        )
        return

    if target_mode == "wrapper":
        cfg = load_wrapper_config(runtime.backend_manager.get_state_snapshot())
        switch_ok, switch_message = await runtime._activate_wrapper_core_backend(
            update.effective_chat.id,
            backend=cfg.core_backend,
            model=cfg.core_model,
        )
        if not switch_ok:
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "mode.wrapper.failed",
                    backend=cfg.core_backend,
                    model=cfg.core_model,
                    reason=switch_message,
                ),
                parse_mode="Markdown",
            )
            return
        runtime.backend_manager.agent_mode = target_mode
        runtime.backend_manager._save_state()
        await runtime._reply_text(
            update,
            ui_language.tr(
                "mode.wrapper.switched",
                core_backend=cfg.core_backend,
                core_model=cfg.core_model,
                wrapper_backend=cfg.wrapper_backend,
                wrapper_model=cfg.wrapper_model,
                core_state=ui_language.tr(
                    "mode.core.ready" if switch_ok else "mode.core.unchanged"
                ),
                detail=switch_message,
            ),
            parse_mode="Markdown",
        )
        return

    cfg = load_audit_config(runtime.backend_manager.get_state_snapshot())
    switch_ok, switch_message = await runtime._activate_wrapper_core_backend(
        update.effective_chat.id,
        backend=cfg.core_backend,
        model=cfg.core_model,
    )
    if not switch_ok:
        await runtime._reply_text(
            update,
            ui_language.tr(
                "mode.audit.failed",
                backend=cfg.core_backend,
                model=cfg.core_model,
                reason=switch_message,
            ),
            parse_mode="Markdown",
        )
        return
    runtime.backend_manager.agent_mode = target_mode
    runtime.backend_manager._save_state()
    await runtime._reply_text(
        update,
        ui_language.tr(
            "mode.audit.switched",
            core_backend=cfg.core_backend,
            core_model=cfg.core_model,
            audit_backend=cfg.audit_backend,
            audit_model=cfg.audit_model,
            delivery=cfg.delivery,
            threshold=cfg.severity_threshold,
            detail=switch_message,
        ),
        parse_mode="Markdown",
    )


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
    if value == current:
        await query.answer(ui_language.tr("mode.callback.already", mode=current))
        return

    backend = runtime.backend_manager.current_backend
    if value == "flex":
        activate_flex_mode(runtime)
    else:
        runtime.backend_manager.agent_mode = value
        runtime.backend_manager._save_state()
    if value == "fixed":
        capabilities = getattr(backend, "capabilities", None)
        if capabilities is not None and not getattr(capabilities, "supports_sessions", False):
            runtime.backend_manager.agent_mode = current
            runtime.backend_manager._save_state()
            await query.answer(
                ui_language.tr("mode.callback.fixed_requires_session"),
                show_alert=True,
            )
            return
        if hasattr(backend, "set_session_mode"):
            backend.set_session_mode(True)
        detail = ui_language.tr("mode.callback.detail.fixed")
    elif value in {"wrapper", "audit", "dual-brain"}:
        if hasattr(backend, "set_session_mode"):
            backend.set_session_mode(False)
        state = runtime.backend_manager.get_state_snapshot()
        if value == "wrapper":
            cfg = load_wrapper_config(state)
            switch_ok, switch_message = await runtime._activate_wrapper_core_backend(
                query.message.chat_id,
                backend=cfg.core_backend,
                model=cfg.core_model,
            )
            if not switch_ok:
                value = _restore_mode_after_failed_switch(runtime, current)
                detail = ui_language.tr(
                    "mode.callback.detail.wrapper_failed", reason=switch_message
                )
            else:
                detail = ui_language.tr(
                    "mode.callback.detail.wrapper", detail=switch_message
                )
        elif value == "audit":
            cfg = load_audit_config(state)
            switch_ok, switch_message = await runtime._activate_wrapper_core_backend(
                query.message.chat_id,
                backend=cfg.core_backend,
                model=cfg.core_model,
            )
            if not switch_ok:
                value = _restore_mode_after_failed_switch(runtime, current)
                detail = ui_language.tr(
                    "mode.callback.detail.audit_failed", reason=switch_message
                )
            else:
                detail = ui_language.tr(
                    "mode.callback.detail.audit", detail=switch_message
                )
        else:
            cfg = load_dual_brain_config(
                state,
                current_backend=getattr(runtime.config, "active_backend", ""),
                current_model=runtime.get_current_model(),
            )
            switch_ok, switch_message = await runtime._activate_wrapper_core_backend(
                query.message.chat_id,
                backend=cfg.right_backend,
                model=cfg.right_model,
            )
            if not switch_ok:
                value = _restore_mode_after_failed_switch(runtime, current)
                detail = ui_language.tr(
                    "mode.callback.detail.dual_failed", reason=switch_message
                )
            else:
                ensure_dual_brain_observer(runtime.workspace_dir)
                runtime.reload_post_turn_observers()
                detail = ui_language.tr(
                    "mode.callback.detail.dual", detail=switch_message
                )
    else:
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


def _restore_mode_after_failed_switch(runtime: Any, mode: str) -> str:
    runtime.backend_manager.agent_mode = mode
    runtime.backend_manager._save_state()
    backend = runtime.backend_manager.current_backend
    if hasattr(backend, "set_session_mode"):
        backend.set_session_mode(
            mode == "fixed"
            and bool(getattr(getattr(backend, "capabilities", None), "supports_sessions", False))
        )
    return mode
