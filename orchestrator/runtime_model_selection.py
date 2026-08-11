from __future__ import annotations

import html
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import runtime_menu_views, runtime_mode
from orchestrator.command_ui import BACK_LABEL, selected_label, setting_card
from orchestrator.flexible_backend_registry import (
    CLAUDE_MODEL_ALIASES,
    get_backend_label,
    normalize_effort,
    normalize_model,
)
from orchestrator.memory_plus_mode import set_memory_plus_enabled


def backend_keyboard(runtime) -> InlineKeyboardMarkup:
    current = runtime.config.active_backend
    buttons = []
    seen: set[str] = set()
    for backend in runtime.config.allowed_backends:
        engine = backend["engine"]
        if engine in seen:
            continue
        seen.add(engine)
        base = get_backend_label(engine)
        buttons.append(
            [
                InlineKeyboardButton(
                    selected_label(base, engine == current),
                    callback_data=f"backend:{engine}:plain",
                ),
                InlineKeyboardButton(
                    f"{base} · with context",
                    callback_data=f"backend:{engine}:context",
                ),
            ]
        )
    return InlineKeyboardMarkup(buttons)


def claw_provider_mode(runtime, *, with_context: bool = False, active: bool = False) -> str:
    if active:
        return "a"
    return "c" if with_context else "p"


def claw_provider_options(runtime) -> list[dict[str, Any]]:
    return runtime.backend_manager.get_claw_provider_options()


def claw_provider_option(runtime, provider: str) -> dict[str, Any] | None:
    requested = str(provider or "").strip().casefold()
    return next(
        (
            option
            for option in runtime._claw_provider_options()
            if str(option["name"]).casefold() == requested
        ),
        None,
    )


def claw_provider_callback_error(runtime, mode_flag: str) -> str | None:
    if mode_flag not in {"a", "p", "c"}:
        return "Invalid provider menu."
    if mode_flag == "a" and runtime.config.active_backend != "her":
        return "Provider control is available only while HER is active."
    if mode_flag in {"p", "c"} and runtime.backend_manager.agent_mode != "flex":
        return "Backend switching is available only in Flex mode."
    if runtime.backend_manager.agent_mode in {"wrapper", "audit", "dual-brain"}:
        return "Provider switching is managed by the active mode."
    return None


def claw_provider_keyboard(runtime, mode_flag: str) -> InlineKeyboardMarkup:
    current = runtime.get_current_provider()
    buttons: list[list[InlineKeyboardButton]] = []
    for option in runtime._claw_provider_options():
        provider = str(option["name"])
        if option["available"]:
            label = selected_label(provider, provider == current)
            callback = f"provider:{mode_flag}:{provider}"
        else:
            label = f"🔒 {provider}"
            callback = f"provider_locked:{mode_flag}:{provider}"
        buttons.append([InlineKeyboardButton(label, callback_data=callback)])
    if mode_flag in {"p", "c"}:
        buttons.append([InlineKeyboardButton(BACK_LABEL, callback_data="backend_menu")])
    return InlineKeyboardMarkup(buttons)


def claw_provider_model_keyboard(
    runtime,
    provider: str,
    mode_flag: str,
) -> InlineKeyboardMarkup:
    option = runtime._claw_provider_option(provider)
    models = list(option["models"]) if option and option["available"] else []
    current_model = (
        runtime.get_current_model()
        if provider == runtime.get_current_provider()
        else None
    )
    buttons = [
        [
            InlineKeyboardButton(
                selected_label(model, model == current_model),
                callback_data=f"pmodel:{mode_flag}:{provider}:{index}",
            )
        ]
        for index, model in enumerate(models)
    ]
    buttons.append(
        [InlineKeyboardButton(BACK_LABEL, callback_data=f"provider_menu:{mode_flag}")]
    )
    return InlineKeyboardMarkup(buttons)


def claw_provider_menu_text(runtime, mode_flag: str) -> str:
    options = runtime._claw_provider_options()
    unavailable = [
        (str(option["name"]), str(option.get("reason") or "unavailable"))
        for option in options
        if not option["available"]
    ]
    return runtime_menu_views.claw_provider_menu_text(
        current_provider=runtime.get_current_provider(),
        available_count=sum(bool(option["available"]) for option in options),
        unavailable=unavailable,
        backend_flow=mode_flag in {"p", "c"},
    )


def claw_provider_model_text(runtime, provider: str, mode_flag: str) -> str:
    option = runtime._claw_provider_option(provider)
    models = list(option["models"]) if option and option["available"] else []
    current_model = (
        runtime.get_current_model()
        if provider == runtime.get_current_provider()
        else ""
    )
    return runtime_menu_views.claw_provider_model_text(
        provider=provider,
        current_model=current_model,
        model_count=len(models),
        with_context=mode_flag == "c",
    )


def set_backend_model(runtime, engine: str, requested: str) -> None:
    normalized = requested.strip() if engine == "her" else normalize_model(engine, requested)
    if not normalized:
        return
    provider = runtime.get_current_provider() if engine == "her" else None
    backend_cfg = runtime._get_backend_cfg(engine, provider)
    if backend_cfg is not None and engine != "her":
        backend_cfg["model"] = normalized
    if engine == runtime.config.active_backend and runtime.backend_manager.current_backend:
        runtime.backend_manager.current_backend.config.model = normalized
        current_effort = getattr(runtime.backend_manager.current_backend, "effort", None)
        normalized_effort = normalize_effort(engine, current_effort, normalized)
        if normalized_effort:
            runtime.backend_manager.current_backend.effort = normalized_effort
            if backend_cfg is not None:
                backend_cfg["effort"] = normalized_effort
        else:
            runtime.backend_manager.current_backend.effort = None
            if backend_cfg is not None:
                backend_cfg.pop("effort", None)
    runtime.backend_manager.persist_state(
        active_model=normalized,
        active_provider=provider,
    )


async def cmd_provider(runtime, update, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if runtime.config.active_backend != "her":
        await runtime._reply_text(
            update,
            runtime_menu_views.claw_provider_unavailable_text(
                backend=runtime.config.active_backend,
            ),
            parse_mode="HTML",
        )
        return

    managed_mode = runtime.backend_manager.agent_mode
    managed_commands = {
        "wrapper": "<code>/core</code> and <code>/wrap</code>",
        "audit": "<code>/core</code> and <code>/audit</code>",
        "dual-brain": "<code>/brain</code>",
    }
    if managed_mode in managed_commands:
        await runtime._reply_text(
            update,
            setting_card(
                "🔌",
                "HER provider",
                current="<b>MANAGED</b>",
                facts=[
                    "<b>Backend</b> · <code>her</code>",
                    f"<b>Mode</b> · <code>{html.escape(managed_mode)}</code>",
                ],
                consequence="Provider and model changes are controlled by this mode's model configuration.",
                action=f"Use {managed_commands[managed_mode]}, or switch to <code>/mode flex</code>.",
            ),
            parse_mode="HTML",
        )
        return

    mode_flag = runtime._claw_provider_mode(active=True)
    args = context.args or []
    if not args:
        await runtime._reply_text(
            update,
            runtime._build_claw_provider_menu_text(mode_flag),
            parse_mode="HTML",
            reply_markup=runtime._claw_provider_keyboard(mode_flag),
        )
        return

    provider = args[0].strip()
    option = runtime._claw_provider_option(provider)
    if option is None or not option["available"]:
        reason = str((option or {}).get("reason") or "provider is not allowed")
        text = runtime._build_claw_provider_menu_text(mode_flag)
        text += f"\n\n❌ {html.escape(provider)} is unavailable: {html.escape(reason)}."
        await runtime._reply_text(
            update,
            text,
            parse_mode="HTML",
            reply_markup=runtime._claw_provider_keyboard(mode_flag),
        )
        return

    provider = str(option["name"])
    await runtime._reply_text(
        update,
        runtime._build_claw_provider_model_text(provider, mode_flag),
        parse_mode="HTML",
        reply_markup=runtime._claw_provider_model_keyboard(provider, mode_flag),
    )


async def cmd_model(runtime, update, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if not runtime.backend_manager.current_backend:
        return
    if runtime.backend_manager.agent_mode == "wrapper":
        await runtime._reply_text(
            update,
            "Model switching is managed by `/core` and `/wrap` in **wrapper** mode.\nUse `/mode flex` for normal `/model` switching.",
            parse_mode="Markdown",
        )
        return
    if runtime.backend_manager.agent_mode == "audit":
        await runtime._reply_text(
            update,
            "Model switching is managed by `/core` and `/audit` in **audit** mode.\nUse `/mode flex` for normal `/model` switching.",
            parse_mode="Markdown",
        )
        return
    if runtime.backend_manager.agent_mode == "dual-brain":
        await runtime._reply_text(
            update,
            "Model switching is managed by `/brain` in **dual-brain** mode.\nUse `/mode flex` for normal `/model` switching.",
            parse_mode="Markdown",
        )
        return

    current_model = runtime.backend_manager.current_backend.config.model
    provider = runtime.get_current_provider()
    if runtime.config.active_backend == "her" and not provider:
        await runtime._reply_text(
            update,
            setting_card(
                "🧠",
                "Hashi model",
                current=f"<code>{html.escape(str(current_model))}</code>",
                facts=[
                    "<b>Backend</b> · <code>her</code>",
                    "<b>Provider</b> · <code>not selected</code>",
                ],
                consequence="A HER model cannot be selected until its provider is known.",
                action="Use <code>/provider</code> to choose a provider first.",
            ),
            parse_mode="HTML",
        )
        return

    args = context.args
    if args:
        requested = args[0].strip()
        if runtime.config.active_backend == "claude-cli":
            requested = CLAUDE_MODEL_ALIASES.get(requested.lower(), requested)
        available = runtime._get_available_models()
        if runtime.config.active_backend == "her" and not available:
            mode_flag = runtime._claw_provider_mode(active=True)
            await runtime._reply_text(
                update,
                runtime._build_claw_provider_model_text(str(provider), mode_flag),
                parse_mode="HTML",
                reply_markup=runtime._claw_provider_model_keyboard(str(provider), mode_flag),
            )
            return
        if available and requested not in available:
            await runtime._reply_text(
                update,
                f"Unknown model: {requested}\nUse /model to see available options.",
            )
            return

        runtime._set_backend_model(runtime.config.active_backend, requested)
        text, reply_markup = runtime._configuration_followup("model")
        await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=reply_markup)
        return

    available = runtime._get_available_models()
    if not available:
        if runtime.config.active_backend == "her":
            mode_flag = runtime._claw_provider_mode(active=True)
            await runtime._reply_text(
                update,
                runtime._build_claw_provider_model_text(str(provider), mode_flag),
                parse_mode="HTML",
                reply_markup=runtime._claw_provider_model_keyboard(str(provider), mode_flag),
            )
            return
        await runtime._reply_text(
            update,
            runtime_menu_views.model_menu_text(
                model=current_model,
                backend=runtime.config.active_backend,
                has_choices=False,
                persists=True,
                provider=provider,
            ),
            parse_mode="HTML",
        )
        return

    await runtime._reply_text(
        update,
        runtime_menu_views.model_menu_text(
            model=current_model,
            backend=runtime.config.active_backend,
            has_choices=True,
            persists=True,
            provider=provider,
        ),
        parse_mode="HTML",
        reply_markup=runtime._model_keyboard(current_model),
    )


async def callback_model(runtime, update, context: Any) -> None:
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        return
    data = query.data
    try:
        if data == "backend_mode_confirm":
            if runtime.backend_manager.agent_mode == "memory+":
                set_memory_plus_enabled(runtime.workspace_dir, True)
            runtime_mode.activate_flex_mode(runtime)
            await query.edit_message_text(
                runtime._build_backend_menu_text(),
                parse_mode="HTML",
                reply_markup=runtime._backend_keyboard(),
            )
        elif data.startswith("backend_mode_cancel:"):
            expected_mode = data.split(":", 1)[1]
            current_mode = runtime.backend_manager.agent_mode
            await query.edit_message_text(
                setting_card(
                    "🧠",
                    "Backend unchanged",
                    current=f"<code>{html.escape(current_mode)}</code>",
                    facts=[
                        f"<b>Backend</b> · <code>{html.escape(runtime.config.active_backend)}</code>"
                    ],
                    consequence="No mode, backend, model, or saved configuration was changed.",
                    action=(
                        "The mode changed elsewhere before this button was used."
                        if current_mode != expected_mode
                        else "The current mode remains active."
                    ),
                ),
                parse_mode="HTML",
            )
        elif data == "model_menu":
            current_model = runtime.get_current_model()
            provider = runtime.get_current_provider()
            available = runtime._get_available_models()
            if runtime.config.active_backend == "her" and provider and not available:
                mode_flag = runtime._claw_provider_mode(active=True)
                await query.edit_message_text(
                    runtime._build_claw_provider_model_text(provider, mode_flag),
                    parse_mode="HTML",
                    reply_markup=runtime._claw_provider_model_keyboard(provider, mode_flag),
                )
            else:
                await query.edit_message_text(
                    runtime_menu_views.model_menu_text(
                        model=current_model,
                        backend=runtime.config.active_backend,
                        has_choices=bool(available),
                        persists=True,
                        provider=provider,
                    ),
                    parse_mode="HTML",
                    reply_markup=runtime._model_keyboard(current_model),
                )
        elif data.startswith("model:"):
            model = data.split(":", 1)[1]
            available = runtime._get_available_models()
            if not available or model in available:
                runtime._set_backend_model(runtime.config.active_backend, model)
                text, reply_markup = runtime._configuration_followup("model")
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
            else:
                await query.answer(
                    "Model is not available for the active provider.",
                    show_alert=True,
                )
                return
        elif data == "backend_menu":
            await query.edit_message_text(
                runtime._build_backend_menu_text(),
                parse_mode="HTML",
                reply_markup=runtime._backend_keyboard(),
            )
        elif data.startswith("backend:"):
            parts = data.split(":", 2)
            if len(parts) != 3:
                await query.answer("Invalid callback data", show_alert=True)
                return
            _, target_engine, mode = parts
            with_context = mode == "context"
            if target_engine == "her":
                mode_flag = runtime._claw_provider_mode(with_context=with_context)
                await query.edit_message_text(
                    runtime._build_claw_provider_menu_text(mode_flag),
                    parse_mode="HTML",
                    reply_markup=runtime._claw_provider_keyboard(mode_flag),
                )
            else:
                await query.edit_message_text(
                    runtime._build_backend_model_prompt(target_engine, with_context),
                    parse_mode="HTML",
                    reply_markup=runtime._backend_model_keyboard(target_engine, with_context),
                )
        elif data.startswith("provider_menu:"):
            mode_flag = data.split(":", 1)[1]
            callback_error = runtime._claw_provider_callback_error(mode_flag)
            if callback_error:
                await query.answer(callback_error, show_alert=True)
                return
            await query.edit_message_text(
                runtime._build_claw_provider_menu_text(mode_flag),
                parse_mode="HTML",
                reply_markup=runtime._claw_provider_keyboard(mode_flag),
            )
        elif data.startswith("provider_locked:"):
            parts = data.split(":", 2)
            mode_flag = parts[1] if len(parts) == 3 else ""
            callback_error = runtime._claw_provider_callback_error(mode_flag)
            if callback_error:
                await query.answer(callback_error, show_alert=True)
                return
            provider = parts[2] if len(parts) == 3 else "provider"
            option = runtime._claw_provider_option(provider)
            reason = str((option or {}).get("reason") or "unavailable")
            await query.answer(f"{provider}: {reason}", show_alert=True)
            return
        elif data.startswith("provider:"):
            parts = data.split(":", 2)
            if len(parts) != 3:
                await query.answer("Invalid provider selection.", show_alert=True)
                return
            _, mode_flag, provider = parts
            callback_error = runtime._claw_provider_callback_error(mode_flag)
            if callback_error:
                await query.answer(callback_error, show_alert=True)
                return
            option = runtime._claw_provider_option(provider)
            if option is None or not option["available"]:
                await query.answer(
                    str((option or {}).get("reason") or "Provider is unavailable."),
                    show_alert=True,
                )
                return
            provider = str(option["name"])
            await query.edit_message_text(
                runtime._build_claw_provider_model_text(provider, mode_flag),
                parse_mode="HTML",
                reply_markup=runtime._claw_provider_model_keyboard(provider, mode_flag),
            )
        elif data.startswith("pmodel:"):
            parts = data.split(":", 3)
            if len(parts) != 4:
                await query.answer("Invalid HER model selection.", show_alert=True)
                return
            _, mode_flag, provider, raw_index = parts
            callback_error = runtime._claw_provider_callback_error(mode_flag)
            if callback_error:
                await query.answer(callback_error, show_alert=True)
                return
            option = runtime._claw_provider_option(provider)
            if option:
                provider = str(option["name"])
            try:
                model_index = int(raw_index)
                model = (
                    list(option["models"])[model_index]
                    if option and option["available"]
                    else None
                )
            except (TypeError, ValueError, IndexError):
                model = None
            if not model:
                await query.answer(
                    "This HER model is no longer available.",
                    show_alert=True,
                )
                return
            success, message = await runtime._switch_backend_mode(
                query.message.chat_id,
                "her",
                target_model=model,
                target_provider=provider,
                with_context=mode_flag == "c",
            )
            if not success and "busy" in message.lower():
                await query.answer(message, show_alert=True)
                return
            if success:
                source = "model" if mode_flag == "a" else "backend"
                text, reply_markup = runtime._configuration_followup(source)
                await query.edit_message_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            else:
                text = runtime._build_claw_provider_model_text(provider, mode_flag)
                text += f"\n\n❌ {html.escape(message)}"
                await query.edit_message_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=runtime._claw_provider_model_keyboard(provider, mode_flag),
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
            if success:
                text, reply_markup = runtime._configuration_followup("backend")
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
            else:
                await query.edit_message_text(
                    message,
                    reply_markup=runtime._backend_model_keyboard(target_engine, with_context, model),
                )
        elif data.startswith("effort:"):
            parts = data.split(":")
            source = parts[1] if len(parts) == 3 else None
            requested = parts[2] if len(parts) == 3 else parts[1]
            if source in {"backend", "model"} and requested == "keep":
                await query.edit_message_text(
                    runtime._build_model_configuration_summary(),
                    parse_mode="HTML",
                )
                await query.answer("Current effort kept")
                return
            if requested in runtime._get_available_efforts():
                runtime._set_active_effort(requested)
                if source in {"backend", "model"}:
                    await query.edit_message_text(
                        runtime._build_model_configuration_summary(),
                        parse_mode="HTML",
                    )
                else:
                    await query.edit_message_text(
                        f"Effort switched to: {requested}",
                        reply_markup=runtime._effort_keyboard(requested),
                    )
    except Exception as exc:
        runtime.error_logger.error("callback_model error: %s", exc, exc_info=True)
        await query.answer(f"Error: {exc}", show_alert=True)
        return
    await query.answer()
