"""User-consented model fallback policy menu (Frontend Connector renderer).

This module renders and edits the one authoritative policy owned by
``orchestrator.model_fallback_policy`` (PAO).  It never keeps a second target
list: every option, lock reason, and marker derives from the policy, the
authoritative compatibility registry, and the backend manager's provider
options.  Callback data stays stable and language-neutral.
"""

from __future__ import annotations

import asyncio
import html
import time
from dataclasses import replace
from hashlib import blake2s
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import model_fallback_policy as policy_owner
from orchestrator import ui_language
from orchestrator.command_ui import back_label, refresh_label, setting_card
from orchestrator.config_json import ConfigConflictError
from orchestrator.privacy_levels import PrivacyLevel


def _token(value: Any) -> str:
    return blake2s(str(value).encode("utf-8"), digest_size=5).hexdigest()


def _actor(runtime: Any) -> str:
    try:
        from orchestrator import runtime_session

        return str(runtime_session.owner_id(runtime) or "default")
    except Exception:
        return "default"


def _load(runtime: Any) -> policy_owner.FallbackPolicy:
    policy, _document = policy_owner.read_policy(runtime, _actor(runtime))
    return policy


def _load_for_update(runtime: Any) -> tuple[policy_owner.FallbackPolicy, Any]:
    return policy_owner.read_policy(runtime, _actor(runtime), for_update=True)


def _save(
    runtime: Any,
    policy: policy_owner.FallbackPolicy,
    *,
    document: Any,
) -> bool:
    """Publish one policy under the read revision.

    A conflict or durability error leaves the prior policy active and reports
    failure; the requested display value is never shown as if it were saved.
    """

    actor = _actor(runtime)
    try:
        policy_owner.write_policy(runtime, actor, policy, document=document)
    except ConfigConflictError:
        return False
    except (OSError, ValueError, UnicodeError):
        return False
    return True


_PENDING_CONFIRMATIONS: dict[
    tuple[int, str],
    tuple[policy_owner.FallbackConfirmation, asyncio.Future[bool]],
] = {}


def _confirmation_key(runtime: Any, action_id: str) -> tuple[int, str]:
    return id(runtime), str(action_id or "")


def _interactive_chat_id(runtime: Any, request_id: str) -> int | None:
    meta = getattr(runtime, "current_request_meta", None)
    if not isinstance(meta, dict):
        return None
    active_id = str(meta.get("request_id") or "")
    expected = str(request_id or "")
    if active_id.removeprefix("hashi-request:") != expected.removeprefix(
        "hashi-request:"
    ):
        return None
    chat_id = meta.get("chat_id")
    try:
        return int(chat_id) if chat_id is not None else None
    except (TypeError, ValueError):
        return None


async def request_fallback_confirmation(
    runtime: Any,
    confirmation: policy_owner.FallbackConfirmation,
    *,
    target_label: str,
    request_id: str,
) -> policy_owner.FallbackConfirmation | None:
    """Ask the active Telegram user once; absent interaction fails closed."""

    chat_id = _interactive_chat_id(runtime, request_id)
    if chat_id is None:
        return None
    key = _confirmation_key(runtime, confirmation.action_id)
    if key in _PENDING_CONFIRMATIONS:
        return None
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    _PENDING_CONFIRMATIONS[key] = (confirmation, future)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    ui_language.tr("fallback.ask.allow"),
                    callback_data=(
                        f"fallback:confirm:{confirmation.action_id}:allow"
                    ),
                ),
                InlineKeyboardButton(
                    ui_language.tr("fallback.ask.deny"),
                    callback_data=(
                        f"fallback:confirm:{confirmation.action_id}:deny"
                    ),
                ),
            ]
        ]
    )
    try:
        from orchestrator import runtime_delivery

        _duration, chunks = await runtime_delivery.send_long_message(
            runtime,
            chat_id=chat_id,
            text=ui_language.tr(
                "fallback.ask.prompt",
                target=html.escape(str(target_label or "")),
            ),
            request_id=str(request_id or ""),
            purpose="response_continuation",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        if not chunks:
            return None
        timeout = max(0.0, float(confirmation.expires_at) - time.time())
        if timeout <= 0:
            return None
        allowed = await asyncio.wait_for(future, timeout=timeout)
        return confirmation if allowed else None
    except TimeoutError:
        return None
    finally:
        _PENDING_CONFIRMATIONS.pop(key, None)


def _provider_options(runtime: Any) -> list[dict[str, Any]]:
    try:
        return list(runtime.backend_manager.get_her_v2_provider_options())
    except Exception:
        return []


def _option_for(runtime: Any, engine: str) -> tuple[dict[str, Any] | None, bool]:
    for option in _provider_options(runtime):
        if str(option.get("engine") or "").casefold() == str(engine or "").casefold():
            return option, option.get("available") is not False
    return None, False


def _codex_available(runtime: Any) -> bool:
    option, available = _option_for(runtime, "codex-cli")
    return bool(option is not None and available and _codex_backend_rows(runtime))


def _allowed_backend_rows(runtime: Any) -> list[dict[str, Any]]:
    config = getattr(runtime, "config", None)
    if config is None:
        config = getattr(getattr(runtime, "backend_manager", None), "config", None)
    return list(getattr(config, "allowed_backends", ()) or ())


def _codex_backend_rows(runtime: Any) -> list[dict[str, Any]]:
    from orchestrator.flexible_backend_registry import canonical_backend_engine

    return [
        row
        for row in _allowed_backend_rows(runtime)
        if canonical_backend_engine(str(row.get("engine") or "")) == "codex-cli"
    ]


def _codex_models(runtime: Any) -> list[str]:
    from orchestrator.runtime_effort_options import get_available_models

    rows = _codex_backend_rows(runtime)
    if not rows:
        return []
    try:
        return list(
            dict.fromkeys(
                get_available_models("codex-cli", allowed_backends=rows)
            )
        )
    except (TypeError, ValueError):
        return []


def _privacy_policy(runtime: Any) -> PrivacyLevel:
    try:
        return PrivacyLevel(int(getattr(runtime.backend_manager, "privacy_level", 1)))
    except (TypeError, ValueError):
        return PrivacyLevel.PROVIDER_TRUST


def _privacy_label(runtime: Any) -> str:
    return ui_language.tr(
        f"fallback.privacy.{_privacy_policy(runtime).name.casefold()}"
    )


def _source_label(source: str) -> str:
    key = (
        "fallback.sources.codex.short"
        if source == policy_owner.SOURCE_CODEX_CLI
        else "fallback.sources.herv3.short"
    )
    return ui_language.tr(key)


def _on_off(value: bool) -> str:
    return ui_language.tr("fallback.value.on" if value else "fallback.value.off")


def _mode_label(mode: str) -> str:
    return ui_language.tr(f"fallback.mode.{mode}")


def _effect_label(mode: str) -> str:
    return ui_language.tr(f"fallback.effect.{mode}")


def _target_label(route: policy_owner.FallbackRoute) -> str:
    if route.target_kind == policy_owner.TARGET_KIND_CODEX_CLI:
        label = ui_language.tr("fallback.target.codex")
        return f"{label} / {route.model}" if route.model else label
    return f"{route.provider} / {route.model}"


def _route_state(runtime: Any, route: policy_owner.FallbackRoute) -> tuple[bool, str]:
    """Return (usable, plain locked reason) from authoritative facts."""

    if route.target_kind == policy_owner.TARGET_KIND_CODEX_CLI:
        if not _codex_available(runtime):
            return False, ui_language.tr("fallback.locked.runtime_missing")
        if route.model and route.model not in _codex_models(runtime):
            return False, ui_language.tr("fallback.locked.model_unsupported")
        return True, ""
    option, available = _option_for(runtime, route.provider)
    if not available:
        return False, ui_language.tr("fallback.locked.key_missing")
    models = [str(item) for item in (option or {}).get("models") or []]
    if route.model not in models:
        return False, ui_language.tr("fallback.locked.model_unsupported")
    return True, ""


def _target_lines(runtime: Any, policy: policy_owner.FallbackPolicy) -> list[str]:
    lines: list[str] = []
    for index, route in enumerate(policy.routes):
        usable, reason = _route_state(runtime, route)
        marker = "✓ " if usable else "🔒 "
        label = html.escape(_target_label(route))
        suffix = f" — {html.escape(reason)}" if reason else ""
        lines.append(f"{marker}{index + 1}. <code>{label}</code>{suffix}")
    if not lines:
        lines.append(ui_language.tr("fallback.target.none"))
    return lines


def _cost_label(policy: policy_owner.FallbackPolicy) -> str:
    if policy.max_extra_cost_usd is None:
        return ui_language.tr("fallback.cost.none")
    return ui_language.tr(
        "fallback.cost.ceiling", amount=f"{policy.max_extra_cost_usd:.2f}"
    )


def fallback_menu_text(runtime: Any) -> str:
    policy = _load(runtime)
    herv3_rule = policy.source_rule(policy_owner.SOURCE_HERV3)
    codex_rule = policy.source_rule(policy_owner.SOURCE_CODEX_CLI)
    facts = [
        ui_language.tr("fallback.fact.scope"),
        ui_language.tr(f"fallback.mode_help.{policy.mode}"),
        ui_language.tr("fallback.fact.privacy", privacy=_privacy_label(runtime)),
        ui_language.tr("fallback.fact.targets"),
        *_target_lines(runtime, policy),
        ui_language.tr(
            "fallback.fact.sources",
            herv3=_on_off(herv3_rule.enabled),
            codex=_on_off(codex_rule.enabled),
        ),
        ui_language.tr(
            "fallback.fact.retries",
            delays=" / ".join(
                str(int(value)) for value in policy_owner.CAPACITY_RETRY_DELAYS_S
            ),
        ),
        ui_language.tr(
            "fallback.fact.effects",
            herv3=_effect_label(herv3_rule.side_effect_mode),
            codex=_effect_label(codex_rule.side_effect_mode),
        ),
        ui_language.tr(
            "fallback.fact.budget",
            count=policy.max_additional_calls,
            cost=_cost_label(policy),
        ),
        ui_language.tr(
            "fallback.fact.unknown_cost",
            state=_on_off(policy.allow_unknown_cost),
        ),
        ui_language.tr("fallback.fact.effect"),
    ]
    return setting_card(
        "🛟",
        ui_language.tr("fallback.title"),
        current=f"<b>{html.escape(_mode_label(policy.mode))}</b>",
        facts=facts,
        consequence=ui_language.tr("fallback.excluded"),
        action=ui_language.tr("fallback.action"),
    )


def _mode_button(policy: policy_owner.FallbackPolicy, mode: str) -> InlineKeyboardButton:
    marker = "✓ " if policy.mode == mode else ""
    return InlineKeyboardButton(
        marker + _mode_label(mode), callback_data=f"fallback:mode:{mode}"
    )


def fallback_menu_keyboard(runtime: Any) -> InlineKeyboardMarkup:
    policy = _load(runtime)
    return InlineKeyboardMarkup(
        [
            [
                _mode_button(policy, policy_owner.MODE_OFF),
                _mode_button(policy, policy_owner.MODE_RETRY),
            ],
            [
                _mode_button(policy, policy_owner.MODE_ASK),
                _mode_button(policy, policy_owner.MODE_AUTO),
            ],
            [
                InlineKeyboardButton(
                    ui_language.tr("fallback.button.targets"),
                    callback_data="fallback:targets",
                ),
                InlineKeyboardButton(
                    ui_language.tr("fallback.button.budget"),
                    callback_data="fallback:budget",
                ),
            ],
            [
                InlineKeyboardButton(
                    ui_language.tr("fallback.button.sources"),
                    callback_data="fallback:sources",
                ),
                InlineKeyboardButton(
                    ui_language.tr("fallback.button.privacy"),
                    callback_data="fallback:privacy",
                ),
            ],
            [InlineKeyboardButton(refresh_label(), callback_data="fallback:refresh")],
        ]
    )


def _sources_text(runtime: Any) -> str:
    policy = _load(runtime)
    herv3_rule = policy.source_rule(policy_owner.SOURCE_HERV3)
    codex_rule = policy.source_rule(policy_owner.SOURCE_CODEX_CLI)
    return setting_card(
        "🛟",
        ui_language.tr("fallback.sources.title"),
        current=html.escape(_mode_label(policy.mode)),
        facts=[
            ui_language.tr(
                "fallback.sources.herv3",
                state=_on_off(herv3_rule.enabled),
                targets=_on_off(herv3_rule.allow_herv3_targets),
                codex_targets=_on_off(herv3_rule.allow_codex_cli_targets),
                effect=_effect_label(herv3_rule.side_effect_mode),
            ),
            ui_language.tr(
                "fallback.sources.codex",
                state=_on_off(codex_rule.enabled),
                targets=_on_off(codex_rule.allow_herv3_targets),
                codex_targets=_on_off(codex_rule.allow_codex_cli_targets),
                effect=_effect_label(codex_rule.side_effect_mode),
            ),
        ],
        consequence=ui_language.tr("fallback.sources.consequence"),
        action=ui_language.tr("fallback.sources.action"),
    )


def _sources_keyboard(runtime: Any) -> InlineKeyboardMarkup:
    policy = _load(runtime)
    rows: list[list[InlineKeyboardButton]] = []
    for name, label_key in (
        (policy_owner.SOURCE_HERV3, "fallback.sources.herv3.short"),
        (policy_owner.SOURCE_CODEX_CLI, "fallback.sources.codex.short"),
    ):
        rule = policy.source_rule(name)
        source_label = ui_language.tr(label_key)
        rows.append(
            [
                InlineKeyboardButton(
                    ("✓ " if rule.enabled else "") + source_label,
                    callback_data=f"fallback:source:{name}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    ("✓ " if rule.side_effect_mode == policy_owner.EFFECT_PAUSE else "")
                    + ui_language.tr("fallback.effect.pause.short"),
                    callback_data=f"fallback:effect:{name}:pause",
                ),
                InlineKeyboardButton(
                    ("✓ " if rule.side_effect_mode == policy_owner.EFFECT_RESUME else "")
                    + ui_language.tr("fallback.effect.resume.short"),
                    callback_data=f"fallback:effect:{name}:resume",
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    ("✓ " if rule.allow_herv3_targets else "")
                    + ui_language.tr(
                        "fallback.sources.direction",
                        source=source_label,
                        target=ui_language.tr("fallback.sources.herv3_targets.short"),
                    ),
                    callback_data=f"fallback:sourcekind:{name}:herv3",
                ),
                InlineKeyboardButton(
                    ("✓ " if rule.allow_codex_cli_targets else "")
                    + ui_language.tr(
                        "fallback.sources.direction",
                        source=source_label,
                        target=ui_language.tr("fallback.sources.codex_targets.short"),
                    ),
                    callback_data=f"fallback:sourcekind:{name}:codex",
                ),
            ]
        )
    rows.append([InlineKeyboardButton(back_label(), callback_data="fallback:refresh")])
    return InlineKeyboardMarkup(rows)


def _budget_text(runtime: Any) -> str:
    policy = _load(runtime)
    return setting_card(
        "🛟",
        ui_language.tr("fallback.budget.title"),
        current=ui_language.tr(
            "fallback.budget.current", count=policy.max_additional_calls
        ),
        facts=[
            ui_language.tr("fallback.budget.rule"),
            _cost_label(policy),
            ui_language.tr(
                "fallback.fact.unknown_cost",
                state=_on_off(policy.allow_unknown_cost),
            ),
            ui_language.tr("fallback.fact.effect"),
        ],
        consequence=ui_language.tr("fallback.budget.consequence"),
        action=ui_language.tr("fallback.budget.action"),
    )


def _budget_keyboard(runtime: Any) -> InlineKeyboardMarkup:
    policy = _load(runtime)
    cost_choices = (
        ("none", None, ui_language.tr("fallback.cost.no_ceiling.short")),
        ("5", 0.05, "$0.05"),
        ("25", 0.25, "$0.25"),
        ("100", 1.0, "$1.00"),
    )
    cost_buttons = []
    for token, amount, label in cost_choices:
        selected = (
            policy.max_extra_cost_usd is None
            if amount is None
            else policy.max_extra_cost_usd is not None
            and abs(float(policy.max_extra_cost_usd) - amount) < 0.000001
        )
        cost_buttons.append(
            InlineKeyboardButton(
                ("✓ " if selected else "") + label,
                callback_data=f"fallback:cost:{token}",
            )
        )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("−", callback_data="fallback:budget:dec"),
                InlineKeyboardButton(
                    str(policy.max_additional_calls), callback_data="fallback:budget"
                ),
                InlineKeyboardButton("+", callback_data="fallback:budget:inc"),
            ],
            cost_buttons,
            [
                InlineKeyboardButton(
                    ("✓ " if policy.allow_unknown_cost else "")
                    + ui_language.tr("fallback.budget.unknown_toggle"),
                    callback_data="fallback:budget:unknown",
                )
            ],
            [InlineKeyboardButton(back_label(), callback_data="fallback:refresh")],
        ]
    )


def _privacy_text(runtime: Any) -> str:
    policy = _load(runtime)
    narrowing = policy.effective_narrowing(int(_privacy_policy(runtime)))
    blocked = narrowing.get("blocked") is True
    return setting_card(
        "🛟",
        ui_language.tr("fallback.privacy.title"),
        current=html.escape(_privacy_label(runtime)),
        facts=[
            ui_language.tr("fallback.privacy.explain"),
            ui_language.tr(
                "fallback.privacy.blocked",
                state=_on_off(blocked),
            ),
        ],
        consequence=ui_language.tr("fallback.privacy.consequence"),
        action=ui_language.tr("fallback.privacy.action"),
    )


def _privacy_keyboard(runtime: Any) -> InlineKeyboardMarkup:
    policy = _load(runtime)
    blocked = policy.effective_narrowing(int(_privacy_policy(runtime))).get(
        "blocked"
    ) is True
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    ("✓ " if blocked else "")
                    + ui_language.tr("fallback.privacy.blocked.button"),
                    callback_data="fallback:privacy:block",
                )
            ],
            [InlineKeyboardButton(back_label(), callback_data="fallback:refresh")],
        ]
    )


def _same_model_candidates(runtime: Any) -> list[dict[str, Any]]:
    """Derive exact same-model targets from the authoritative registry."""

    from orchestrator.flexible_backend_registry import (
        get_same_model_provider_targets,
    )

    try:
        target = runtime.backend_manager.get_her_v3_target()
        provider, model = str(target.provider), str(target.model)
    except Exception:
        return []
    candidates = []
    for engine, mapped_model in get_same_model_provider_targets(provider, model):
        option, available = _option_for(runtime, engine)
        offered = [str(item) for item in (option or {}).get("models") or []]
        usable = bool(available and mapped_model in offered)
        reason = ""
        if not available:
            reason = ui_language.tr("fallback.locked.key_missing")
        elif mapped_model not in offered:
            reason = ui_language.tr("fallback.locked.model_unsupported")
        candidates.append(
            {
                "engine": engine,
                "model": mapped_model,
                "usable": usable,
                "reason": reason,
                "same_model": True,
            }
        )
    return candidates


def _herv3_model_candidates(runtime: Any) -> list[dict[str, Any]]:
    """Derive all exact HERV3 Provider/model targets from runtime options."""

    same_model = _same_model_candidates(runtime)
    candidates = list(same_model)
    seen = {(item["engine"], item["model"]) for item in candidates}
    try:
        current = runtime.backend_manager.get_her_v3_target()
        current_key = (str(current.provider).casefold(), str(current.model))
    except Exception:
        current_key = ("", "")
    for option in _provider_options(runtime):
        engine = str(option.get("engine") or "").strip().casefold()
        if not engine or engine in {"codex-cli", "her-v2", "hashi-api"}:
            continue
        available = option.get("available") is not False
        for raw_model in option.get("models") or []:
            model = str(raw_model or "").strip()
            key = (engine, model)
            if not model or key in seen or key == current_key:
                continue
            seen.add(key)
            candidates.append(
                {
                    "engine": engine,
                    "model": model,
                    "usable": bool(available),
                    "reason": (
                        "" if available else ui_language.tr("fallback.locked.key_missing")
                    ),
                    "same_model": False,
                }
            )
    return candidates


def _model_candidates(runtime: Any, source: str) -> list[dict[str, Any]]:
    if source == policy_owner.SOURCE_HERV3:
        return _herv3_model_candidates(runtime)
    if source != policy_owner.SOURCE_CODEX_CLI:
        return []
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for option in _provider_options(runtime):
        engine = str(option.get("engine") or "").strip().casefold()
        if not engine or engine in {"codex-cli", "her-v2", "hashi-api"}:
            continue
        available = option.get("available") is not False
        for raw_model in option.get("models") or []:
            model = str(raw_model or "").strip()
            key = (engine, model)
            if not model or key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "engine": engine,
                    "model": model,
                    "usable": bool(available),
                    "reason": (
                        "" if available else ui_language.tr("fallback.locked.key_missing")
                    ),
                    "same_model": False,
                }
            )
    return candidates


def _codex_model_candidates(runtime: Any) -> list[dict[str, Any]]:
    available = _codex_available(runtime)
    try:
        current = str(runtime.backend_manager.current_backend.config.model or "")
    except Exception:
        current = ""
    return [
        {
            "model": model,
            "usable": available,
            "reason": (
                "" if available else ui_language.tr("fallback.locked.runtime_missing")
            ),
        }
        for model in _codex_models(runtime)
        if model != current
    ]


def _targets_text(runtime: Any) -> str:
    policy = _load(runtime)
    return setting_card(
        "🛟",
        ui_language.tr("fallback.targets.title"),
        current=html.escape(_mode_label(policy.mode)),
        facts=[
            ui_language.tr("fallback.targets.explain"),
            *_target_lines(runtime, policy),
        ],
        consequence=ui_language.tr("fallback.targets.consequence"),
        action=ui_language.tr("fallback.targets.action"),
    )


def _targets_keyboard(runtime: Any) -> InlineKeyboardMarkup:
    policy = _load(runtime)
    rows: list[list[InlineKeyboardButton]] = []
    for index, route in enumerate(policy.routes):
        route_token = _token(route.key())
        row: list[InlineKeyboardButton] = []
        if index > 0:
            row.append(
                InlineKeyboardButton(
                    "↑",
                    callback_data=f"fallback:move:{index}:up:{route_token}",
                )
            )
        row.append(
            InlineKeyboardButton(
                (
                    f"✕ {index + 1}. {_source_label(route.source)} → "
                    f"{_target_label(route)}"
                )[:48],
                callback_data=f"fallback:remove:{index}:{route_token}",
            )
        )
        if index + 1 < len(policy.routes):
            row.append(
                InlineKeyboardButton(
                    "↓",
                    callback_data=f"fallback:move:{index}:down:{route_token}",
                )
            )
        rows.append(row)

    for source in policy_owner.FALLBACK_SOURCES:
        source_label = _source_label(source)
        candidates = _model_candidates(runtime, source)
        for index, candidate in enumerate(candidates):
            marker = "" if candidate["usable"] else "🔒 "
            label = (
                f"{marker}+ {source_label} → "
                f"{candidate['engine']} / {candidate['model']}"
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        label[:60],
                        callback_data=(
                            f"fallback:add:model:{source}:{index}:"
                            f"{_token(candidate['engine'] + '/' + candidate['model'])}"
                        ),
                    )
                ]
            )
        for index, candidate in enumerate(_codex_model_candidates(runtime)):
            marker = "" if candidate["usable"] else "🔒 "
            rows.append(
                [
                    InlineKeyboardButton(
                        (
                            marker
                            + f"+ {source_label} → "
                            + ui_language.tr("fallback.target.codex")
                            + f" / {candidate['model']}"
                        )[:60],
                        callback_data=(
                            f"fallback:add:codexmodel:{source}:{index}:"
                            f"{_token(candidate['model'])}"
                        ),
                    )
                ]
            )
    rows.append([InlineKeyboardButton(back_label(), callback_data="fallback:refresh")])
    return InlineKeyboardMarkup(rows)


async def _render(runtime: Any, query: Any, text: str, keyboard: InlineKeyboardMarkup) -> None:
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


async def cmd_fallback(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    args = [str(item).strip() for item in (context.args or [])]
    if args and args[0].casefold() not in {"status", "menu", "show", ""}:
        await runtime._reply_text(
            update,
            ui_language.tr("fallback.usage"),
            parse_mode="HTML",
        )
        return
    await runtime._reply_text(
        update,
        fallback_menu_text(runtime),
        parse_mode="HTML",
        reply_markup=fallback_menu_keyboard(runtime),
    )


async def callback_fallback(runtime: Any, update: Any, context: Any) -> None:
    del context
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        return
    parts = str(query.data or "").split(":")
    try:
        action = parts[1]
        if action == "confirm":
            action_id, answer = parts[2], parts[3]
            pending = _PENDING_CONFIRMATIONS.get(
                _confirmation_key(runtime, action_id)
            )
            if pending is None or answer not in {"allow", "deny"}:
                raise ValueError(ui_language.tr("fallback.error.stale"))
            _confirmation, future = pending
            if future.done():
                raise ValueError(ui_language.tr("fallback.error.stale"))
            allowed = answer == "allow"
            future.set_result(allowed)
            await query.edit_message_text(
                ui_language.tr(
                    "fallback.ask.allowed"
                    if allowed
                    else "fallback.ask.denied"
                )
            )
            await query.answer()
            return
        if action == "refresh":
            pass
        elif action == "mode":
            mode = parts[2]
            if mode not in policy_owner.FALLBACK_MODES:
                raise ValueError(ui_language.tr("fallback.error.stale"))
            policy, document = _load_for_update(runtime)
            if not _save(
                runtime,
                replace(policy, mode=mode),
                document=document,
            ):
                raise ValueError(ui_language.tr("fallback.save_failed"))
        elif action == "targets":
            await _render(runtime, query, _targets_text(runtime), _targets_keyboard(runtime))
            await query.answer()
            return
        elif action == "budget":
            if len(parts) == 3 and parts[2] in {"inc", "dec"}:
                policy, document = _load_for_update(runtime)
                delta = 1 if parts[2] == "inc" else -1
                updated = replace(
                    policy,
                    max_additional_calls=max(0, policy.max_additional_calls + delta),
                )
                if not _save(runtime, updated, document=document):
                    raise ValueError(ui_language.tr("fallback.save_failed"))
            elif len(parts) == 3 and parts[2] == "unknown":
                policy, document = _load_for_update(runtime)
                if not _save(
                    runtime,
                    replace(
                        policy,
                        allow_unknown_cost=not policy.allow_unknown_cost,
                    ),
                    document=document,
                ):
                    raise ValueError(ui_language.tr("fallback.save_failed"))
            await _render(
                runtime, query, _budget_text(runtime), _budget_keyboard(runtime)
            )
            await query.answer()
            return
        elif action == "cost":
            token = parts[2]
            choices = {
                "none": None,
                "5": 0.05,
                "25": 0.25,
                "100": 1.0,
            }
            if token not in choices:
                raise ValueError(ui_language.tr("fallback.error.stale"))
            policy, document = _load_for_update(runtime)
            if not _save(
                runtime,
                replace(policy, max_extra_cost_usd=choices[token]),
                document=document,
            ):
                raise ValueError(ui_language.tr("fallback.save_failed"))
            await _render(
                runtime, query, _budget_text(runtime), _budget_keyboard(runtime)
            )
            await query.answer()
            return
        elif action == "sources":
            await _render(
                runtime, query, _sources_text(runtime), _sources_keyboard(runtime)
            )
            await query.answer()
            return
        elif action == "source":
            name = parts[2]
            policy, document = _load_for_update(runtime)
            rule = policy.source_rule(name)
            if name not in policy_owner.FALLBACK_SOURCES:
                raise ValueError(ui_language.tr("fallback.error.stale"))
            updated = replace(
                policy,
                sources={
                    **policy.sources,
                    name: replace(rule, enabled=not rule.enabled),
                },
            )
            if not _save(runtime, updated, document=document):
                raise ValueError(ui_language.tr("fallback.save_failed"))
            await _render(
                runtime, query, _sources_text(runtime), _sources_keyboard(runtime)
            )
            await query.answer()
            return
        elif action == "sourcekind":
            name, kind = parts[2], parts[3]
            if (
                name not in policy_owner.FALLBACK_SOURCES
                or kind not in {"herv3", "codex"}
            ):
                raise ValueError(ui_language.tr("fallback.error.stale"))
            policy, document = _load_for_update(runtime)
            rule = policy.source_rule(name)
            updated = replace(
                policy,
                sources={
                    **policy.sources,
                    name: replace(
                        rule,
                        allow_herv3_targets=(
                            not rule.allow_herv3_targets
                            if kind == "herv3"
                            else rule.allow_herv3_targets
                        ),
                        allow_codex_cli_targets=(
                            not rule.allow_codex_cli_targets
                            if kind == "codex"
                            else rule.allow_codex_cli_targets
                        ),
                    ),
                },
            )
            if not _save(runtime, updated, document=document):
                raise ValueError(ui_language.tr("fallback.save_failed"))
            await _render(
                runtime, query, _sources_text(runtime), _sources_keyboard(runtime)
            )
            await query.answer()
            return
        elif action == "effect":
            name, mode = parts[2], parts[3]
            if (
                name not in policy_owner.FALLBACK_SOURCES
                or mode not in policy_owner.SIDE_EFFECT_MODES
            ):
                raise ValueError(ui_language.tr("fallback.error.stale"))
            policy, document = _load_for_update(runtime)
            rule = policy.source_rule(name)
            updated = replace(
                policy,
                sources={
                    **policy.sources,
                    name: replace(rule, side_effect_mode=mode),
                },
            )
            if not _save(runtime, updated, document=document):
                raise ValueError(ui_language.tr("fallback.save_failed"))
            await _render(
                runtime, query, _sources_text(runtime), _sources_keyboard(runtime)
            )
            await query.answer()
            return
        elif action == "privacy":
            if len(parts) == 3 and parts[2] == "block":
                policy, document = _load_for_update(runtime)
                name = _privacy_policy(runtime).name.casefold()
                narrowing = dict(policy.privacy_narrowing)
                current = dict(narrowing.get(name) or {})
                current["blocked"] = current.get("blocked") is not True
                narrowing[name] = current
                if not _save(
                    runtime,
                    replace(policy, privacy_narrowing=narrowing),
                    document=document,
                ):
                    raise ValueError(ui_language.tr("fallback.save_failed"))
            await _render(
                runtime, query, _privacy_text(runtime), _privacy_keyboard(runtime)
            )
            await query.answer()
            return
        elif action == "move":
            index = int(parts[2])
            direction = parts[3]
            token = parts[4]
            policy, document = _load_for_update(runtime)
            if (
                index < 0
                or index >= len(policy.routes)
                or direction not in {"up", "down"}
            ):
                raise ValueError(ui_language.tr("fallback.error.stale"))
            route = policy.routes[index]
            if _token(route.key()) != token:
                raise ValueError(ui_language.tr("fallback.error.stale"))
            position = index - 1 if direction == "up" else index + 1
            if position < 0 or position >= len(policy.routes):
                raise ValueError(ui_language.tr("fallback.error.stale"))
            if not _save(
                runtime,
                policy.with_route(route, position=position),
                document=document,
            ):
                raise ValueError(ui_language.tr("fallback.save_failed"))
            await _render(
                runtime, query, _targets_text(runtime), _targets_keyboard(runtime)
            )
            await query.answer()
            return
        elif action == "remove":
            index = int(parts[2])
            token = parts[3]
            policy, document = _load_for_update(runtime)
            if index < 0 or index >= len(policy.routes):
                raise ValueError(ui_language.tr("fallback.error.stale"))
            route = policy.routes[index]
            if _token(route.key()) != token:
                raise ValueError(ui_language.tr("fallback.error.stale"))
            if not _save(
                runtime,
                policy.without_route(route.key()),
                document=document,
            ):
                raise ValueError(ui_language.tr("fallback.save_failed"))
            await _render(
                runtime, query, _targets_text(runtime), _targets_keyboard(runtime)
            )
            await query.answer()
            return
        elif action == "add":
            kind = parts[2]
            source = parts[3]
            if source not in policy_owner.FALLBACK_SOURCES:
                raise ValueError(ui_language.tr("fallback.error.stale"))
            policy, document = _load_for_update(runtime)
            if kind == "codex":
                if (
                    source != policy_owner.SOURCE_HERV3
                    or not _codex_available(runtime)
                ):
                    raise ValueError(ui_language.tr("fallback.locked.runtime_missing"))
                route = policy_owner.FallbackRoute(
                    source=source,
                    target_kind=policy_owner.TARGET_KIND_CODEX_CLI,
                )
            elif kind == "codexmodel":
                index = int(parts[4])
                token = parts[5]
                candidates = _codex_model_candidates(runtime)
                if index < 0 or index >= len(candidates):
                    raise ValueError(ui_language.tr("fallback.error.stale"))
                candidate = candidates[index]
                if (
                    _token(candidate["model"]) != token
                    or not candidate["usable"]
                ):
                    raise ValueError(ui_language.tr("fallback.error.stale"))
                route = policy_owner.FallbackRoute(
                    source=source,
                    target_kind=policy_owner.TARGET_KIND_CODEX_CLI,
                    model=candidate["model"],
                )
            elif kind == "model":
                index = int(parts[4])
                token = parts[5]
                candidates = _model_candidates(runtime, source)
                if index < 0 or index >= len(candidates):
                    raise ValueError(ui_language.tr("fallback.error.stale"))
                candidate = candidates[index]
                if (
                    _token(candidate["engine"] + "/" + candidate["model"]) != token
                    or not candidate["usable"]
                ):
                    raise ValueError(ui_language.tr("fallback.error.stale"))
                route = policy_owner.FallbackRoute(
                    source=source,
                    target_kind=policy_owner.TARGET_KIND_HERV3_MODEL,
                    provider=candidate["engine"],
                    model=candidate["model"],
                    same_model=bool(candidate["same_model"]),
                )
            else:
                raise ValueError(ui_language.tr("fallback.error.stale"))
            if not _save(runtime, policy.with_route(route), document=document):
                raise ValueError(ui_language.tr("fallback.save_failed"))
            await _render(
                runtime, query, _targets_text(runtime), _targets_keyboard(runtime)
            )
            await query.answer()
            return
        else:
            raise ValueError(ui_language.tr("fallback.error.stale"))
        await _render(
            runtime, query, fallback_menu_text(runtime), fallback_menu_keyboard(runtime)
        )
        await query.answer()
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        if str(exc) == ui_language.tr("fallback.save_failed"):
            try:
                await _render(
                    runtime,
                    query,
                    fallback_menu_text(runtime),
                    fallback_menu_keyboard(runtime),
                )
            except Exception:
                pass
        await query.answer(str(exc), show_alert=True)
