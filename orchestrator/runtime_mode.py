from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator.audit_mode import load_audit_config
from orchestrator.dual_brain_mode import ensure_dual_brain_observer, load_dual_brain_config
from orchestrator.memory_plus_mode import ensure_memory_plus_notepad, ensure_memory_plus_observer
from orchestrator.wrapper_mode import load_wrapper_config


def mode_keyboard(current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Fixed" if current == "fixed" else "Fixed", callback_data="tgl:mode:fixed"),
                InlineKeyboardButton("✅ Flex" if current == "flex" else "Flex", callback_data="tgl:mode:flex"),
            ],
            [
                InlineKeyboardButton("✅ Wrapper" if current == "wrapper" else "Wrapper", callback_data="tgl:mode:wrapper"),
                InlineKeyboardButton("✅ Audit" if current == "audit" else "Audit", callback_data="tgl:mode:audit"),
            ],
            [
                InlineKeyboardButton("✅ Dual Brain" if current == "dual-brain" else "Dual Brain", callback_data="tgl:mode:dual-brain"),
                InlineKeyboardButton("✅ Memory+" if current == "memory+" else "Memory+", callback_data="tgl:mode:memory+"),
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

    if not args or args not in ("fixed", "flex", "wrapper", "audit", "dual-brain", "memory+"):
        await runtime._reply_text(
            update,
            f"Current mode: <b>{current}</b>\n\n"
            f"• <b>fixed</b> — continuous CLI session, incremental prompts\n"
            f"• <b>flex</b> — multi-backend switching, full context injection\n"
            f"• <b>wrapper</b> — configure core/wrapper model pair with /core and /wrap\n"
            f"• <b>audit</b> — configure core/audit model pair with /core and /audit\n"
            f"• <b>dual-brain</b> — left-brain memory preflight + right-brain execution; configure with /brain\n"
            f"• <b>memory+</b> — one-model daily continuity notepad; enable fully with /reboot",
            parse_mode="HTML",
            reply_markup=mode_keyboard(current),
        )
        return

    if args == current:
        await runtime._reply_text(update, f"Already in **{current}** mode.", parse_mode="Markdown")
        return

    await switch_mode_from_command(runtime, update, args)


async def switch_mode_from_command(runtime: Any, update: Any, target_mode: str) -> None:
    backend = runtime.backend_manager.current_backend
    if target_mode == "fixed":
        runtime.backend_manager.agent_mode = target_mode
        runtime.backend_manager._save_state()
        if hasattr(backend, "set_session_mode"):
            backend.set_session_mode(True)
        await runtime._reply_text(
            update,
            "Switched to **fixed** mode.\n"
            "• CLI session will persist across messages\n"
            "• Bridge sends incremental prompts (no history re-injection)\n"
            "• `/backend` is disabled; use `/mode flex` to re-enable\n"
            "• `/new` will terminate the current session and start fresh",
            parse_mode="Markdown",
        )
        return

    if target_mode == "memory+":
        runtime.backend_manager.agent_mode = target_mode
        runtime.backend_manager._save_state()
        ensure_memory_plus_observer(runtime.workspace_dir)
        ensure_memory_plus_notepad(runtime.workspace_dir)
        if hasattr(backend, "set_session_mode"):
            backend.set_session_mode(True)
        await runtime._reply_text(
            update,
            "Memory+ mode saved.\n"
            "• One brain reads the daily notepad, answers, then writes a hidden memory update\n"
            "• No second LLM query is used\n"
            "• Run `/reboot` to fully enable the notepad provider for this runtime",
            parse_mode="Markdown",
        )
        return

    if target_mode == "flex":
        runtime.backend_manager.agent_mode = target_mode
        runtime.backend_manager._save_state()
        if hasattr(backend, "set_session_mode"):
            backend.set_session_mode(False)
        await runtime._reply_text(
            update,
            "Switched to **flex** mode.\n"
            "• Full context injection per request\n"
            "• `/backend` switching re-enabled",
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
                "Dual-brain mode was not activated.\n"
                f"• Right brain: `{cfg.right_backend}` / `{cfg.right_model}`\n"
                f"• Reason: {switch_message}",
                parse_mode="Markdown",
            )
            return
        ensure_dual_brain_observer(runtime.workspace_dir)
        runtime.reload_post_turn_observers()
        runtime.backend_manager.agent_mode = target_mode
        runtime.backend_manager._save_state()
        await runtime._reply_text(
            update,
            "Switched to **dual-brain** mode.\n"
            f"• Left brain: `{cfg.left_backend}` / `{cfg.left_model}`\n"
            f"• Right brain: `{cfg.right_backend}` / `{cfg.right_model}`\n"
            f"{switch_message}\n"
            "• Use `/brain` to configure left/right models and custom prompts\n"
            "• Active `/sys` slots remain part of the right-brain runtime prompt",
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
                "Wrapper mode was not activated.\n"
                f"• Core: `{cfg.core_backend}` / `{cfg.core_model}`\n"
                f"• Reason: {switch_message}",
                parse_mode="Markdown",
            )
            return
        runtime.backend_manager.agent_mode = target_mode
        runtime.backend_manager._save_state()
        await runtime._reply_text(
            update,
            "Switched to **wrapper** mode.\n"
            f"• Core: `{cfg.core_backend}` / `{cfg.core_model}`\n"
            f"• Wrapper: `{cfg.wrapper_backend}` / `{cfg.wrapper_model}`\n"
            f"• Active core: {'ready' if switch_ok else 'not changed'}\n"
            f"{switch_message}\n"
            "• Use `/core`, `/wrap`, and `/wrapper` to configure\n"
            "• User-visible responses are rewritten through the wrapper model",
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
            "Audit mode was not activated.\n"
            f"• Core: `{cfg.core_backend}` / `{cfg.core_model}`\n"
            f"• Reason: {switch_message}",
            parse_mode="Markdown",
        )
        return
    runtime.backend_manager.agent_mode = target_mode
    runtime.backend_manager._save_state()
    await runtime._reply_text(
        update,
        "Switched to **audit** mode.\n"
        f"• Core: `{cfg.core_backend}` / `{cfg.core_model}`\n"
        f"• Audit: `{cfg.audit_backend}` / `{cfg.audit_model}`\n"
        f"• Delivery: `{cfg.delivery}`\n"
        f"• Threshold: `{cfg.severity_threshold}`\n"
        f"{switch_message}\n"
        "• Use `/core` and `/audit` to configure\n"
        "• Core responses are delivered unchanged; audit findings follow separately",
        parse_mode="Markdown",
    )


async def callback_mode_toggle(runtime: Any, query: Any, value: str) -> None:
    current = runtime.backend_manager.agent_mode
    if value == current:
        await query.answer(f"Already in {current} mode.")
        return

    runtime.backend_manager.agent_mode = value
    runtime.backend_manager._save_state()
    backend = runtime.backend_manager.current_backend
    if value == "fixed":
        if hasattr(backend, "set_session_mode"):
            backend.set_session_mode(True)
        detail = "CLI session persists · /backend disabled"
    elif value == "memory+":
        ensure_memory_plus_observer(runtime.workspace_dir)
        ensure_memory_plus_notepad(runtime.workspace_dir)
        if hasattr(backend, "set_session_mode"):
            backend.set_session_mode(True)
        detail = "One-brain daily notepad memory · run /reboot to fully enable"
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
            detail = (
                "Core/wrapper mode · use /core and /wrap · "
                f"{'core ready' if switch_ok else 'core unchanged'} ({switch_message})"
            )
        elif value == "audit":
            cfg = load_audit_config(state)
            switch_ok, switch_message = await runtime._activate_wrapper_core_backend(
                query.message.chat_id,
                backend=cfg.core_backend,
                model=cfg.core_model,
            )
            detail = (
                "Core/audit mode · use /core and /audit · "
                f"{'core ready' if switch_ok else 'core unchanged'} ({switch_message})"
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
                runtime.backend_manager.agent_mode = current
                runtime.backend_manager._save_state()
                detail = f"Dual-brain not activated · right brain switch failed ({switch_message})"
                value = current
            else:
                ensure_dual_brain_observer(runtime.workspace_dir)
                runtime.reload_post_turn_observers()
                detail = f"Left/right brain mode · use /brain · right brain ready ({switch_message})"
    else:
        if hasattr(backend, "set_session_mode"):
            backend.set_session_mode(False)
        detail = "Full context injection · /backend enabled"

    await query.edit_message_text(
        f"Mode: <b>{value}</b>\n{detail}",
        parse_mode="HTML",
        reply_markup=mode_keyboard(value),
    )
    await query.answer(f"Switched to {value}")
