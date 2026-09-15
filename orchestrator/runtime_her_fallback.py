from __future__ import annotations

import html
from dataclasses import replace
from hashlib import blake2s
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import runtime_model_selection, ui_language
from orchestrator.command_ui import refresh_label, setting_card, status_label
from orchestrator.flexible_backend_registry import HER_V2_ENGINE


def _token(value: Any) -> str:
    return blake2s(str(value).encode("utf-8"), digest_size=5).hexdigest()


def _model_class(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized in {"fast", "quick", "light"}:
        return "light"
    if normalized == "pro":
        return "pro"
    raise ValueError(ui_language.tr("fallback.error.class"))


def _level(value: str) -> int:
    normalized = str(value or "").strip().casefold().replace("-", "")
    if normalized in {"1", "l1", "level1"}:
        return 1
    if normalized in {"2", "l2", "level2"}:
        return 2
    raise ValueError(ui_language.tr("fallback.error.level"))


def _target_text(selected, level: int, model_class: str) -> str:
    target = selected.fallback_targets.get(level, {}).get(model_class)
    if target is None:
        return ui_language.tr("common.none")
    return f"<code>{html.escape(target.provider)}/{html.escape(target.model)}</code>"


def fallback_menu_text(runtime) -> str:
    selected = runtime.backend_manager.get_her_v2_configuration()
    facts = [
        ui_language.tr(
            "fallback.fact.slot",
            level=level,
            model_class=ui_language.tr(f"fallback.class.{model_class}"),
            target=_target_text(selected, level, model_class),
        )
        for level in (1, 2)
        for model_class in ("light", "pro")
    ]
    return setting_card(
        "🛟",
        ui_language.tr("fallback.title"),
        current=f"<b>{html.escape(status_label(selected.fallback_enabled))}</b>",
        facts=facts,
        consequence=ui_language.tr("fallback.effect"),
        action=ui_language.tr("fallback.action"),
    )


def fallback_menu_keyboard(runtime) -> InlineKeyboardMarkup:
    selected = runtime.backend_manager.get_her_v2_configuration()
    toggle_key = "fallback.button.disable" if selected.fallback_enabled else "fallback.button.enable"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    ui_language.tr(toggle_key),
                    callback_data="fallback:toggle",
                )
            ],
            [
                InlineKeyboardButton(
                    ui_language.tr("fallback.button.slot", level=1, model_class="Light"),
                    callback_data="fallback:slot:1:light",
                ),
                InlineKeyboardButton(
                    ui_language.tr("fallback.button.slot", level=1, model_class="Pro"),
                    callback_data="fallback:slot:1:pro",
                ),
            ],
            [
                InlineKeyboardButton(
                    ui_language.tr("fallback.button.slot", level=2, model_class="Light"),
                    callback_data="fallback:slot:2:light",
                ),
                InlineKeyboardButton(
                    ui_language.tr("fallback.button.slot", level=2, model_class="Pro"),
                    callback_data="fallback:slot:2:pro",
                ),
            ],
            [
                InlineKeyboardButton(
                    ui_language.tr("fallback.button.clear"),
                    callback_data="fallback:clear:all",
                ),
                InlineKeyboardButton(
                    refresh_label(),
                    callback_data="fallback:refresh",
                ),
            ],
        ]
    )


def _eligible_options(runtime, level: int, model_class: str) -> list[dict[str, Any]]:
    selected = runtime.backend_manager.get_her_v2_configuration()
    slot = "fast" if model_class == "light" else "pro"
    primary = selected.target_for_slot(slot)
    return [
        option
        for option in runtime.backend_manager.get_her_v2_provider_options()
        if option.get("available")
        and (
            (level == 1 and option.get("engine") == primary.provider)
            or (level == 2 and option.get("engine") != primary.provider)
        )
    ]


def _provider_keyboard(runtime, level: int, model_class: str) -> InlineKeyboardMarkup:
    options = _eligible_options(runtime, level, model_class)
    buttons = [
        [
            InlineKeyboardButton(
                str(option.get("label") or option["engine"]),
                callback_data=(
                    f"fallback:provider:{level}:{model_class}:{index}:"
                    f"{_token(option['engine'])}"
                ),
            )
        ]
        for index, option in enumerate(options)
    ]
    buttons.append(
        [
            InlineKeyboardButton(
                ui_language.tr("fallback.button.clear_slot"),
                callback_data=f"fallback:clear:{level}:{model_class}",
            )
        ]
    )
    buttons.append(
        [InlineKeyboardButton(ui_language.tr("common.back"), callback_data="fallback:refresh")]
    )
    return InlineKeyboardMarkup(buttons)


def _provider_text(level: int, model_class: str) -> str:
    return setting_card(
        "🛟",
        ui_language.tr("fallback.provider.title"),
        current=ui_language.tr(
            "fallback.provider.current",
            level=level,
            model_class=html.escape(ui_language.tr(f"fallback.class.{model_class}")),
        ),
        consequence=ui_language.tr(f"fallback.level{level}.effect"),
        action=ui_language.tr("fallback.provider.action"),
    )


def _model_keyboard(
    runtime,
    level: int,
    model_class: str,
    provider_index: int,
) -> InlineKeyboardMarkup:
    options = _eligible_options(runtime, level, model_class)
    option = options[provider_index]
    provider = str(option["engine"])
    buttons = [
        [
            InlineKeyboardButton(
                str(model),
                callback_data=(
                    f"fallback:model:{level}:{model_class}:{provider_index}:"
                    f"{_token(provider)}:{index}:{_token(model)}"
                ),
            )
        ]
        for index, model in enumerate(option.get("models") or [])
    ]
    buttons.append(
        [
            InlineKeyboardButton(
                ui_language.tr("common.back"),
                callback_data=f"fallback:slot:{level}:{model_class}",
            )
        ]
    )
    return InlineKeyboardMarkup(buttons)


def _model_text(level: int, model_class: str, provider: str) -> str:
    return setting_card(
        "🛟",
        ui_language.tr("fallback.model.title"),
        current=(
            f"L{level} · {html.escape(ui_language.tr(f'fallback.class.{model_class}'))}"
            f" · <code>{html.escape(provider)}</code>"
        ),
        consequence=ui_language.tr(f"fallback.level{level}.effect"),
        action=ui_language.tr("fallback.model.action"),
    )


def _apply(runtime, selected) -> str | None:
    return runtime_model_selection.apply_her_v2_configuration(runtime, selected)


def _clear(runtime, selected, scope: str):
    levels = {
        int(level): dict(targets)
        for level, targets in selected.fallback_targets.items()
    }
    if scope == "all":
        return replace(selected, fallback_enabled=False, fallback_targets={})
    level = _level(scope)
    levels.pop(level, None)
    return replace(selected, fallback_targets=levels)


async def cmd_fallback(runtime, update, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if runtime.config.active_backend != HER_V2_ENGINE:
        await runtime._reply_text(update, ui_language.tr("fallback.active_only"))
        return
    args = [str(item).strip() for item in (context.args or [])]
    if not args or args[0].casefold() in {"status", "menu", "show"}:
        await runtime._reply_text(
            update,
            fallback_menu_text(runtime),
            parse_mode="HTML",
            reply_markup=fallback_menu_keyboard(runtime),
        )
        return
    try:
        selected = runtime.backend_manager.get_her_v2_configuration()
        action = args[0].casefold()
        if action in {"on", "off"} and len(args) == 1:
            selected = runtime.backend_manager.prepare_her_v2_fallback(
                enabled=action == "on",
                current=selected,
            )
        elif action == "clear" and len(args) in {1, 2}:
            selected = _clear(runtime, selected, args[1] if len(args) == 2 else "all")
        else:
            if action in {"l1", "level1", "l2", "level2"}:
                if len(args) != 4:
                    raise ValueError(ui_language.tr("fallback.usage"))
                level = _level(action)
                provider, model, model_class = args[1], args[2], _model_class(args[3])
            else:
                if len(args) != 3:
                    raise ValueError(ui_language.tr("fallback.usage"))
                provider, model, model_class = args[0], args[1], _model_class(args[2])
                requested = provider.casefold()
                option = next(
                    (
                        candidate
                        for candidate in runtime.backend_manager.get_her_v2_provider_options()
                        if requested
                        in {
                            str(candidate.get("name") or "").casefold(),
                            str(candidate.get("engine") or "").casefold(),
                            str(candidate.get("label") or "").casefold(),
                        }
                    ),
                    None,
                )
                if option is None:
                    raise ValueError(ui_language.tr("fallback.error.provider"))
                slot = "fast" if model_class == "light" else "pro"
                primary = selected.target_for_slot(slot)
                level = 1 if option.get("engine") == primary.provider else 2
            selected = runtime.backend_manager.prepare_her_v2_fallback(
                enabled=True,
                level=level,
                model_class=model_class,
                provider=provider,
                model=model,
                current=selected,
            )
        error = _apply(runtime, selected)
        if error:
            raise ValueError(error)
    except (IndexError, OSError, TypeError, ValueError) as exc:
        await runtime._reply_text(
            update,
            ui_language.tr("fallback.failed", reason=html.escape(str(exc))),
            parse_mode="HTML",
        )
        return
    await runtime._reply_text(
        update,
        fallback_menu_text(runtime),
        parse_mode="HTML",
        reply_markup=fallback_menu_keyboard(runtime),
    )


async def callback_fallback(runtime, update, context: Any) -> None:
    del context
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        return
    if runtime.config.active_backend != HER_V2_ENGINE:
        await query.answer(ui_language.tr("fallback.active_only"), show_alert=True)
        return
    parts = str(query.data or "").split(":")
    try:
        action = parts[1]
        if action == "refresh":
            pass
        elif action == "toggle":
            current = runtime.backend_manager.get_her_v2_configuration()
            selected = runtime.backend_manager.prepare_her_v2_fallback(
                enabled=not current.fallback_enabled,
                current=current,
            )
            error = _apply(runtime, selected)
            if error:
                raise ValueError(error)
        elif action == "slot":
            level, model_class = _level(parts[2]), _model_class(parts[3])
            await query.edit_message_text(
                _provider_text(level, model_class),
                parse_mode="HTML",
                reply_markup=_provider_keyboard(runtime, level, model_class),
            )
            await query.answer()
            return
        elif action == "provider":
            level, model_class = _level(parts[2]), _model_class(parts[3])
            options = _eligible_options(runtime, level, model_class)
            index = int(parts[4])
            option = options[index]
            if parts[5] != _token(option["engine"]):
                raise ValueError(ui_language.tr("fallback.error.stale"))
            await query.edit_message_text(
                _model_text(level, model_class, str(option["engine"])),
                parse_mode="HTML",
                reply_markup=_model_keyboard(runtime, level, model_class, index),
            )
            await query.answer()
            return
        elif action == "model":
            level, model_class = _level(parts[2]), _model_class(parts[3])
            options = _eligible_options(runtime, level, model_class)
            provider_index = int(parts[4])
            option = options[provider_index]
            if parts[5] != _token(option["engine"]):
                raise ValueError(ui_language.tr("fallback.error.stale"))
            model_index = int(parts[6])
            model = list(option.get("models") or [])[model_index]
            if parts[7] != _token(model):
                raise ValueError(ui_language.tr("fallback.error.stale"))
            selected = runtime.backend_manager.prepare_her_v2_fallback(
                enabled=True,
                level=level,
                model_class=model_class,
                provider=str(option["engine"]),
                model=str(model),
            )
            error = _apply(runtime, selected)
            if error:
                raise ValueError(error)
        elif action == "clear":
            current = runtime.backend_manager.get_her_v2_configuration()
            if len(parts) == 3:
                selected = _clear(runtime, current, parts[2])
            else:
                level, model_class = _level(parts[2]), _model_class(parts[3])
                selected = runtime.backend_manager.prepare_her_v2_fallback(
                    level=level,
                    model_class=model_class,
                    clear=True,
                    current=current,
                )
            error = _apply(runtime, selected)
            if error:
                raise ValueError(error)
        else:
            raise ValueError(ui_language.tr("fallback.error.stale"))
        await query.edit_message_text(
            fallback_menu_text(runtime),
            parse_mode="HTML",
            reply_markup=fallback_menu_keyboard(runtime),
        )
        await query.answer()
    except (IndexError, KeyError, OSError, TypeError, ValueError) as exc:
        await query.answer(
            ui_language.tr("fallback.failed", reason=str(exc)),
            show_alert=True,
        )
