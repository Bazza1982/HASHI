from __future__ import annotations

from typing import Any

from orchestrator.command_registry import RuntimeCallback
from orchestrator.flexible_backend_registry import CLAUDE_MODEL_ALIASES


async def cmd_model(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if not runtime.backend_manager.current_backend:
        return

    current_model = runtime.backend_manager.current_backend.config.model
    args = context.args
    if args:
        requested = args[0].strip()
        if runtime.config.active_backend == "claude-cli":
            requested = CLAUDE_MODEL_ALIASES.get(requested.lower(), requested)
        available = runtime._get_available_models()
        if available and requested not in available:
            await runtime._reply_text(update, f"Unknown model: {requested}\nUse /model to see available options.")
            return

        runtime._set_backend_model(runtime.config.active_backend, requested)
        await runtime._reply_text(update, f"Model switched to: {requested}")
        return

    available = runtime._get_available_models()
    if not available:
        await runtime._reply_text(update, f"Current model: {current_model}\nUse /model <name> to switch.")
        return

    await runtime._reply_text(
        update,
        f"Current model: {current_model}\nSelect:",
        reply_markup=runtime._model_keyboard(current_model),
    )


async def callback_model(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        return
    data = query.data
    try:
        if data.startswith("model:"):
            model = data.split(":", 1)[1]
            available = runtime._get_available_models()
            if not available or model in available:
                runtime._set_backend_model(runtime.config.active_backend, model)
                await query.edit_message_text(
                    f"Model switched to: {model}",
                    reply_markup=runtime._model_keyboard(model),
                )
        elif data == "backend_menu":
            await query.edit_message_text(
                runtime._build_backend_menu_text(),
                reply_markup=runtime._backend_keyboard(),
            )
        elif data.startswith("backend:"):
            parts = data.split(":", 2)
            if len(parts) != 3:
                await query.answer("Invalid callback data", show_alert=True)
                return
            _, target_engine, mode = parts
            with_context = mode == "context"
            await query.edit_message_text(
                runtime._build_backend_model_prompt(target_engine, with_context),
                reply_markup=runtime._backend_model_keyboard(target_engine, with_context),
            )
        elif data.startswith("bmodel:"):
            parts = data.split(":", 3)
            if len(parts) != 4:
                await query.answer("Invalid callback data", show_alert=True)
                return
            _, target_engine, mode_flag, model = parts
            with_context = mode_flag == "c"
            success, message = await runtime._switch_backend_mode(
                query.message.chat_id,
                target_engine,
                target_model=model,
                with_context=with_context,
            )
            if not success and "busy" in message.lower():
                await query.answer(message, show_alert=True)
                return
            await query.edit_message_text(
                message,
                reply_markup=runtime._backend_keyboard() if success else runtime._backend_model_keyboard(target_engine, with_context, model),
            )
        elif data.startswith("effort:"):
            requested = data.split(":", 1)[1]
            if requested in runtime._get_available_efforts():
                runtime._set_active_effort(requested)
                await query.edit_message_text(
                    f"Effort switched to: {requested}",
                    reply_markup=runtime._effort_keyboard(requested),
                )
    except Exception as exc:
        runtime.error_logger.error(f"callback_model error: {exc}", exc_info=True)
        await query.answer(f"Error: {exc}", show_alert=True)
        return
    await query.answer()


CALLBACKS = [
    RuntimeCallback(pattern=r"^(model|backend|bmodel|effort|backend_menu)", callback=callback_model),
]
