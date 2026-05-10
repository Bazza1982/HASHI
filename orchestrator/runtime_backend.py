from __future__ import annotations

from typing import Any

from orchestrator.flexible_backend_registry import CLAUDE_MODEL_ALIASES


async def cmd_backend(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    if runtime.backend_manager.agent_mode == "fixed":
        await runtime._reply_text(
            update,
            "Backend switching is disabled in **fixed** mode.\nUse `/mode flex` to re-enable.",
            parse_mode="Markdown",
        )
        return

    args = context.args
    allowed_engines = [backend["engine"] for backend in runtime.config.allowed_backends]

    if not args:
        await runtime._reply_text(update, runtime._build_backend_menu_text(), reply_markup=runtime._backend_keyboard())
        return

    target_engine = args[0].lower()
    with_context = False
    requested_model = None
    for raw_arg in args[1:]:
        raw_value = raw_arg.strip()
        if not raw_value:
            continue
        flag = raw_value.lower()
        if flag in {"+", "context", "handoff", "with-context"}:
            with_context = True
        else:
            requested_model = raw_value

    if target_engine not in allowed_engines:
        await runtime._reply_text(update, f"Backend not allowed: {target_engine}")
        return

    if requested_model:
        if target_engine == "claude-cli":
            requested_model = CLAUDE_MODEL_ALIASES.get(requested_model.lower(), requested_model)
        available = runtime._get_available_models_for(target_engine)
        if available and requested_model not in available:
            await runtime._reply_text(
                update,
                f"Unknown model for {target_engine}: {requested_model}\nUse /backend {target_engine} to see available options.",
            )
            return

        _, message = await runtime._switch_backend_mode(
            update.effective_chat.id,
            target_engine,
            target_model=requested_model,
            with_context=with_context,
        )
        await runtime._reply_text(update, message)
        return

    await runtime._reply_text(
        update,
        runtime._build_backend_model_prompt(target_engine, with_context),
        reply_markup=runtime._backend_model_keyboard(target_engine, with_context),
    )
