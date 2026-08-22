from __future__ import annotations

import html
from hashlib import blake2s
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import runtime_menu_views, runtime_mode
from orchestrator.command_ui import BACK_LABEL, selected_label, setting_card
from orchestrator.flexible_backend_registry import (
    CLAUDE_MODEL_ALIASES,
    HER_V2_ENGINE,
    get_backend_label,
    normalize_effort,
    normalize_model,
)
from orchestrator.her_v2.models import Route
from orchestrator.her_v2.runtime_configuration import HER_V2_STANDARD_REASONING
from orchestrator.memory_plus_mode import set_memory_plus_enabled

HER_V2_ROUTE_ORDER = tuple(Route)
HER_V2_ROUTE_LABELS = {
    Route.IMMEDIATE_RESPONSE: "Immediate response",
    Route.TRIAGE: "Triage",
    Route.PLANNING: "Planning",
    Route.EXECUTION_SIMPLE: "Simple execution",
    Route.EXECUTION_COMPLEX: "Complex execution",
    Route.EXECUTION_HIGH_VOLUME: "High-volume execution",
    Route.REPLANNING: "Replanning",
    Route.REVIEW: "Review",
    Route.FINALISATION: "Finalisation",
    Route.MEDITATION: "Meditation",
    Route.DREAM: "Dream",
}
HER_V2_ROUTE_ALIASES = {
    "immediate": Route.IMMEDIATE_RESPONSE,
    "simple": Route.EXECUTION_SIMPLE,
    "complex": Route.EXECUTION_COMPLEX,
    "high_volume": Route.EXECUTION_HIGH_VOLUME,
    "finalization": Route.FINALISATION,
}


def _her_v2_callback_token(value: Any) -> str:
    return blake2s(str(value).encode("utf-8"), digest_size=6).hexdigest()


def _her_v2_indexed_choice(values, raw_index: str):
    index = int(raw_index)
    if index < 0:
        raise IndexError("callback index must be non-negative")
    return index, values[index]


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


def her_v2_provider_keyboard(runtime) -> InlineKeyboardMarkup:
    current = runtime.backend_manager.get_her_v2_configuration().provider
    buttons: list[list[InlineKeyboardButton]] = []
    for index, option in enumerate(runtime.backend_manager.get_her_v2_provider_options()):
        label = str(option["label"])
        token = _her_v2_callback_token(option["engine"])
        if option["available"]:
            callback = f"her_provider:{index}:{token}"
            label = selected_label(label, option["engine"] == current)
        else:
            callback = f"her_provider_locked:{index}:{token}"
            label = f"🔒 {label}"
        buttons.append([InlineKeyboardButton(label, callback_data=callback)])
    return InlineKeyboardMarkup(buttons)


def her_v2_provider_menu_text(runtime) -> str:
    options = runtime.backend_manager.get_her_v2_provider_options()
    selected = runtime.backend_manager.get_her_v2_configuration()
    return runtime_menu_views.her_v2_provider_menu_text(
        current_provider=selected.provider,
        available_count=sum(bool(option["available"]) for option in options),
        unavailable=[
            (str(option["label"]), str(option.get("reason") or "unavailable"))
            for option in options
            if not option["available"]
        ],
    )


def her_v2_model_keyboard(runtime) -> InlineKeyboardMarkup:
    selected = runtime.backend_manager.get_her_v2_configuration()
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Quick model · {selected.fast_model}",
                    callback_data="her_model_slot:fast",
                )
            ],
            [
                InlineKeyboardButton(
                    f"Pro model · {selected.pro_model}",
                    callback_data="her_model_slot:pro",
                )
            ],
            [
                InlineKeyboardButton(
                    "Task routes · model + reasoning",
                    callback_data="her_routes",
                )
            ],
        ]
    )


def her_v2_model_menu_text(runtime) -> str:
    selected = runtime.backend_manager.get_her_v2_configuration()
    return runtime_menu_views.her_v2_model_menu_text(
        provider=selected.provider,
        fast_model=selected.fast_model,
        pro_model=selected.pro_model,
    )


def her_v2_slot_model_keyboard(runtime, slot: str) -> InlineKeyboardMarkup:
    selected = runtime.backend_manager.get_her_v2_configuration()
    options = runtime.backend_manager.get_her_v2_provider_options()
    provider_index = next(
        (
            index
            for index, candidate in enumerate(options)
            if candidate.get("engine") == selected.provider
        ),
        -1,
    )
    option = options[provider_index] if provider_index >= 0 else None
    models = list(option["models"]) if option and option["available"] else []
    current = selected.fast_model if slot == "fast" else selected.pro_model
    buttons = [
        [
            InlineKeyboardButton(
                selected_label(model, model == current),
                callback_data=(
                    f"her_model:{slot}:{provider_index}:"
                    f"{_her_v2_callback_token(selected.provider)}:{index}:"
                    f"{_her_v2_callback_token(model)}"
                ),
            )
        ]
        for index, model in enumerate(models)
    ]
    buttons.append([InlineKeyboardButton(BACK_LABEL, callback_data="model_menu")])
    return InlineKeyboardMarkup(buttons)


def her_v2_slot_model_text(runtime, slot: str) -> str:
    selected = runtime.backend_manager.get_her_v2_configuration()
    option = runtime.backend_manager._her_v2_provider_option(selected.provider)
    models = list(option["models"]) if option and option["available"] else []
    current = selected.fast_model if slot == "fast" else selected.pro_model
    return runtime_menu_views.her_v2_slot_model_text(
        provider=selected.provider,
        slot=slot,
        current_model=current,
        model_count=len(models),
    )


def _her_v2_route(raw: Route | str) -> Route:
    if isinstance(raw, Route):
        return raw
    normalized = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    alias = HER_V2_ROUTE_ALIASES.get(normalized)
    return alias if alias is not None else Route(normalized)


def _her_v2_route_slot_label(slot: str) -> str:
    return {
        "fast": "Quick",
        "pro": "Pro",
        "inherit": "Follow source",
    }.get(slot, slot)


def _her_v2_route_effective_model(runtime, route: Route) -> str:
    selected = runtime.backend_manager.get_her_v2_configuration()
    slot = selected.model_slot_for_route(route)
    if slot == "fast":
        return selected.fast_model
    if slot == "pro":
        return selected.pro_model
    return "source-stage model"


def _her_v2_route_effective_reasoning(runtime, route: Route) -> str:
    selected = runtime.backend_manager.get_her_v2_configuration()
    return selected.reasoning_for_route(
        runtime.backend_manager._her_v2_base_config(),
        route,
    )


def her_v2_routes_text(runtime) -> str:
    selected = runtime.backend_manager.get_her_v2_configuration()
    return runtime_menu_views.her_v2_routes_text(
        route_count=len(HER_V2_ROUTE_ORDER),
        explicit_reasoning_count=len(selected.route_reasoning),
    )


def her_v2_routes_keyboard(runtime) -> InlineKeyboardMarkup:
    selected = runtime.backend_manager.get_her_v2_configuration()
    buttons = [
        [
            InlineKeyboardButton(
                (
                    f"{HER_V2_ROUTE_LABELS[route]} · "
                    f"{_her_v2_route_slot_label(selected.model_slot_for_route(route))} · "
                    f"{_her_v2_route_effective_reasoning(runtime, route)}"
                ),
                callback_data=f"her_route_menu:{route.value}",
            )
        ]
        for route in HER_V2_ROUTE_ORDER
    ]
    buttons.append([InlineKeyboardButton(BACK_LABEL, callback_data="model_menu")])
    return InlineKeyboardMarkup(buttons)


def _her_v2_route_reasoning_choices(runtime, route: Route) -> tuple[str, list[str]]:
    current = _her_v2_route_effective_reasoning(runtime, route)
    choices = list(HER_V2_STANDARD_REASONING)
    if current not in {"default", "Follow source", *choices}:
        choices.append(current)
    choices.append("inherit")
    return current, choices


def her_v2_route_keyboard(runtime, route: Route | str) -> InlineKeyboardMarkup:
    parsed = _her_v2_route(route)
    selected = runtime.backend_manager.get_her_v2_configuration()
    current_slot = selected.model_slot_for_route(parsed)
    slots = ["fast", "pro"]
    buttons = [
        [
            InlineKeyboardButton(
                selected_label(
                    f"Model · {_her_v2_route_slot_label(slot)}",
                    slot == current_slot,
                ),
                callback_data=f"her_route_slot:{parsed.value}:{slot}",
            )
            for slot in slots
        ]
    ]
    current_reasoning, choices = _her_v2_route_reasoning_choices(runtime, parsed)
    explicit = selected.route_reasoning.get(parsed.value)
    for index, value in enumerate(choices):
        is_selected = (
            value == explicit if explicit is not None else value == "inherit"
        )
        label = (
            f"Reasoning · Inherit ({current_reasoning})"
            if value == "inherit"
            else f"Reasoning · {value}"
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    selected_label(label, is_selected),
                    callback_data=(
                        f"her_route_reasoning:{parsed.value}:{index}:"
                        f"{_her_v2_callback_token(value)}"
                    ),
                )
            ]
        )
    buttons.append([InlineKeyboardButton(BACK_LABEL, callback_data="her_routes")])
    return InlineKeyboardMarkup(buttons)


def her_v2_route_text(runtime, route: Route | str) -> str:
    parsed = _her_v2_route(route)
    selected = runtime.backend_manager.get_her_v2_configuration()
    slot = selected.model_slot_for_route(parsed)
    return runtime_menu_views.her_v2_route_text(
        label=HER_V2_ROUTE_LABELS[parsed],
        model_slot=_her_v2_route_slot_label(slot),
        effective_model=_her_v2_route_effective_model(runtime, parsed),
        reasoning=_her_v2_route_effective_reasoning(runtime, parsed),
        reasoning_inherited=parsed.value not in selected.route_reasoning,
    )


def apply_her_v2_configuration(runtime, selected) -> str | None:
    if runtime.config.active_backend != HER_V2_ENGINE:
        return "HER v2 configuration is available only while HER v2 is active."
    if runtime._backend_busy():
        return "HER v2 configuration is blocked while a request is running or queued."
    try:
        runtime.backend_manager.apply_her_v2_configuration(selected)
    except (OSError, TypeError, ValueError) as exc:
        return str(exc)
    return None


def set_backend_model(runtime, engine: str, requested: str) -> None:
    normalized = normalize_model(engine, requested)
    if not normalized:
        return
    backend_cfg = runtime._get_backend_cfg(engine)
    if backend_cfg is not None:
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
    )


async def cmd_provider(runtime, update, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if runtime.config.active_backend != HER_V2_ENGINE:
        await runtime._reply_text(
            update,
            runtime_menu_views.her_v2_provider_unavailable_text(
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
                "HER v2 provider",
                current="<b>MANAGED</b>",
                facts=[
                    "<b>Backend</b> · <code>her-v2</code>",
                    f"<b>Mode</b> · <code>{html.escape(managed_mode)}</code>",
                ],
                consequence="Provider and model-slot changes are controlled by this mode's model configuration.",
                action=f"Use {managed_commands[managed_mode]}, or switch to <code>/mode flex</code>.",
            ),
            parse_mode="HTML",
        )
        return

    args = context.args or []
    if not args:
        await runtime._reply_text(
            update,
            her_v2_provider_menu_text(runtime),
            parse_mode="HTML",
            reply_markup=her_v2_provider_keyboard(runtime),
        )
        return

    provider = args[0].strip()
    try:
        selected = runtime.backend_manager.prepare_her_v2_provider(provider)
        error = apply_her_v2_configuration(runtime, selected)
    except (TypeError, ValueError) as exc:
        error = str(exc)
    if error:
        text = her_v2_provider_menu_text(runtime)
        reason = error
        text += f"\n\n❌ {html.escape(provider)} is unavailable: {html.escape(reason)}."
        await runtime._reply_text(
            update,
            text,
            parse_mode="HTML",
            reply_markup=her_v2_provider_keyboard(runtime),
        )
        return

    await runtime._reply_text(
        update,
        her_v2_model_menu_text(runtime),
        parse_mode="HTML",
        reply_markup=her_v2_model_keyboard(runtime),
    )


async def _cmd_her_v2_model(runtime, update, args: list[str]) -> None:
    if not args:
        await runtime._reply_text(
            update,
            her_v2_model_menu_text(runtime),
            parse_mode="HTML",
            reply_markup=her_v2_model_keyboard(runtime),
        )
        return

    action = args[0].strip().lower()
    if action in {"quick", "fast", "pro"}:
        slot = "fast" if action in {"quick", "fast"} else "pro"
    else:
        slot = ""
    if slot and len(args) == 1:
        await runtime._reply_text(
            update,
            her_v2_slot_model_text(runtime, slot),
            parse_mode="HTML",
            reply_markup=her_v2_slot_model_keyboard(runtime, slot),
        )
        return
    if slot and len(args) == 2:
        try:
            selected = runtime.backend_manager.prepare_her_v2_model(slot, args[1])
            error = apply_her_v2_configuration(runtime, selected)
        except (TypeError, ValueError) as exc:
            error = str(exc)
        if error:
            await runtime._reply_text(update, f"HER v2 model was not changed: {error}")
            return
        await runtime._reply_text(
            update,
            her_v2_model_menu_text(runtime),
            parse_mode="HTML",
            reply_markup=her_v2_model_keyboard(runtime),
        )
        return

    if action in {"route", "routes"} and len(args) == 1:
        await runtime._reply_text(
            update,
            her_v2_routes_text(runtime),
            parse_mode="HTML",
            reply_markup=her_v2_routes_keyboard(runtime),
        )
        return
    if action == "route" and len(args) in {2, 3}:
        try:
            route = _her_v2_route(args[1])
            if len(args) == 3:
                selected = runtime.backend_manager.prepare_her_v2_route_model_slot(
                    route.value,
                    args[2],
                )
                error = apply_her_v2_configuration(runtime, selected)
            else:
                error = None
        except (TypeError, ValueError) as exc:
            error = str(exc)
            route = None
        if error or route is None:
            await runtime._reply_text(update, f"HER v2 route was not changed: {error}")
            return
        await runtime._reply_text(
            update,
            her_v2_route_text(runtime, route),
            parse_mode="HTML",
            reply_markup=her_v2_route_keyboard(runtime, route),
        )
        return

    if action == "reasoning" and len(args) == 1:
        await runtime._reply_text(
            update,
            her_v2_routes_text(runtime),
            parse_mode="HTML",
            reply_markup=her_v2_routes_keyboard(runtime),
        )
        return
    if action == "reasoning" and len(args) in {2, 3}:
        try:
            route = _her_v2_route(args[1])
            if len(args) == 3:
                selected = runtime.backend_manager.prepare_her_v2_route_reasoning(
                    route.value,
                    args[2],
                )
                error = apply_her_v2_configuration(runtime, selected)
            else:
                error = None
        except (TypeError, ValueError) as exc:
            error = str(exc)
            route = None
        if error or route is None:
            await runtime._reply_text(update, f"HER v2 reasoning was not changed: {error}")
            return
        await runtime._reply_text(
            update,
            her_v2_route_text(runtime, route),
            parse_mode="HTML",
            reply_markup=her_v2_route_keyboard(runtime, route),
        )
        return

    await runtime._reply_text(
        update,
        "Usage: /model | /model quick|pro [model] | /model routes | "
        "/model route <route> [quick|pro|inherit] | "
        "/model reasoning <route> [value|inherit]",
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

    if runtime.config.active_backend == HER_V2_ENGINE:
        await _cmd_her_v2_model(runtime, update, list(context.args or []))
        return

    current_model = runtime.backend_manager.current_backend.config.model
    provider = runtime.get_current_provider()

    args = context.args
    if args:
        requested = args[0].strip()
        if runtime.config.active_backend == "claude-cli":
            requested = CLAUDE_MODEL_ALIASES.get(requested.lower(), requested)
        available = runtime._get_available_models()
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
    her_v2_control = data.startswith(
        ("her_provider", "her_model", "her_route", "her_reasoning")
    ) or data in {"model_menu", "her_routes"}
    if (
        her_v2_control
        and runtime.config.active_backend == HER_V2_ENGINE
        and runtime.backend_manager.agent_mode in {"wrapper", "audit", "dual-brain"}
    ):
        await query.answer(
            "HER v2 provider and model controls are managed by the active mode.",
            show_alert=True,
        )
        return
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
        elif data.startswith("her_provider_locked:"):
            if runtime.config.active_backend != HER_V2_ENGINE:
                await query.answer(
                    "Provider control is available only while HER v2 is active.",
                    show_alert=True,
                )
                return
            try:
                _, raw_index, token = data.split(":", 2)
                _index, option = _her_v2_indexed_choice(
                    runtime.backend_manager.get_her_v2_provider_options(),
                    raw_index,
                )
                if token != _her_v2_callback_token(option["engine"]):
                    raise ValueError("stale provider option")
            except (IndexError, TypeError, ValueError):
                await query.answer("This provider option is no longer available.", show_alert=True)
                return
            await query.answer(
                f"{option['label']}: {option.get('reason') or 'unavailable'}",
                show_alert=True,
            )
            return
        elif data.startswith("her_provider:"):
            if runtime.config.active_backend != HER_V2_ENGINE:
                await query.answer(
                    "Provider control is available only while HER v2 is active.",
                    show_alert=True,
                )
                return
            try:
                _, raw_index, token = data.split(":", 2)
                _index, option = _her_v2_indexed_choice(
                    runtime.backend_manager.get_her_v2_provider_options(),
                    raw_index,
                )
                if token != _her_v2_callback_token(option["engine"]):
                    raise ValueError("This provider menu is stale.")
                selected = runtime.backend_manager.prepare_her_v2_provider(
                    str(option["engine"])
                )
                error = apply_her_v2_configuration(runtime, selected)
            except (IndexError, TypeError, ValueError) as exc:
                error = str(exc)
            if error:
                await query.answer(error, show_alert=True)
                return
            await query.edit_message_text(
                her_v2_model_menu_text(runtime),
                parse_mode="HTML",
                reply_markup=her_v2_model_keyboard(runtime),
            )
        elif data.startswith("her_model_slot:"):
            if runtime.config.active_backend != HER_V2_ENGINE:
                await query.answer(
                    "Model-slot control is available only while HER v2 is active.",
                    show_alert=True,
                )
                return
            slot = data.split(":", 1)[1]
            if slot not in {"fast", "pro"}:
                await query.answer("Invalid HER v2 model slot.", show_alert=True)
                return
            await query.edit_message_text(
                her_v2_slot_model_text(runtime, slot),
                parse_mode="HTML",
                reply_markup=her_v2_slot_model_keyboard(runtime, slot),
            )
        elif data.startswith("her_model:"):
            if runtime.config.active_backend != HER_V2_ENGINE:
                await query.answer(
                    "Model-slot control is available only while HER v2 is active.",
                    show_alert=True,
                )
                return
            try:
                (
                    _,
                    slot,
                    raw_provider_index,
                    provider_token,
                    raw_model_index,
                    model_token,
                ) = data.split(":", 5)
                selected = runtime.backend_manager.get_her_v2_configuration()
                options = runtime.backend_manager.get_her_v2_provider_options()
                _provider_index, option = _her_v2_indexed_choice(
                    options,
                    raw_provider_index,
                )
                if (
                    option.get("engine") != selected.provider
                    or provider_token != _her_v2_callback_token(option["engine"])
                ):
                    raise ValueError(
                        "This model menu is stale because the HER v2 provider changed."
                    )
                _model_index, model = _her_v2_indexed_choice(
                    list(option["models"]),
                    raw_model_index,
                )
                if model_token != _her_v2_callback_token(model):
                    raise ValueError("This HER v2 model menu is stale.")
                candidate = runtime.backend_manager.prepare_her_v2_model(slot, model)
                error = apply_her_v2_configuration(runtime, candidate)
            except (IndexError, TypeError, ValueError) as exc:
                error = str(exc)
            if error:
                await query.answer(error, show_alert=True)
                return
            await query.edit_message_text(
                her_v2_model_menu_text(runtime),
                parse_mode="HTML",
                reply_markup=her_v2_model_keyboard(runtime),
            )
        elif data == "her_routes" or data == "her_reasoning_stages":
            if runtime.config.active_backend != HER_V2_ENGINE:
                await query.answer(
                    "Task-route control is available only while HER v2 is active.",
                    show_alert=True,
                )
                return
            await query.edit_message_text(
                her_v2_routes_text(runtime),
                parse_mode="HTML",
                reply_markup=her_v2_routes_keyboard(runtime),
            )
        elif data.startswith("her_route_menu:"):
            if runtime.config.active_backend != HER_V2_ENGINE:
                await query.answer(
                    "Task-route control is available only while HER v2 is active.",
                    show_alert=True,
                )
                return
            try:
                route = _her_v2_route(data.split(":", 1)[1])
            except ValueError:
                await query.answer("Invalid HER v2 task route.", show_alert=True)
                return
            await query.edit_message_text(
                her_v2_route_text(runtime, route),
                parse_mode="HTML",
                reply_markup=her_v2_route_keyboard(runtime, route),
            )
        elif data.startswith("her_route_slot:"):
            if runtime.config.active_backend != HER_V2_ENGINE:
                await query.answer(
                    "Task-route control is available only while HER v2 is active.",
                    show_alert=True,
                )
                return
            try:
                _, raw_route, slot = data.split(":", 2)
                route = _her_v2_route(raw_route)
                candidate = runtime.backend_manager.prepare_her_v2_route_model_slot(
                    route.value,
                    slot,
                )
                error = apply_her_v2_configuration(runtime, candidate)
            except (TypeError, ValueError) as exc:
                error = str(exc)
                route = None
            if error or route is None:
                await query.answer(error or "Invalid HER v2 task route.", show_alert=True)
                return
            await query.edit_message_text(
                her_v2_route_text(runtime, route),
                parse_mode="HTML",
                reply_markup=her_v2_route_keyboard(runtime, route),
            )
        elif data.startswith("her_route_reasoning:"):
            if runtime.config.active_backend != HER_V2_ENGINE:
                await query.answer(
                    "Task-route control is available only while HER v2 is active.",
                    show_alert=True,
                )
                return
            try:
                _, raw_route, raw_index, token = data.split(":", 3)
                route = _her_v2_route(raw_route)
                _current, choices = _her_v2_route_reasoning_choices(runtime, route)
                _index, reasoning = _her_v2_indexed_choice(choices, raw_index)
                if token != _her_v2_callback_token(reasoning):
                    raise ValueError("This HER v2 task-route menu is stale.")
                candidate = runtime.backend_manager.prepare_her_v2_route_reasoning(
                    route.value,
                    None if reasoning == "inherit" else reasoning,
                )
                error = apply_her_v2_configuration(runtime, candidate)
            except (IndexError, TypeError, ValueError) as exc:
                error = str(exc)
                route = None
            if error or route is None:
                await query.answer(error, show_alert=True)
                return
            await query.edit_message_text(
                her_v2_route_text(runtime, route),
                parse_mode="HTML",
                reply_markup=her_v2_route_keyboard(runtime, route),
            )
        elif data.startswith(("her_reasoning_menu:", "her_reasoning:")):
            await query.answer(
                "This old reasoning menu was retired. Reopen /model and choose a task route.",
                show_alert=True,
            )
            return
        elif data == "model_menu":
            if runtime.config.active_backend == HER_V2_ENGINE:
                await query.edit_message_text(
                    her_v2_model_menu_text(runtime),
                    parse_mode="HTML",
                    reply_markup=her_v2_model_keyboard(runtime),
                )
                await query.answer()
                return
            current_model = runtime.get_current_model()
            provider = runtime.get_current_provider()
            available = runtime._get_available_models()
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
            if runtime.config.active_backend == HER_V2_ENGINE:
                await query.answer(
                    "Use the HER v2 Quick/Pro model controls.",
                    show_alert=True,
                )
                return
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
            if target_engine == HER_V2_ENGINE:
                success, message = await runtime._switch_backend_mode(
                    query.message.chat_id,
                    HER_V2_ENGINE,
                    with_context=with_context,
                )
                if not success:
                    await query.answer(message, show_alert=True)
                    return
                await query.edit_message_text(
                    runtime_menu_views.her_v2_backend_selected_text(
                        with_context=with_context
                    ),
                    parse_mode="HTML",
                    reply_markup=None,
                )
            else:
                await query.edit_message_text(
                    runtime._build_backend_model_prompt(target_engine, with_context),
                    parse_mode="HTML",
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
