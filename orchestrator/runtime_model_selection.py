from __future__ import annotations

import html
from hashlib import blake2s
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import runtime_menu_views, runtime_mode, ui_language
from orchestrator.command_ui import back_label, selected_label, setting_card
from orchestrator.flexible_backend_registry import (
    CLAUDE_MODEL_ALIASES,
    HER_V2_ENGINE,
    get_backend_label,
    is_selectable_backend,
    normalize_effort,
    normalize_model,
)
from orchestrator.her_v2.models import Route
from orchestrator.her_v2.runtime_configuration import HER_V2_STANDARD_REASONING
from orchestrator.memory_plus_mode import set_memory_plus_enabled

HER_V2_ROUTE_ORDER = tuple(Route)
HER_V2_ROUTE_LABELS = {
    Route.DIRECT: "Direct",
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
    # Two tokens plus the longest route ID must remain within Telegram's
    # 64-byte callback_data limit. Forty bits is ample for stale-menu guards.
    return blake2s(str(value).encode("utf-8"), digest_size=5).hexdigest()


def _her_v2_indexed_choice(values, raw_index: str):
    index = int(raw_index)
    if index < 0:
        raise IndexError("callback index must be non-negative")
    return index, values[index]


def _her_v2_edit_configuration(runtime):
    return runtime.backend_manager.get_her_v2_edit_configuration()


def backend_keyboard(runtime) -> InlineKeyboardMarkup:
    current = runtime.config.active_backend
    buttons = []
    seen: set[str] = set()
    for backend in runtime.config.allowed_backends:
        engine = backend["engine"]
        if engine in seen or not is_selectable_backend(engine):
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
                    f"{base} · {ui_language.tr('menu.backend.with_handoff')}",
                    callback_data=f"backend:{engine}:context",
                ),
            ]
        )
    return InlineKeyboardMarkup(buttons)


def her_v2_provider_keyboard(runtime) -> InlineKeyboardMarkup:
    current = _her_v2_edit_configuration(runtime)
    buttons: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                selected_label(
                    ui_language.tr("menu.her.hybrid_routing"),
                    current.routing_mode == "hybrid",
                ),
                callback_data="her_provider_hybrid",
            )
        ]
    ]
    for index, option in enumerate(runtime.backend_manager.get_her_v2_provider_options()):
        label = str(option["label"])
        token = _her_v2_callback_token(option["engine"])
        if option["available"]:
            callback = f"her_provider:{index}:{token}"
            label = selected_label(
                label,
                current.routing_mode == "single"
                and option["engine"] == current.provider,
            )
        else:
            callback = f"her_provider_locked:{index}:{token}"
            label = f"🔒 {label}"
        buttons.append([InlineKeyboardButton(label, callback_data=callback)])
    return InlineKeyboardMarkup(buttons)


def her_v2_provider_menu_text(runtime) -> str:
    options = runtime.backend_manager.get_her_v2_provider_options()
    selected = _her_v2_edit_configuration(runtime)
    return runtime_menu_views.her_v2_provider_menu_text(
        current_provider=(
            "Hybrid" if selected.routing_mode == "hybrid" else selected.provider
        ),
        available_count=sum(bool(option["available"]) for option in options),
        unavailable=[
            (str(option["label"]), str(option.get("reason") or "unavailable"))
            for option in options
            if not option["available"]
        ],
    )


def her_v2_model_keyboard(runtime) -> InlineKeyboardMarkup:
    selected = _her_v2_edit_configuration(runtime)
    buttons = [
            [
                InlineKeyboardButton(
                    f"Quick · {selected.fast_provider} / {selected.fast_model}",
                    callback_data="her_model_slot:fast",
                )
            ],
            [
                InlineKeyboardButton(
                    f"Pro · {selected.pro_provider} / {selected.pro_model}",
                    callback_data="her_model_slot:pro",
                )
            ],
            [
                InlineKeyboardButton(
                    ui_language.tr("menu.her.task_routes"),
                    callback_data="her_routes",
                )
            ],
            [
                InlineKeyboardButton(
                    ui_language.tr("menu.her.compact_route"),
                    callback_data="her_model_compact",
                )
            ],
        ]
    if runtime.backend_manager.has_her_v2_configuration_draft():
        buttons.extend(
            [
                [
                    InlineKeyboardButton(
                        ui_language.tr("menu.her.apply_draft"),
                        callback_data="her_model_apply",
                    ),
                    InlineKeyboardButton(
                        ui_language.tr("menu.her.discard"),
                        callback_data="her_model_discard",
                    ),
                ]
            ]
        )
    return InlineKeyboardMarkup(buttons)


def her_v2_model_menu_text(runtime) -> str:
    selected = _her_v2_edit_configuration(runtime)
    return runtime_menu_views.her_v2_model_menu_text(
        provider=(
            "Hybrid" if selected.routing_mode == "hybrid" else selected.provider
        ),
        routing_mode=selected.routing_mode,
        fast_provider=selected.fast_provider,
        fast_model=selected.fast_model,
        pro_provider=selected.pro_provider,
        pro_model=selected.pro_model,
        draft=runtime.backend_manager.has_her_v2_configuration_draft(),
    )


def her_v2_compact_text(runtime) -> str:
    from orchestrator.context_compaction import compact_status_text

    return compact_status_text(runtime)


def her_v2_compact_keyboard(runtime) -> InlineKeyboardMarkup:
    from orchestrator.context_compaction import load_route_config

    current = load_route_config(runtime)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    selected_label(
                        ui_language.tr("menu.her.tier_auto"),
                        current.timeout_tier == "auto",
                    ),
                    callback_data="her_model_compact_tier:auto",
                ),
                InlineKeyboardButton(
                    selected_label(
                        ui_language.tr("menu.her.tier_2"),
                        current.timeout_tier == "tier_2",
                    ),
                    callback_data="her_model_compact_tier:tier_2",
                ),
                InlineKeyboardButton(
                    selected_label(
                        ui_language.tr("menu.her.tier_3"),
                        current.timeout_tier == "tier_3",
                    ),
                    callback_data="her_model_compact_tier:tier_3",
                ),
            ],
            [InlineKeyboardButton(back_label(), callback_data="model_menu")],
        ]
    )


def her_v2_compact_provider_keyboard(runtime) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for index, option in enumerate(runtime.backend_manager.get_her_v2_provider_options()):
        if not option.get("available") or not option.get("models"):
            continue
        provider = str(option["engine"])
        buttons.append(
            [
                InlineKeyboardButton(
                    str(option.get("label") or provider),
                    callback_data=(
                        f"her_model_compact_provider:{index}:"
                        f"{_her_v2_callback_token(provider)}"
                    ),
                )
            ]
        )
    buttons.append([InlineKeyboardButton(back_label(), callback_data="her_model_compact")])
    return InlineKeyboardMarkup(buttons)


def her_v2_compact_model_keyboard(runtime, provider_index: int) -> InlineKeyboardMarkup:
    options = runtime.backend_manager.get_her_v2_provider_options()
    option = options[provider_index]
    provider = str(option["engine"])
    provider_token = _her_v2_callback_token(provider)
    buttons = [
        [
            InlineKeyboardButton(
                str(model),
                callback_data=(
                    f"her_model_compact_model:{provider_index}:{provider_token}:"
                    f"{model_index}:{_her_v2_callback_token(model)}"
                ),
            )
        ]
        for model_index, model in enumerate(option.get("models") or [])
    ]
    buttons.append(
        [InlineKeyboardButton(back_label(), callback_data="her_model_compact_providers")]
    )
    return InlineKeyboardMarkup(buttons)


def her_v2_compact_reasoning_keyboard(
    runtime,
    provider_index: int,
    model_index: int,
) -> InlineKeyboardMarkup:
    option = runtime.backend_manager.get_her_v2_provider_options()[provider_index]
    provider = str(option["engine"])
    model = str(list(option.get("models") or [])[model_index])
    prefix = (
        f"her_model_compact_select:{provider_index}:{_her_v2_callback_token(provider)}:"
        f"{model_index}:{_her_v2_callback_token(model)}"
    )
    buttons = [
        [
            InlineKeyboardButton(
                ui_language.tr(
                    "menu.her.reasoning_value", reasoning=reasoning
                ),
                callback_data=f"{prefix}:{reasoning}",
            )
        ]
        for reasoning in ("high", "max")
    ]
    buttons.append(
        [
            InlineKeyboardButton(
                back_label(),
                callback_data=(
                    f"her_model_compact_provider:{provider_index}:"
                    f"{_her_v2_callback_token(provider)}"
                ),
            )
        ]
    )
    return InlineKeyboardMarkup(buttons)


def _her_v2_compact_callback_selection(
    runtime,
    raw_provider_index: str,
    provider_token: str,
    raw_model_index: str,
    model_token: str,
) -> tuple[int, str, int, str]:
    options = runtime.backend_manager.get_her_v2_provider_options()
    provider_index, option = _her_v2_indexed_choice(options, raw_provider_index)
    if not option.get("available"):
        raise ValueError(ui_language.tr("model.menu.stale"))
    provider = str(option["engine"])
    if provider_token != _her_v2_callback_token(provider):
        raise ValueError(ui_language.tr("model.provider_menu.stale"))
    model_index, model = _her_v2_indexed_choice(
        list(option.get("models") or []),
        raw_model_index,
    )
    model = str(model)
    if model_token != _her_v2_callback_token(model):
        raise ValueError(ui_language.tr("model.model_menu.stale"))
    return provider_index, provider, model_index, model


def her_v2_slot_model_keyboard(runtime, slot: str) -> InlineKeyboardMarkup:
    selected = _her_v2_edit_configuration(runtime)
    target = selected.target_for_slot(slot)
    options = runtime.backend_manager.get_her_v2_provider_options()
    provider_index = next(
        (
            index
            for index, candidate in enumerate(options)
            if candidate.get("engine") == target.provider
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
                    f"{_her_v2_callback_token(target.provider)}:{index}:"
                    f"{_her_v2_callback_token(model)}"
                ),
            )
        ]
        for index, model in enumerate(models)
    ]
    if selected.routing_mode == "hybrid":
        buttons.insert(
            0,
            [
                InlineKeyboardButton(
                    ui_language.tr("menu.her.change_provider"),
                    callback_data=f"her_target_providers:{slot}",
                )
            ],
        )
    buttons.append([InlineKeyboardButton(back_label(), callback_data="model_menu")])
    return InlineKeyboardMarkup(buttons)


def her_v2_slot_model_text(runtime, slot: str) -> str:
    selected = _her_v2_edit_configuration(runtime)
    target = selected.target_for_slot(slot)
    option = runtime.backend_manager._her_v2_provider_option(target.provider)
    models = list(option["models"]) if option and option["available"] else []
    current = selected.fast_model if slot == "fast" else selected.pro_model
    return runtime_menu_views.her_v2_slot_model_text(
        provider=target.provider,
        slot=slot,
        current_model=current,
        model_count=len(models),
    )


def her_v2_target_provider_keyboard(runtime, slot: str) -> InlineKeyboardMarkup:
    selected = _her_v2_edit_configuration(runtime)
    target = selected.target_for_slot(slot)
    buttons: list[list[InlineKeyboardButton]] = []
    for index, option in enumerate(runtime.backend_manager.get_her_v2_provider_options()):
        provider = str(option["engine"])
        label = selected_label(str(option["label"]), provider == target.provider)
        callback = (
            f"her_target_provider:{slot}:{index}:"
            f"{_her_v2_callback_token(provider)}"
        )
        if not option.get("available"):
            label = f"🔒 {option['label']}"
            callback = f"her_provider_locked:{index}:{_her_v2_callback_token(provider)}"
        buttons.append([InlineKeyboardButton(label, callback_data=callback)])
    buttons.append(
        [InlineKeyboardButton(back_label(), callback_data=f"her_model_slot:{slot}")]
    )
    return InlineKeyboardMarkup(buttons)


def her_v2_target_model_keyboard(
    runtime,
    slot: str,
    provider_index: int,
) -> InlineKeyboardMarkup:
    options = runtime.backend_manager.get_her_v2_provider_options()
    option = options[provider_index]
    provider = str(option["engine"])
    current = _her_v2_edit_configuration(runtime).target_for_slot(slot)
    buttons = [
        [
            InlineKeyboardButton(
                selected_label(model, provider == current.provider and model == current.model),
                callback_data=(
                    f"her_target_model:{slot}:{provider_index}:"
                    f"{_her_v2_callback_token(provider)}:{model_index}:"
                    f"{_her_v2_callback_token(model)}"
                ),
            )
        ]
        for model_index, model in enumerate(option.get("models") or [])
    ]
    buttons.append(
        [InlineKeyboardButton(back_label(), callback_data=f"her_target_providers:{slot}")]
    )
    return InlineKeyboardMarkup(buttons)


def _her_v2_route(raw: Route | str) -> Route:
    if isinstance(raw, Route):
        return raw
    normalized = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    alias = HER_V2_ROUTE_ALIASES.get(normalized)
    return alias if alias is not None else Route(normalized)


def _her_v2_route_slot_label(slot: str) -> str:
    key = f"menu.her.slot.{slot}"
    translated = ui_language.tr(key)
    return slot if translated == key else translated


def _her_v2_route_label(route: Route) -> str:
    key = f"menu.her.route.{route.value}"
    translated = ui_language.tr(key)
    return HER_V2_ROUTE_LABELS[route] if translated == key else translated


def _her_v2_route_effective_model(runtime, route: Route) -> str:
    target = _her_v2_edit_configuration(runtime).target_for_route(route)
    return f"{target.provider} / {target.model}"


def _her_v2_route_effective_reasoning(runtime, route: Route) -> str:
    selected = _her_v2_edit_configuration(runtime)
    return selected.reasoning_for_route(
        runtime.backend_manager._her_v2_base_config(),
        route,
    )


def her_v2_routes_text(runtime) -> str:
    selected = _her_v2_edit_configuration(runtime)
    return runtime_menu_views.her_v2_routes_text(
        route_count=len(HER_V2_ROUTE_ORDER),
        explicit_reasoning_count=len(selected.route_reasoning),
        custom_target_count=len(selected.route_targets),
        draft=runtime.backend_manager.has_her_v2_configuration_draft(),
    )


def her_v2_routes_keyboard(runtime) -> InlineKeyboardMarkup:
    selected = _her_v2_edit_configuration(runtime)
    buttons = [
        [
            InlineKeyboardButton(
                (
                    f"{_her_v2_route_label(route)} · "
                    f"{_her_v2_route_slot_label(selected.route_target_mode(route))} · "
                    f"{_her_v2_route_effective_reasoning(runtime, route)}"
                ),
                callback_data=f"her_route_menu:{route.value}",
            )
        ]
        for route in HER_V2_ROUTE_ORDER
    ]
    buttons.append([InlineKeyboardButton(back_label(), callback_data="model_menu")])
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
    selected = _her_v2_edit_configuration(runtime)
    current_slot = selected.route_target_mode(parsed)
    slots = ["fast"] if parsed is Route.DIRECT else ["fast", "pro"]
    buttons = [
        [
            InlineKeyboardButton(
                selected_label(
                    ui_language.tr(
                        "menu.her.route_model",
                        slot=_her_v2_route_slot_label(slot),
                    ),
                    slot == current_slot,
                ),
                callback_data=f"her_route_slot:{parsed.value}:{slot}",
            )
            for slot in slots
        ]
    ]
    if selected.routing_mode == "hybrid" and parsed is not Route.DIRECT:
        buttons.append(
            [
                InlineKeyboardButton(
                    selected_label(
                        ui_language.tr(
                            "menu.her.route_model",
                            slot=_her_v2_route_slot_label("custom"),
                        ),
                        current_slot == "custom",
                    ),
                    callback_data=f"her_route_custom:{parsed.value}",
                )
            ]
        )
    current_reasoning, choices = _her_v2_route_reasoning_choices(runtime, parsed)
    explicit = selected.route_reasoning.get(parsed.value)
    for index, value in enumerate(choices):
        is_selected = (
            value == explicit if explicit is not None else value == "inherit"
        )
        label = (
            ui_language.tr(
                "menu.her.reasoning_inherit", reasoning=current_reasoning
            )
            if value == "inherit"
            else ui_language.tr("menu.her.reasoning_value", reasoning=value)
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
    buttons.append([InlineKeyboardButton(back_label(), callback_data="her_routes")])
    return InlineKeyboardMarkup(buttons)


def her_v2_route_text(runtime, route: Route | str) -> str:
    parsed = _her_v2_route(route)
    selected = _her_v2_edit_configuration(runtime)
    slot = selected.route_target_mode(parsed)
    return runtime_menu_views.her_v2_route_text(
        label=_her_v2_route_label(parsed),
        model_slot=_her_v2_route_slot_label(slot),
        effective_model=_her_v2_route_effective_model(runtime, parsed),
        reasoning=_her_v2_route_effective_reasoning(runtime, parsed),
        reasoning_inherited=parsed.value not in selected.route_reasoning,
    )


def her_v2_route_provider_keyboard(runtime, route: Route | str) -> InlineKeyboardMarkup:
    parsed = _her_v2_route(route)
    selected = _her_v2_edit_configuration(runtime)
    current = selected.target_for_route(parsed)
    buttons: list[list[InlineKeyboardButton]] = []
    for index, option in enumerate(runtime.backend_manager.get_her_v2_provider_options()):
        provider = str(option["engine"])
        label = selected_label(str(option["label"]), provider == current.provider)
        callback = (
            f"her_route_provider:{parsed.value}:{index}:"
            f"{_her_v2_callback_token(provider)}"
        )
        if not option.get("available"):
            label = f"🔒 {option['label']}"
            callback = f"her_provider_locked:{index}:{_her_v2_callback_token(provider)}"
        buttons.append([InlineKeyboardButton(label, callback_data=callback)])
    buttons.append(
        [InlineKeyboardButton(back_label(), callback_data=f"her_route_menu:{parsed.value}")]
    )
    return InlineKeyboardMarkup(buttons)


def her_v2_route_model_keyboard(
    runtime,
    route: Route | str,
    provider_index: int,
) -> InlineKeyboardMarkup:
    parsed = _her_v2_route(route)
    options = runtime.backend_manager.get_her_v2_provider_options()
    option = options[provider_index]
    provider = str(option["engine"])
    current = _her_v2_edit_configuration(runtime).target_for_route(parsed)
    buttons = [
        [
            InlineKeyboardButton(
                selected_label(model, provider == current.provider and model == current.model),
                callback_data=(
                    f"her_route_model:{parsed.value}:{provider_index}:"
                    f"{_her_v2_callback_token(provider)}:{model_index}:"
                    f"{_her_v2_callback_token(model)}"
                ),
            )
        ]
        for model_index, model in enumerate(option.get("models") or [])
    ]
    buttons.append(
        [
            InlineKeyboardButton(
                back_label(),
                callback_data=f"her_route_custom:{parsed.value}",
            )
        ]
    )
    return InlineKeyboardMarkup(buttons)


def apply_her_v2_configuration(runtime, selected) -> str | None:
    if runtime.config.active_backend != HER_V2_ENGINE:
        return ui_language.tr("menu.her.configuration_active_only")
    try:
        runtime.backend_manager.apply_her_v2_configuration(selected)
    except (OSError, TypeError, ValueError) as exc:
        return str(exc)
    return None


def save_her_v2_candidate(runtime, selected) -> str | None:
    """Stage Hybrid edits; preserve immediate Single-provider compatibility."""

    if runtime.config.active_backend != HER_V2_ENGINE:
        return ui_language.tr("menu.her.configuration_active_only")
    try:
        if (
            selected.routing_mode == "hybrid"
            or runtime.backend_manager.has_her_v2_configuration_draft()
        ):
            runtime.backend_manager.stage_her_v2_configuration(selected)
        else:
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
                current=(
                    f"<b>{html.escape(ui_language.tr('menu.her.managed'))}</b>"
                ),
                facts=[
                    f"<b>{html.escape(ui_language.tr('common.backend'))}</b> · "
                    "<code>her-v2</code>",
                    f"<b>{html.escape(ui_language.tr('common.mode'))}</b> · "
                    f"<code>{html.escape(managed_mode)}</code>",
                ],
                consequence=ui_language.tr("menu.her.managed_effect"),
                action=ui_language.tr(
                    "menu.her.managed_action",
                    commands=managed_commands[managed_mode],
                ),
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
        if provider.casefold() == "hybrid":
            runtime.backend_manager.begin_her_v2_hybrid_draft()
            await runtime._reply_text(
                update,
                her_v2_model_menu_text(runtime),
                parse_mode="HTML",
                reply_markup=her_v2_model_keyboard(runtime),
            )
            return
        selected = runtime.backend_manager.prepare_her_v2_provider(
            provider,
            current=_her_v2_edit_configuration(runtime),
        )
        error = apply_her_v2_configuration(runtime, selected)
    except (OSError, TypeError, ValueError) as exc:
        error = str(exc)
    if error:
        text = her_v2_provider_menu_text(runtime)
        reason = error
        text += "\n\n❌ " + ui_language.tr(
            "menu.her.provider_unavailable",
            provider=html.escape(provider),
            reason=html.escape(reason),
        )
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


async def _cmd_her_v2_compact(runtime, update, args: list[str]) -> None:
    from orchestrator.context_compaction import configure_route, load_route_config

    if not args or args[0].strip().lower() in {"status", "show", "info"}:
        await runtime._reply_text(
            update,
            her_v2_compact_text(runtime),
            parse_mode="HTML",
            reply_markup=her_v2_compact_keyboard(runtime),
        )
        return

    action = args[0].strip().lower().replace("-", "_")
    try:
        current = load_route_config(runtime)
        if action in {"inherit", "inherit_pro", "pro", "quick", "inherit_quick"}:
            tier = args[1] if len(args) == 2 else current.timeout_tier
            if len(args) > 2:
                raise ValueError(ui_language.tr("model.compact.optional_tier"))
            configure_route(runtime, mode="inherit_quick", timeout_tier=tier)
        elif action in {"tier", "timeout"} and len(args) == 2:
            configure_route(runtime, mode=current.mode, timeout_tier=args[1])
        else:
            raise ValueError(ui_language.tr("model.compact_usage"))
    except (OSError, TypeError, ValueError) as exc:
        await runtime._reply_text(
            update,
            ui_language.tr(
                "model.compact_unchanged", reason=html.escape(str(exc))
            ),
            parse_mode="HTML",
        )
        return

    await runtime._reply_text(
        update,
        her_v2_compact_text(runtime),
        parse_mode="HTML",
        reply_markup=her_v2_compact_keyboard(runtime),
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
    if action == "apply" and len(args) == 1:
        try:
            runtime.backend_manager.apply_her_v2_configuration_draft()
        except (OSError, TypeError, ValueError) as exc:
            await runtime._reply_text(
                update,
                ui_language.tr("model.draft_apply_failed", reason=str(exc)),
            )
            return
        await runtime._reply_text(
            update,
            her_v2_model_menu_text(runtime),
            parse_mode="HTML",
            reply_markup=her_v2_model_keyboard(runtime),
        )
        return
    if action in {"discard", "cancel"} and len(args) == 1:
        try:
            runtime.backend_manager.discard_her_v2_configuration_draft()
        except OSError as exc:
            await runtime._reply_text(
                update,
                ui_language.tr("model.draft_discard_failed", reason=str(exc)),
            )
            return
        await runtime._reply_text(
            update,
            her_v2_model_menu_text(runtime),
            parse_mode="HTML",
            reply_markup=her_v2_model_keyboard(runtime),
        )
        return
    if action == "compact":
        await _cmd_her_v2_compact(runtime, update, args[1:])
        return
    if action in {"quick", "fast", "pro"}:
        slot = "fast" if action in {"quick", "fast"} else "pro"
    else:
        slot = ""
    if slot and len(args) == 1:
        selected = _her_v2_edit_configuration(runtime)
        if selected.routing_mode == "hybrid":
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "model.select_provider_for_slot",
                    slot=_her_v2_route_slot_label(slot),
                ),
                reply_markup=her_v2_target_provider_keyboard(runtime, slot),
            )
            return
        await runtime._reply_text(
            update,
            her_v2_slot_model_text(runtime, slot),
            parse_mode="HTML",
            reply_markup=her_v2_slot_model_keyboard(runtime, slot),
        )
        return
    if slot and len(args) == 2:
        try:
            selected = runtime.backend_manager.prepare_her_v2_model(
                slot,
                args[1],
                current=_her_v2_edit_configuration(runtime),
            )
            error = save_her_v2_candidate(runtime, selected)
        except (TypeError, ValueError) as exc:
            error = str(exc)
        if error:
            await runtime._reply_text(
                update, ui_language.tr("model.change_failed", reason=error)
            )
            return
        await runtime._reply_text(
            update,
            her_v2_model_menu_text(runtime),
            parse_mode="HTML",
            reply_markup=her_v2_model_keyboard(runtime),
        )
        return
    if slot and len(args) == 3:
        try:
            selected = runtime.backend_manager.prepare_her_v2_model(
                slot,
                args[2],
                provider=args[1],
                current=_her_v2_edit_configuration(runtime),
            )
            error = save_her_v2_candidate(runtime, selected)
        except (TypeError, ValueError) as exc:
            error = str(exc)
        if error:
            await runtime._reply_text(
                update, ui_language.tr("model.target_failed", reason=error)
            )
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
    if action == "route" and len(args) in {2, 3, 5}:
        try:
            route = _her_v2_route(args[1])
            if len(args) == 5 and args[2].strip().lower() == "custom":
                selected = runtime.backend_manager.prepare_her_v2_route_target(
                    route.value,
                    args[3],
                    args[4],
                    current=_her_v2_edit_configuration(runtime),
                )
                error = save_her_v2_candidate(runtime, selected)
            elif len(args) == 5:
                raise ValueError(ui_language.tr("model.custom_syntax"))
            elif len(args) == 3:
                selected = runtime.backend_manager.prepare_her_v2_route_model_slot(
                    route.value,
                    args[2],
                    current=_her_v2_edit_configuration(runtime),
                )
                error = save_her_v2_candidate(runtime, selected)
            else:
                error = None
        except (TypeError, ValueError) as exc:
            error = str(exc)
            route = None
        if error or route is None:
            await runtime._reply_text(
                update, ui_language.tr("model.route_failed", reason=error)
            )
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
                    None if args[2].strip().lower() == "inherit" else args[2],
                    current=_her_v2_edit_configuration(runtime),
                )
                error = save_her_v2_candidate(runtime, selected)
            else:
                error = None
        except (TypeError, ValueError) as exc:
            error = str(exc)
            route = None
        if error or route is None:
            await runtime._reply_text(
                update, ui_language.tr("model.reasoning_failed", reason=error)
            )
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
        ui_language.tr("model.usage"),
        parse_mode="HTML",
    )


async def cmd_model(runtime, update, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if not runtime.backend_manager.current_backend:
        return
    if runtime.backend_manager.agent_mode == "wrapper":
        await runtime._reply_text(
            update,
            ui_language.tr("model.managed.wrapper"),
        )
        return
    if runtime.backend_manager.agent_mode == "audit":
        await runtime._reply_text(
            update,
            ui_language.tr("model.managed.audit"),
        )
        return
    if runtime.backend_manager.agent_mode == "dual-brain":
        await runtime._reply_text(
            update,
            ui_language.tr("model.managed.dual_brain"),
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
                ui_language.tr("model.unknown", model=requested),
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
        (
            "her_provider",
            "her_model",
            "her_route",
            "her_reasoning",
            "her_target",
        )
    ) or data in {"model_menu", "her_routes"}
    if (
        her_v2_control
        and runtime.config.active_backend == HER_V2_ENGINE
        and runtime.backend_manager.agent_mode in {"wrapper", "audit", "dual-brain"}
    ):
        await query.answer(
            ui_language.tr("model.managed_by_mode"),
            show_alert=True,
        )
        return
    if (
        data.startswith("her_model_compact")
        and runtime.config.active_backend != HER_V2_ENGINE
    ):
        await query.answer(
            ui_language.tr("model.compact.active_only"),
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
                        f"<b>{html.escape(ui_language.tr('common.backend'))}</b> · "
                        f"<code>{html.escape(runtime.config.active_backend)}</code>"
                    ],
                    consequence=ui_language.tr("model.backend_unchanged_effect"),
                    action=(
                        ui_language.tr("model.backend_unchanged.changed_elsewhere")
                        if current_mode != expected_mode
                        else ui_language.tr("model.backend_unchanged.current")
                    ),
                ),
                parse_mode="HTML",
            )
        elif data == "her_model_compact":
            if runtime.config.active_backend != HER_V2_ENGINE:
                await query.answer(
                    ui_language.tr("model.compact.active_only"),
                    show_alert=True,
                )
                return
            await query.edit_message_text(
                her_v2_compact_text(runtime),
                parse_mode="HTML",
                reply_markup=her_v2_compact_keyboard(runtime),
            )
        elif data == "her_model_apply":
            try:
                runtime.backend_manager.apply_her_v2_configuration_draft()
            except (OSError, TypeError, ValueError) as exc:
                await query.answer(str(exc), show_alert=True)
                return
            await query.edit_message_text(
                her_v2_model_menu_text(runtime),
                parse_mode="HTML",
                reply_markup=her_v2_model_keyboard(runtime),
            )
        elif data == "her_model_discard":
            try:
                runtime.backend_manager.discard_her_v2_configuration_draft()
            except OSError as exc:
                await query.answer(str(exc), show_alert=True)
                return
            await query.edit_message_text(
                her_v2_model_menu_text(runtime),
                parse_mode="HTML",
                reply_markup=her_v2_model_keyboard(runtime),
            )
        elif data == "her_model_compact_providers" or data.startswith(
            (
                "her_model_compact_mode:",
                "her_model_compact_provider:",
                "her_model_compact_model:",
                "her_model_compact_reasoning:",
                "her_model_compact_select:",
                "her_model_compact_confirm:",
            )
        ):
            await query.answer(
                ui_language.tr("model.compact.follows_quick"),
                show_alert=True,
            )
            return
        elif data.startswith("her_model_compact_tier:"):
            from orchestrator.context_compaction import (
                configure_route,
                load_route_config,
            )

            tier = data.split(":", 1)[1]
            try:
                current = load_route_config(runtime)
                configure_route(
                    runtime,
                    mode=current.mode,
                    provider=current.provider,
                    model=current.model,
                    reasoning=current.reasoning,
                    timeout_tier=tier,
                    confirmed_cross_provider=current.cross_provider_confirmed,
                )
            except (OSError, TypeError, ValueError) as exc:
                await query.answer(str(exc), show_alert=True)
                return
            await query.edit_message_text(
                her_v2_compact_text(runtime),
                parse_mode="HTML",
                reply_markup=her_v2_compact_keyboard(runtime),
            )
        elif data == "her_provider_hybrid":
            if runtime.config.active_backend != HER_V2_ENGINE:
                await query.answer(
                    ui_language.tr("model.control.active_only"),
                    show_alert=True,
                )
                return
            try:
                runtime.backend_manager.begin_her_v2_hybrid_draft()
            except (OSError, TypeError, ValueError) as exc:
                await query.answer(str(exc), show_alert=True)
                return
            await query.edit_message_text(
                her_v2_model_menu_text(runtime),
                parse_mode="HTML",
                reply_markup=her_v2_model_keyboard(runtime),
            )
        elif data.startswith("her_provider_locked:"):
            if runtime.config.active_backend != HER_V2_ENGINE:
                await query.answer(
                    ui_language.tr("model.control.active_only"),
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
                await query.answer(
                    ui_language.tr("model.provider_option_gone"), show_alert=True
                )
                return
            await query.answer(
                ui_language.tr(
                    "model.provider_unavailable_reason",
                    provider=option["label"],
                    reason=option.get("reason")
                    or ui_language.tr("common.unavailable"),
                ),
                show_alert=True,
            )
            return
        elif data.startswith("her_provider:"):
            if runtime.config.active_backend != HER_V2_ENGINE:
                await query.answer(
                    ui_language.tr("model.control.active_only"),
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
                    raise ValueError(ui_language.tr("model.menu.stale"))
                selected = runtime.backend_manager.prepare_her_v2_provider(
                    str(option["engine"]),
                    current=_her_v2_edit_configuration(runtime),
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
                    ui_language.tr("model.control.active_only"),
                    show_alert=True,
                )
                return
            slot = data.split(":", 1)[1]
            if slot not in {"fast", "pro"}:
                await query.answer(ui_language.tr("model.slot.invalid"), show_alert=True)
                return
            selected = _her_v2_edit_configuration(runtime)
            if selected.routing_mode == "hybrid":
                await query.edit_message_text(
                    f"🧠 <b>{html.escape(ui_language.tr('model.select_target_provider'))}</b>\n\n"
                    f"<b>{html.escape(ui_language.tr('model.slot'))}</b> · "
                    f"<code>{html.escape(_her_v2_route_slot_label(slot))}</code>",
                    parse_mode="HTML",
                    reply_markup=her_v2_target_provider_keyboard(runtime, slot),
                )
                return
            await query.edit_message_text(
                her_v2_slot_model_text(runtime, slot),
                parse_mode="HTML",
                reply_markup=her_v2_slot_model_keyboard(runtime, slot),
            )
        elif data.startswith("her_target_providers:"):
            slot = data.split(":", 1)[1]
            if slot not in {"fast", "pro"}:
                await query.answer(ui_language.tr("model.slot.invalid"), show_alert=True)
                return
            await query.edit_message_text(
                f"🧠 <b>{html.escape(ui_language.tr('model.select_target_provider'))}</b>\n\n"
                f"<b>{html.escape(ui_language.tr('model.slot'))}</b> · "
                f"<code>{html.escape(_her_v2_route_slot_label(slot))}</code>",
                parse_mode="HTML",
                reply_markup=her_v2_target_provider_keyboard(runtime, slot),
            )
        elif data.startswith("her_target_provider:"):
            try:
                _, slot, raw_index, token = data.split(":", 3)
                if slot not in {"fast", "pro"}:
                    raise ValueError(ui_language.tr("model.slot.invalid"))
                provider_index, option = _her_v2_indexed_choice(
                    runtime.backend_manager.get_her_v2_provider_options(),
                    raw_index,
                )
                provider = str(option["engine"])
                if (
                    token != _her_v2_callback_token(provider)
                    or not option.get("available")
                ):
                    raise ValueError(ui_language.tr("model.menu.stale"))
            except (IndexError, TypeError, ValueError) as exc:
                await query.answer(str(exc), show_alert=True)
                return
            await query.edit_message_text(
                f"🧠 <b>{html.escape(ui_language.tr('model.select_target_model'))}</b>\n\n"
                f"<b>{html.escape(ui_language.tr('common.provider'))}</b> · <code>{html.escape(provider)}</code>",
                parse_mode="HTML",
                reply_markup=her_v2_target_model_keyboard(
                    runtime,
                    slot,
                    provider_index,
                ),
            )
        elif data.startswith("her_target_model:"):
            try:
                (
                    _,
                    slot,
                    raw_provider_index,
                    provider_token,
                    raw_model_index,
                    model_token,
                ) = data.split(":", 5)
                options = runtime.backend_manager.get_her_v2_provider_options()
                _provider_index, option = _her_v2_indexed_choice(
                    options,
                    raw_provider_index,
                )
                provider = str(option["engine"])
                if (
                    provider_token != _her_v2_callback_token(provider)
                    or not option.get("available")
                ):
                    raise ValueError(ui_language.tr("model.menu.stale"))
                _model_index, model = _her_v2_indexed_choice(
                    list(option.get("models") or []),
                    raw_model_index,
                )
                if model_token != _her_v2_callback_token(model):
                    raise ValueError(ui_language.tr("model.menu.stale"))
                candidate = runtime.backend_manager.prepare_her_v2_model(
                    slot,
                    model,
                    provider=provider,
                    current=_her_v2_edit_configuration(runtime),
                )
                error = save_her_v2_candidate(runtime, candidate)
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
        elif data.startswith("her_model:"):
            if runtime.config.active_backend != HER_V2_ENGINE:
                await query.answer(
                    ui_language.tr("model.control.active_only"),
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
                selected = _her_v2_edit_configuration(runtime)
                target = selected.target_for_slot(slot)
                options = runtime.backend_manager.get_her_v2_provider_options()
                _provider_index, option = _her_v2_indexed_choice(
                    options,
                    raw_provider_index,
                )
                if (
                    option.get("engine") != target.provider
                    or provider_token != _her_v2_callback_token(option["engine"])
                ):
                    raise ValueError(ui_language.tr("model.provider_menu.stale"))
                _model_index, model = _her_v2_indexed_choice(
                    list(option["models"]),
                    raw_model_index,
                )
                if model_token != _her_v2_callback_token(model):
                    raise ValueError(ui_language.tr("model.model_menu.stale"))
                candidate = runtime.backend_manager.prepare_her_v2_model(
                    slot,
                    model,
                    current=selected,
                )
                error = save_her_v2_candidate(runtime, candidate)
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
                    ui_language.tr("model.control.active_only"),
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
                    ui_language.tr("model.control.active_only"),
                    show_alert=True,
                )
                return
            try:
                route = _her_v2_route(data.split(":", 1)[1])
            except ValueError:
                await query.answer(ui_language.tr("model.route.invalid"), show_alert=True)
                return
            await query.edit_message_text(
                her_v2_route_text(runtime, route),
                parse_mode="HTML",
                reply_markup=her_v2_route_keyboard(runtime, route),
            )
        elif data.startswith("her_route_custom:"):
            try:
                route = _her_v2_route(data.split(":", 1)[1])
                if _her_v2_edit_configuration(runtime).routing_mode != "hybrid":
                    raise ValueError(ui_language.tr("model.custom_requires_hybrid"))
            except ValueError as exc:
                await query.answer(str(exc), show_alert=True)
                return
            await query.edit_message_text(
                f"🧭 <b>{html.escape(ui_language.tr('model.select_custom_provider'))}</b>\n\n"
                f"<b>{html.escape(ui_language.tr('common.route'))}</b> · {html.escape(_her_v2_route_label(route))}",
                parse_mode="HTML",
                reply_markup=her_v2_route_provider_keyboard(runtime, route),
            )
        elif data.startswith("her_route_provider:"):
            try:
                _, raw_route, raw_index, token = data.split(":", 3)
                route = _her_v2_route(raw_route)
                provider_index, option = _her_v2_indexed_choice(
                    runtime.backend_manager.get_her_v2_provider_options(),
                    raw_index,
                )
                provider = str(option["engine"])
                if (
                    token != _her_v2_callback_token(provider)
                    or not option.get("available")
                ):
                    raise ValueError(ui_language.tr("model.menu.stale"))
            except (IndexError, TypeError, ValueError) as exc:
                await query.answer(str(exc), show_alert=True)
                return
            await query.edit_message_text(
                f"🧭 <b>{html.escape(ui_language.tr('model.select_custom_model'))}</b>\n\n"
                f"<b>{html.escape(ui_language.tr('common.provider'))}</b> · <code>{html.escape(provider)}</code>",
                parse_mode="HTML",
                reply_markup=her_v2_route_model_keyboard(
                    runtime,
                    route,
                    provider_index,
                ),
            )
        elif data.startswith("her_route_model:"):
            try:
                (
                    _,
                    raw_route,
                    raw_provider_index,
                    provider_token,
                    raw_model_index,
                    model_token,
                ) = data.split(":", 5)
                route = _her_v2_route(raw_route)
                options = runtime.backend_manager.get_her_v2_provider_options()
                _provider_index, option = _her_v2_indexed_choice(
                    options,
                    raw_provider_index,
                )
                provider = str(option["engine"])
                if (
                    provider_token != _her_v2_callback_token(provider)
                    or not option.get("available")
                ):
                    raise ValueError(ui_language.tr("model.menu.stale"))
                _model_index, model = _her_v2_indexed_choice(
                    list(option.get("models") or []),
                    raw_model_index,
                )
                if model_token != _her_v2_callback_token(model):
                    raise ValueError(ui_language.tr("model.menu.stale"))
                candidate = runtime.backend_manager.prepare_her_v2_route_target(
                    route.value,
                    provider,
                    model,
                    current=_her_v2_edit_configuration(runtime),
                )
                error = save_her_v2_candidate(runtime, candidate)
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
        elif data.startswith("her_route_slot:"):
            if runtime.config.active_backend != HER_V2_ENGINE:
                await query.answer(
                    ui_language.tr("model.control.active_only"),
                    show_alert=True,
                )
                return
            try:
                _, raw_route, slot = data.split(":", 2)
                route = _her_v2_route(raw_route)
                candidate = runtime.backend_manager.prepare_her_v2_route_model_slot(
                    route.value,
                    slot,
                    current=_her_v2_edit_configuration(runtime),
                )
                error = save_her_v2_candidate(runtime, candidate)
            except (TypeError, ValueError) as exc:
                error = str(exc)
                route = None
            if error or route is None:
                await query.answer(
                    error or ui_language.tr("model.route.invalid"), show_alert=True
                )
                return
            await query.edit_message_text(
                her_v2_route_text(runtime, route),
                parse_mode="HTML",
                reply_markup=her_v2_route_keyboard(runtime, route),
            )
        elif data.startswith("her_route_reasoning:"):
            if runtime.config.active_backend != HER_V2_ENGINE:
                await query.answer(
                    ui_language.tr("model.control.active_only"),
                    show_alert=True,
                )
                return
            try:
                _, raw_route, raw_index, token = data.split(":", 3)
                route = _her_v2_route(raw_route)
                _current, choices = _her_v2_route_reasoning_choices(runtime, route)
                _index, reasoning = _her_v2_indexed_choice(choices, raw_index)
                if token != _her_v2_callback_token(reasoning):
                    raise ValueError(ui_language.tr("model.menu.stale"))
                candidate = runtime.backend_manager.prepare_her_v2_route_reasoning(
                    route.value,
                    None if reasoning == "inherit" else reasoning,
                    current=_her_v2_edit_configuration(runtime),
                )
                error = save_her_v2_candidate(runtime, candidate)
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
                ui_language.tr("model.old_reasoning_retired"),
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
                    ui_language.tr("model.use_her_controls"),
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
                    ui_language.tr("model.unavailable_provider"),
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
                await query.answer(ui_language.tr("model.invalid_callback"), show_alert=True)
                return
            _, target_engine, mode = parts
            if not is_selectable_backend(target_engine):
                await query.answer(
                    ui_language.tr("model.provider_her_only"),
                    show_alert=True,
                )
                return
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
                await query.answer(ui_language.tr("model.invalid_callback"), show_alert=True)
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
                await query.answer(ui_language.tr("model.effort_kept"))
                return
            if requested in runtime._get_available_efforts():
                runtime._set_active_effort(requested)
                if source in {"backend", "model"}:
                    await query.edit_message_text(
                        runtime._build_model_configuration_summary(),
                        parse_mode="HTML",
                    )
                else:
                    if runtime.config.active_backend == HER_V2_ENGINE:
                        from orchestrator.her_v2.models import effort_display_label

                        switched_text = ui_language.tr(
                            "model.execution_mode_switched",
                            mode=effort_display_label(requested),
                        )
                    else:
                        switched_text = ui_language.tr(
                            "model.effort_switched", effort=requested
                        )
                    await query.edit_message_text(
                        switched_text,
                        reply_markup=runtime._effort_keyboard(requested),
                    )
    except Exception as exc:
        runtime.error_logger.exception("callback_model error")
        await query.answer(
            ui_language.tr("model.callback_error", reason=str(exc)),
            show_alert=True,
        )
        return
    await query.answer()
