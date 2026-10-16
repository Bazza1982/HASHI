"""User-consented model fallback policy menu (Frontend Connector renderer).

This module renders and edits the one authoritative policy owned by
``orchestrator.model_fallback_policy`` (PAO).  It never keeps a second target
list: every option, lock reason, and marker derives from the policy, the
authoritative compatibility registry, and the backend manager's provider
options.  Callback data stays stable and language-neutral.
"""

from __future__ import annotations

import html
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


def _save(runtime: Any, policy: policy_owner.FallbackPolicy) -> bool:
    """Publish one policy under the read revision.

    A conflict or durability error leaves the prior policy active and reports
    failure; the requested display value is never shown as if it were saved.
    """

    actor = _actor(runtime)
    try:
        _current, document = policy_owner.read_policy(
            runtime, actor, for_update=True
        )
        policy_owner.write_policy(runtime, actor, policy, document=document)
    except ConfigConflictError:
        return False
    except (OSError, ValueError, UnicodeError):
        return False
    return True


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
    return bool(option is not None and available)


def _privacy_policy(runtime: Any) -> PrivacyLevel:
    try:
        return PrivacyLevel(int(getattr(runtime.backend_manager, "privacy_level", 1)))
    except (TypeError, ValueError):
        return PrivacyLevel.PROVIDER_TRUST


def _privacy_label(runtime: Any) -> str:
    return ui_language.tr(
        f"fallback.privacy.{_privacy_policy(runtime).name.casefold()}"
    )


def _on_off(value: bool) -> str:
    return ui_language.tr("fallback.value.on" if value else "fallback.value.off")


def _mode_label(mode: str) -> str:
    return ui_language.tr(f"fallback.mode.{mode}")


def _target_label(route: policy_owner.FallbackRoute) -> str:
    if route.target_kind == policy_owner.TARGET_KIND_CODEX_CLI:
        return ui_language.tr("fallback.target.codex")
    return f"{route.provider} / {route.model}"


def _route_state(runtime: Any, route: policy_owner.FallbackRoute) -> tuple[bool, str]:
    """Return (usable, plain locked reason) from authoritative facts."""

    if route.target_kind == policy_owner.TARGET_KIND_CODEX_CLI:
        if not _codex_available(runtime):
            return False, ui_language.tr("fallback.locked.runtime_missing")
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
        ui_language.tr("fallback.fact.privacy", privacy=_privacy_label(runtime)),
        ui_language.tr("fallback.fact.targets"),
        *_target_lines(runtime, policy),
        ui_language.tr(
            "fallback.fact.sources",
            herv3=_on_off(herv3_rule.enabled),
            codex=_on_off(codex_rule.enabled),
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
            ),
            ui_language.tr(
                "fallback.sources.codex",
                state=_on_off(codex_rule.enabled),
                targets=_on_off(codex_rule.allow_herv3_targets),
                codex_targets=_on_off(codex_rule.allow_codex_cli_targets),
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
        marker = "✓ " if rule.enabled else ""
        rows.append(
            [
                InlineKeyboardButton(
                    marker + ui_language.tr(label_key),
                    callback_data=f"fallback:source:{name}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                ui_language.tr("fallback.sources.herv3_targets.short"),
                callback_data=f"fallback:sourcekind:{policy_owner.SOURCE_HERV3}:herv3",
            ),
            InlineKeyboardButton(
                ui_language.tr("fallback.sources.codex_targets.short"),
                callback_data=f"fallback:sourcekind:{policy_owner.SOURCE_HERV3}:codex",
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
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("−", callback_data="fallback:budget:dec"),
                InlineKeyboardButton(
                    str(policy.max_additional_calls), callback_data="fallback:budget"
                ),
                InlineKeyboardButton("+", callback_data="fallback:budget:inc"),
            ],
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
    name = _privacy_policy(runtime).name.casefold()
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
    name = _privacy_policy(runtime).name.casefold()
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
            }
        )
    return candidates


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
        rows.append(
            [
                InlineKeyboardButton(
                    f"✕ {index + 1}. {_target_label(route)[:40]}",
                    callback_data=f"fallback:remove:{index}:{_token(route.key())}",
                )
            ]
        )
    candidates = _same_model_candidates(runtime)
    for index, candidate in enumerate(candidates):
        marker = "" if candidate["usable"] else "🔒 "
        label = f"{marker}{candidate['engine']} / {candidate['model']}"
        rows.append(
            [
                InlineKeyboardButton(
                    label[:60],
                    callback_data=(
                        f"fallback:add:model:{index}:"
                        f"{_token(candidate['engine'] + '/' + candidate['model'])}"
                    ),
                )
            ]
        )
    codex_locked = not _codex_available(runtime)
    rows.append(
        [
            InlineKeyboardButton(
                ("🔒 " if codex_locked else "")
                + ui_language.tr("fallback.target.codex"),
                callback_data="fallback:add:codex:-:-",
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
        if action == "refresh":
            pass
        elif action == "mode":
            mode = parts[2]
            if mode not in policy_owner.FALLBACK_MODES:
                raise ValueError(ui_language.tr("fallback.error.stale"))
            if not _save(runtime, replace(_load(runtime), mode=mode)):
                raise ValueError(ui_language.tr("fallback.save_failed"))
        elif action == "targets":
            await _render(runtime, query, _targets_text(runtime), _targets_keyboard(runtime))
            await query.answer()
            return
        elif action == "budget":
            if len(parts) == 3 and parts[2] in {"inc", "dec"}:
                policy = _load(runtime)
                delta = 1 if parts[2] == "inc" else -1
                updated = replace(
                    policy,
                    max_additional_calls=max(0, policy.max_additional_calls + delta),
                )
                if not _save(runtime, updated):
                    raise ValueError(ui_language.tr("fallback.save_failed"))
            elif len(parts) == 3 and parts[2] == "unknown":
                if not _save(
                    runtime,
                    replace(
                        _load(runtime),
                        allow_unknown_cost=not _load(runtime).allow_unknown_cost,
                    ),
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
            policy = _load(runtime)
            rule = policy.source_rule(name)
            updated = replace(
                policy,
                sources={
                    **policy.sources,
                    name: replace(rule, enabled=not rule.enabled),
                },
            )
            if not _save(runtime, updated):
                raise ValueError(ui_language.tr("fallback.save_failed"))
            await _render(
                runtime, query, _sources_text(runtime), _sources_keyboard(runtime)
            )
            await query.answer()
            return
        elif action == "sourcekind":
            name, kind = parts[2], parts[3]
            policy = _load(runtime)
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
            if not _save(runtime, updated):
                raise ValueError(ui_language.tr("fallback.save_failed"))
            await _render(
                runtime, query, _sources_text(runtime), _sources_keyboard(runtime)
            )
            await query.answer()
            return
        elif action == "privacy":
            if len(parts) == 3 and parts[2] == "block":
                policy = _load(runtime)
                name = _privacy_policy(runtime).name.casefold()
                narrowing = dict(policy.privacy_narrowing)
                current = dict(narrowing.get(name) or {})
                current["blocked"] = not (current.get("blocked") is True)
                narrowing[name] = current
                if not _save(runtime, replace(policy, privacy_narrowing=narrowing)):
                    raise ValueError(ui_language.tr("fallback.save_failed"))
            await _render(
                runtime, query, _privacy_text(runtime), _privacy_keyboard(runtime)
            )
            await query.answer()
            return
        elif action == "remove":
            index = int(parts[2])
            token = parts[3]
            policy = _load(runtime)
            if index < 0 or index >= len(policy.routes):
                raise ValueError(ui_language.tr("fallback.error.stale"))
            route = policy.routes[index]
            if _token(route.key()) != token:
                raise ValueError(ui_language.tr("fallback.error.stale"))
            if not _save(runtime, policy.without_route(route.key())):
                raise ValueError(ui_language.tr("fallback.save_failed"))
            await _render(
                runtime, query, _targets_text(runtime), _targets_keyboard(runtime)
            )
            await query.answer()
            return
        elif action == "add":
            kind = parts[2]
            policy = _load(runtime)
            if kind == "codex":
                if not _codex_available(runtime):
                    raise ValueError(ui_language.tr("fallback.locked.runtime_missing"))
                route = policy_owner.FallbackRoute(
                    source=policy_owner.SOURCE_HERV3,
                    target_kind=policy_owner.TARGET_KIND_CODEX_CLI,
                )
            elif kind == "model":
                index = int(parts[3])
                token = parts[4]
                candidates = _same_model_candidates(runtime)
                if index < 0 or index >= len(candidates):
                    raise ValueError(ui_language.tr("fallback.error.stale"))
                candidate = candidates[index]
                if (
                    _token(candidate["engine"] + "/" + candidate["model"]) != token
                    or not candidate["usable"]
                ):
                    raise ValueError(ui_language.tr("fallback.error.stale"))
                route = policy_owner.FallbackRoute(
                    source=policy_owner.SOURCE_HERV3,
                    target_kind=policy_owner.TARGET_KIND_HERV3_MODEL,
                    provider=candidate["engine"],
                    model=candidate["model"],
                    same_model=True,
                )
            else:
                raise ValueError(ui_language.tr("fallback.error.stale"))
            if not _save(runtime, policy.with_route(route)):
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
        await query.answer(str(exc), show_alert=True)
