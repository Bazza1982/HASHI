from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import ui_language
from orchestrator.command_ui import (
    card_title,
    confirm_card,
    selected_label,
    setting_card,
    status_label,
)


def _command(command: str, description: str) -> str:
    return f"<code>{html.escape(command)}</code> · {html.escape(description)}"


def _tr(key: str, **values: Any) -> str:
    return ui_language.tr(key, **values)


def _label(key: str) -> str:
    return f"<b>{html.escape(_tr(key))}</b>"


def _state(key: str) -> str:
    return f"<b>{html.escape(_tr(key))}</b>"


def _fact(label_key: str, value: str) -> str:
    return f"{_label(label_key)} · {value}"


def _command_key(command: str, description_key: str) -> str:
    return _command(command, _tr(description_key))


def parked_topics_text(topics: Sequence[dict[str, Any]]) -> str:
    lines = [
        card_title("🅿️", "Parked topics"),
        "",
        _fact(
            "common.current",
            _tr("menu.park.current", count=f"<code>{len(topics)}</code>"),
        ),
        _fact("common.changes", html.escape(_tr("menu.park.changes"))),
    ]
    if not topics:
        lines.extend(["", html.escape(_tr("menu.park.none"))])
    else:
        lines.extend(["", _label("menu.park.section")])
        for topic in topics:
            slot_id = int(topic.get("slot_id", 0) or 0)
            title = topic.get("title") or _tr("menu.park.topic_default", slot=slot_id)
            short = topic.get("summary_short") or _tr("menu.park.no_summary")
            followup = topic.get("followup") or {}
            reminder_status = followup.get("status") or "scheduled"
            attempts = int(followup.get("attempts", 0) or 0)
            next_at = followup.get("next_at")
            lines.extend(
                [
                    "",
                    f"<b><code>{slot_id}</code> · {html.escape(str(title))}</b>",
                    html.escape(str(short)),
                    (
                        f"{html.escape(_tr('menu.park.reminders'))} · "
                        f"<code>{html.escape(str(reminder_status))}</code> "
                        f"· <code>{attempts}/3</code>"
                        + (
                            f" · {html.escape(_tr('menu.park.next'))} "
                            f"<code>{html.escape(str(next_at))}</code>"
                            if next_at
                            else ""
                        )
                    ),
                ]
            )
    lines.extend(
        [
            "",
            _label("common.use"),
            _command_key("/park chat [title]", "menu.park.action.park"),
            _command_key("/load <slot>", "menu.park.action.restore"),
            _command_key("/park delete <slot>", "menu.park.action.remove"),
        ]
    )
    return "\n".join(lines)


def ticket_list_text(
    open_tickets: Sequence[dict[str, Any]],
    in_progress_tickets: Sequence[dict[str, Any]],
) -> str:
    total = len(open_tickets) + len(in_progress_tickets)
    lines = [
        card_title("🎫", "Support tickets"),
        "",
        _fact(
            "common.current",
            _tr("menu.ticket.current", count=f"<code>{total}</code>"),
        ),
        _fact("menu.ticket.open", f"<code>{len(open_tickets)}</code>"),
        _fact("menu.ticket.in_progress", f"<code>{len(in_progress_tickets)}</code>"),
    ]
    for heading, tickets in (
        (_tr("menu.ticket.section.open"), open_tickets),
        (_tr("menu.ticket.section.in_progress"), in_progress_tickets),
    ):
        if not tickets:
            continue
        lines.extend(["", f"<b>{heading}</b>"])
        for ticket in tickets:
            ticket_id = html.escape(str(ticket.get("ticket_id") or "unknown"))
            source = html.escape(str(ticket.get("source_agent") or "unknown"))
            summary = html.escape(str(ticket.get("summary") or "")[:90])
            lines.append(f"<code>{ticket_id}</code> · {source} · {summary}")
    if not total:
        lines.extend(["", html.escape(_tr("menu.ticket.none"))])
    lines.extend(
        [
            "",
            _command_key("/ticket <description>", "menu.ticket.action.create"),
        ]
    )
    return "\n".join(lines)


def _sys_command_prefix(scope: str) -> str:
    return "/sys global" if scope == "global" else "/sys"


def sys_slots_text(
    manager: Any,
    *,
    scope: str = "local",
    instance_id: str | None = None,
) -> str:
    is_global = scope == "global"
    prefix = _sys_command_prefix(scope)
    slots = []
    active_count = 0
    configured_count = 0
    items = (
        manager.list_slots()
        if hasattr(manager, "list_slots")
        else [
            {"slot": slot_id, **manager._slot(slot_id)}
            for slot_id in manager.SLOTS
        ]
    )
    for item in items:
        slot_id = str(item.get("slot") or "")
        active = bool(item.get("active"))
        text = str(item.get("text") or "")
        active_count += int(active)
        configured_count += int(bool(text))
        slots.append((slot_id, active, text))

    lines = [
        card_title("🌐" if is_global else "🧾", "Global system prompt slots" if is_global else "System prompt slots"),
        "",
        f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
        f"<code>{active_count}</code> {html.escape(ui_language.tr('sys.active_suffix'))}",
        f"<b>{html.escape(ui_language.tr('common.configured'))}</b> · "
        f"<code>{configured_count}/{len(slots)}</code>",
        (
            f"<b>{html.escape(ui_language.tr('common.scope'))}</b> · "
            + ui_language.tr(
                "sys.scope.global",
                instance=f"<code>{html.escape(instance_id or 'this HASHI instance')}</code>",
            )
            if is_global
            else f"<b>{html.escape(ui_language.tr('common.scope'))}</b> · "
            f"{html.escape(ui_language.tr('sys.scope.local'))}"
        ),
        (
            f"<b>{html.escape(ui_language.tr('common.changes'))}</b> · "
            f"{html.escape(ui_language.tr('sys.changes.global'))}"
            if is_global
            else f"<b>{html.escape(ui_language.tr('common.changes'))}</b> · "
            f"{html.escape(ui_language.tr('sys.changes.local'))}"
        ),
        "",
        f"<b>{html.escape(ui_language.tr('sys.section.slots'))}</b>",
    ]
    for slot_id, active, text in slots:
        state = status_label(active)
        preview = text[:70].strip() + ("…" if len(text) > 70 else "")
        lines.append(
            f"<code>{slot_id}</code> · <b>{state}</b> · "
            f"{html.escape(preview or ui_language.tr('common.empty'))}"
        )
    lines.extend(
        [
            "",
            f"<b>{html.escape(ui_language.tr('common.use'))}</b>",
            _command(f"{prefix} <slot>", ui_language.tr("sys.action.view_slot")),
            _command(f"{prefix} <slot> on|off", ui_language.tr("sys.action.change_state")),
            _command(f"{prefix} <slot> save|replace <text>", ui_language.tr("sys.action.update_content")),
            _command(f"{prefix} output <slot>", ui_language.tr("sys.action.raw_content")),
        ]
    )
    if is_global:
        lines.append(_command("/sys g …", ui_language.tr("sys.action.global_alias")))
    return "\n".join(lines)


def sys_slot_text(
    manager: Any,
    slot_id: str,
    *,
    scope: str = "local",
    instance_id: str | None = None,
) -> str:
    is_global = scope == "global"
    prefix = _sys_command_prefix(scope)
    slot = manager.get_slot(slot_id) if hasattr(manager, "get_slot") else manager._slot(slot_id)
    active = bool(slot.get("active"))
    text = str(slot.get("text") or "")
    return "\n".join(
        [
            card_title("🌐" if is_global else "🧾", "Global system prompt slot" if is_global else "System prompt slot"),
            "",
            f"<b>{html.escape(ui_language.tr('common.current'))}</b> · <b>{status_label(active)}</b>",
            f"<b>{html.escape(ui_language.tr('common.slot'))}</b> · <code>{html.escape(slot_id)}</code>",
            (
                f"<b>{html.escape(ui_language.tr('common.scope'))}</b> · "
                + ui_language.tr(
                    "sys.scope.global",
                    instance=f"<code>{html.escape(instance_id or 'this HASHI instance')}</code>",
                )
                if is_global
                else f"<b>{html.escape(ui_language.tr('common.scope'))}</b> · "
                f"{html.escape(ui_language.tr('sys.scope.local'))}"
            ),
            f"<b>{html.escape(ui_language.tr('common.size'))}</b> · <code>{len(text)}</code> "
            f"{html.escape(ui_language.tr('sys.characters_suffix'))}",
            "",
            f"<pre>{html.escape(text or ui_language.tr('common.empty'))}</pre>",
            "",
            f"<b>{html.escape(ui_language.tr('common.use'))}</b>",
            _command(f"{prefix} {slot_id} on|off", ui_language.tr("sys.action.change_state")),
            _command(f"{prefix} {slot_id} replace <text>", ui_language.tr("sys.action.replace_content")),
            _command(f"{prefix} output {slot_id}", ui_language.tr("sys.action.raw_content")),
        ]
    )


def credit_status_text(data: dict[str, Any]) -> str:
    free_tier = bool(data.get("is_free_tier", False))
    return setting_card(
        "💳",
        "OpenRouter credit",
        current=(
            f"<code>{html.escape(str(data.get('limit_remaining', _tr('common.unknown'))))}</code> "
            f"{html.escape(_tr('menu.credit.remaining'))}"
        ),
        facts=[
            _fact(
                "common.key",
                f"<code>{html.escape(str(data.get('label', _tr('common.unknown'))))}</code>",
            ),
            _fact(
                "common.usage",
                f"<code>{html.escape(str(data.get('usage', _tr('common.unknown'))))}</code>",
            ),
            _fact(
                "common.limit",
                f"<code>{html.escape(str(data.get('limit', _tr('common.unknown'))))}</code>",
            ),
            _fact(
                "common.free_tier",
                f"<code>{html.escape(_tr('common.yes' if free_tier else 'common.no'))}</code>",
            ),
        ],
        consequence=_tr("menu.credit.read_only"),
        action=_tr("menu.credit.refresh"),
    )


def model_menu_text(
    *,
    model: str,
    backend: str,
    has_choices: bool,
    persists: bool,
    provider: str | None = None,
) -> str:
    facts = [_fact("common.backend", f"<code>{html.escape(backend)}</code>")]
    if provider:
        facts.append(_fact("common.provider", f"<code>{html.escape(provider)}</code>"))
    return setting_card(
        "🧠",
        "Hashi model",
        current=f"<code>{html.escape(model)}</code>",
        facts=facts,
        consequence=(
            _tr(
                "menu.model.selection_persistent"
                if persists
                else "menu.model.selection_temporary"
            )
            if has_choices
            else _tr("menu.model.typed")
        ),
        action=(
            _tr("menu.model.choose")
            if has_choices
            else _tr("menu.model.typed_action")
        ),
    )


def thinking_output_text(
    *,
    enabled: bool,
    her_backend: bool,
    reasoning_available: bool,
    commentary_available: bool,
) -> str:
    """Render backend-aware reasoning ownership without growing the runtime."""

    commentary_fact = (
        _fact(
            "menu.think.her_commentary_label",
            f"<code>{html.escape(_tr('menu.think.owned_commentary'))}</code>",
        )
        if her_backend
        else (
            _fact(
                "menu.think.model_commentary_label",
                f"<code>{html.escape(_tr('common.available' if commentary_available else 'common.not_exposed'))}</code>",
            )
        )
    )
    if enabled and her_backend:
        consequence = _tr("menu.think.enabled_her")
    elif enabled:
        consequence = _tr("menu.think.enabled_other")
    elif her_backend:
        consequence = _tr("menu.think.disabled_her")
    else:
        consequence = _tr("menu.think.disabled_other")
    return setting_card(
        "💭",
        "Thinking output",
        current=f"<b>{status_label(enabled)}</b>",
        facts=[
            (
                _fact(
                    "menu.think.reasoning_label",
                    f"<code>{html.escape(_tr('common.available' if reasoning_available else 'common.not_exposed'))}</code>",
                )
            ),
            commentary_fact,
            _fact("common.saved", html.escape(_tr("menu.setting.workspace"))),
        ],
        consequence=consequence,
        action=_tr("menu.setting.immediate_persistent_reboot"),
    )


def her_commentary_text(*, enabled: bool, effort: str) -> str:
    """Render the HER-only Persona commentary ownership contract."""

    from orchestrator.her_v2.models import effort_display_label

    try:
        effort = effort_display_label(effort)
    except ValueError:
        pass

    return setting_card(
        "🌿",
        "HER commentary",
        current=f"<b>{status_label(enabled)}</b>",
        facts=[
            _fact(
                "menu.commentary.execution_mode",
                f"<code>{html.escape(effort)}</code>",
            ),
            f"<b>{html.escape(_tr('menu.commentary.level.direct'))}</b> · <code>zero</code> · {html.escape(_tr('menu.commentary.direct'))}",
            f"<b>{html.escape(_tr('menu.commentary.level.planned'))}</b> · <code>medium</code> · {html.escape(_tr('menu.commentary.planned'))}",
            f"<b>{html.escape(_tr('menu.commentary.level.adaptive'))}</b> · <code>high+</code> · {html.escape(_tr('menu.commentary.adaptive'))}",
            _fact(
                "menu.commentary.delivery_label",
                html.escape(_tr("menu.commentary.delivery")),
            ),
            _fact(
                "menu.commentary.leases_label",
                html.escape(_tr("menu.commentary.leases")),
            ),
            _fact(
                "menu.commentary.independent_label",
                html.escape(_tr("menu.commentary.independent")),
            ),
            _fact("common.saved", html.escape(_tr("menu.setting.workspace"))),
        ],
        consequence=(
            _tr("menu.commentary.enabled")
            if enabled
            else _tr("menu.commentary.disabled")
        ),
        action=_tr("menu.commentary.action"),
    )


def her_v2_provider_menu_text(
    *,
    current_provider: str,
    available_count: int,
    unavailable: Sequence[tuple[str, str]] = (),
) -> str:
    facts = [
        _fact("common.backend", "<code>her-v2</code>"),
        _fact(
            "menu.provider.instance_configured",
            f"<code>{available_count}</code> {html.escape(_tr('menu.provider.call_providers'))}",
        ),
        _fact(
            "menu.provider.modes_label",
            html.escape(_tr("menu.provider.modes")),
        ),
    ]
    if unavailable:
        facts.append(
            _label("menu.provider.unavailable_label")
            + " · "
            + ", ".join(
                f"{html.escape(name)} ({html.escape(reason)})"
                for name, reason in unavailable
            )
        )
    return setting_card(
        "🔌",
        "HER v2 provider",
        current=f"<code>{html.escape(current_provider)}</code>",
        facts=facts,
        consequence=_tr("menu.provider.effect"),
        action=(
            _tr("menu.provider.choose")
            if available_count
            else _tr("menu.provider.none")
        ),
    )


def her_v2_provider_unavailable_text(*, backend: str) -> str:
    return setting_card(
        "🔌",
        "HER v2 provider",
        current=_state("common.unavailable"),
        facts=[
            _fact("common.backend", f"<code>{html.escape(backend)}</code>"),
            _fact("common.scope", _tr("menu.provider.her_only")),
        ],
        consequence=_tr("menu.provider.unchanged"),
        action=_tr("menu.provider.use_her"),
    )


def her_v2_model_menu_text(
    *,
    provider: str,
    routing_mode: str = "single",
    fast_provider: str = "",
    fast_model: str,
    pro_provider: str = "",
    pro_model: str,
    draft: bool = False,
) -> str:
    fast_provider = fast_provider or provider
    pro_provider = pro_provider or provider
    return setting_card(
        "🧠",
        "HER v2 models",
        current=(
            f"<code>{html.escape(routing_mode.upper())}</code>"
            + (f" · {_state('common.draft')}" if draft else "")
        ),
        facts=[
            _fact("common.provider", f"<code>{html.escape(provider)}</code>"),
            _fact(
                "menu.her.quick_target",
                f"<code>{html.escape(fast_provider)} / {html.escape(fast_model)}</code>",
            ),
            _fact(
                "menu.her.pro_target",
                f"<code>{html.escape(pro_provider)} / {html.escape(pro_model)}</code>",
            ),
        ],
        consequence=_tr("menu.her.model_effect"),
        action=(
            _tr("menu.her.review_apply")
            if draft
            else _tr("menu.her.choose_target")
        ),
    )


def her_v2_slot_model_text(
    *,
    provider: str,
    slot: str,
    current_model: str,
    model_count: int,
) -> str:
    display_slot = "Quick" if slot == "fast" else "Pro"
    return setting_card(
        "🧠",
        _tr("menu.her.slot_title", slot=display_slot),
        current=f"<code>{html.escape(current_model)}</code>",
        facts=[
            _fact("common.provider", f"<code>{html.escape(provider)}</code>"),
            _fact("menu.her.slot_label", f"<code>{display_slot}</code>"),
            _fact("menu.her.allowed_models", f"<code>{model_count}</code>"),
        ],
        consequence=_tr("menu.her.slot_effect"),
        action=_tr("menu.her.choose_model"),
    )


def her_v2_routes_text(
    *,
    route_count: int,
    explicit_reasoning_count: int,
    custom_target_count: int = 0,
    draft: bool = False,
) -> str:
    return setting_card(
        "🧭",
        "HER v2 task routes",
        current=(
            f"<code>{route_count}</code> {html.escape(_tr('menu.her.effective_routes'))}"
            + (f" · {_state('common.draft')}" if draft else "")
        ),
        facts=[
            _fact("common.target", _tr("menu.her.route_target")),
            _fact("menu.her.custom_targets", f"<code>{custom_target_count}</code>"),
            _fact(
                "menu.her.reasoning_label",
                html.escape(_tr("menu.her.reasoning")),
            ),
            _fact(
                "menu.her.custom_reasoning",
                f"<code>{explicit_reasoning_count}</code> "
                f"{html.escape(_tr('menu.her.route_overrides'))}",
            ),
        ],
        consequence=_tr("menu.her.routes_effect"),
        action=_tr("menu.her.choose_route"),
    )


def her_v2_route_text(
    *,
    label: str,
    model_slot: str,
    effective_model: str,
    reasoning: str,
    reasoning_inherited: bool,
) -> str:
    reasoning_source = _tr(
        "menu.her.reasoning_source.inherited"
        if reasoning_inherited
        else "menu.her.reasoning_source.explicit"
    )
    return setting_card(
        "🧭",
        _tr("menu.her.route_title", label=label),
        current=(
            f"<code>{html.escape(model_slot)}</code> + "
            f"<code>{html.escape(reasoning)}</code>"
        ),
        facts=[
            _fact("menu.her.target_mode", f"<code>{html.escape(model_slot)}</code>"),
            _fact(
                "menu.her.effective_target",
                f"<code>{html.escape(effective_model)}</code>",
            ),
            _fact(
                "menu.think.reasoning_label",
                f"<code>{html.escape(reasoning)}</code>",
            ),
            _fact(
                "menu.her.reasoning_source_label",
                f"<code>{html.escape(reasoning_source)}</code>",
            ),
        ],
        consequence=_tr("menu.her.route_effect"),
        action=_tr("menu.her.choose_route_values"),
    )


def her_v2_backend_selected_text(*, with_context: bool) -> str:
    return setting_card(
        "✅",
        "HER v2 selected",
        current="<code>her-v2</code>",
        facts=[
            _fact(
                "common.context",
                f"<code>{'HANDOFF' if with_context else 'FRESH'}</code>",
            ),
        ],
        consequence=_tr("menu.her.backend_changed"),
        action=_tr("menu.her.independent_controls"),
    )


def backend_menu_text(*, active_backend: str) -> str:
    return setting_card(
        "🧠",
        "Hashi backend",
        current=f"<code>{html.escape(active_backend)}</code>",
        consequence=_tr("menu.backend.context_effect"),
        action=_tr("menu.backend.choose"),
    )


def backend_model_prompt_text(
    *,
    backend: str,
    current_model: str,
    with_context: bool,
) -> str:
    mode_text = _tr(
        "menu.backend.with_handoff"
        if with_context
        else "menu.backend.without_handoff"
    )
    return setting_card(
        "🧠",
        "Choose model",
        current=f"<code>{html.escape(current_model or 'auto')}</code>",
        facts=[
            _fact("common.backend", f"<code>{html.escape(backend)}</code>"),
            _fact("common.switch", html.escape(mode_text)),
        ],
        action=_tr("menu.backend.select_model"),
    )


def loop_manager_text() -> str:
    return setting_card(
        "🔄",
        "Loop manager",
        current=_state("common.ready"),
        facts=[_fact("common.scope", html.escape(_tr("menu.loop.scope")))],
        consequence=_tr("menu.loop.effect"),
        action=(
            _command_key("/loop <task>", "menu.loop.action.create")
            + "\n"
            + _command_key("/loop list", "menu.loop.action.list")
            + "\n"
            + _command_key("/loop stop [id]", "menu.loop.action.stop")
        ),
    )


def loop_list_text(loops: Iterable[tuple[str, dict[str, Any]]]) -> str:
    rows = list(loops)
    enabled_count = sum(bool(job.get("enabled")) for _kind, job in rows)
    lines = [
        card_title("🔄", "Loops"),
        "",
        _fact(
            "common.current",
            _tr(
                "menu.loop.current",
                active=f"<code>{enabled_count}</code>",
                configured=f"<code>{len(rows)}</code>",
            ),
        ),
        _fact("common.changes", html.escape(_tr("menu.loop.changes"))),
    ]
    if not rows:
        lines.extend(["", html.escape(_tr("menu.loop.none"))])
    for job_kind, job in rows:
        meta = job.get("loop_meta") or {}
        enabled = bool(job.get("enabled"))
        count = int(meta.get("count", 0) or 0)
        maximum = int(meta.get("max", 100) or 100)
        schedule = (
            _tr(
                "menu.loop.every_seconds",
                seconds=job.get("interval_seconds", "?"),
            )
            if job_kind == "heartbeat"
            else str(job.get("schedule") or _tr("common.unknown"))
        )
        job_id = html.escape(str(job.get("id") or _tr("common.unknown")))
        summary = html.escape(
            str(meta.get("task_summary") or job.get("note") or "")[:90]
        )
        reason = html.escape(str(meta.get("stopped_reason") or ""))
        lines.extend(
            [
                "",
                f"<b>{status_label(enabled)}</b> · <code>{job_id}</code>",
                _fact(
                    "common.schedule",
                    f"<code>{html.escape(schedule)}</code> · "
                    f"{html.escape(_tr(f'menu.loop.kind.{job_kind}') if job_kind in {'heartbeat', 'cron'} else job_kind)}",
                ),
                _fact("common.progress", f"<code>{count}/{maximum}</code>"),
            ]
        )
        if summary:
            lines.append(summary)
        if reason:
            lines.append(f"⚠️ {reason}")
    lines.extend(["", _command_key("/jobs", "menu.loop.open_jobs")])
    return "\n".join(lines)


def debug_menu_text(*, enabled: bool) -> str:
    return setting_card(
        "🐛",
        "Debug mode",
        current=f"<b>{status_label(enabled)}</b>",
        facts=[_fact("common.scope", html.escape(_tr("menu.debug.scope")))],
        consequence=_tr("menu.debug.effect"),
        action=(
            _command_key("/debug on|off", "menu.debug.action.toggle")
            + "\n"
            + _command_key("/debug <prompt>", "menu.debug.action.run")
        ),
    )


def skills_menu_text(
    *,
    count: int,
    agent_name: str,
    enabled_count: int | None = None,
    invalid_count: int = 0,
) -> str:
    enabled_count = count if enabled_count is None else enabled_count
    return setting_card(
        "🧰",
        "Skills",
        current=_tr("menu.skill.count_available", count=f"<code>{count}</code>"),
        facts=[
            _fact("common.agent", f"<code>{html.escape(agent_name)}</code>"),
            _fact("menu.skill.enabled_here", f"<code>{enabled_count}/{count}</code>"),
            _fact("menu.skill.invalid_label", f"<code>{invalid_count}</code>"),
            _fact("common.format", _tr("menu.skill.standard_format")),
        ],
        consequence=_tr("menu.skill.effect"),
        action=_tr("menu.skill.choose"),
    )


def skill_detail_text(skill: Any, workspace_dir: Any, *, manager: Any) -> str:
    skill_id = str(getattr(skill, "id", _tr("common.unknown")) or _tr("common.unknown"))
    skill_name = str(getattr(skill, "name", skill_id) or skill_id)
    description = str(getattr(skill, "description", "") or _tr("menu.skill.no_description"))
    enabled_method = getattr(manager, "is_skill_enabled", None)
    enabled = (
        bool(enabled_method(workspace_dir, skill_id))
        if callable(enabled_method)
        else None
    )
    source_type = str(getattr(skill, "source_type", "project") or "project")
    scope = str(getattr(skill, "scope", "project") or "project")
    source = str(getattr(skill, "source", "") or "")
    source_labels = {
        "project": _tr("menu.skill.source.project"),
        "installed": _tr("menu.skill.source.installed"),
        "linked": _tr("menu.skill.source.linked"),
    }
    resource_method = getattr(manager, "skill_resource_counts", None)
    resources = (
        resource_method(skill)
        if callable(resource_method)
        else {"scripts": 0, "references": 0, "assets": 0, "other": 0}
    )
    dependency_method = getattr(manager, "skill_dependencies", None)
    dependencies = dependency_method(skill_id) if callable(dependency_method) else []
    usage_method = getattr(manager, "skill_usage_stats", None)
    usage_stats = (
        usage_method(skill_id)
        if callable(usage_method)
        else {"total": 0, "agents": 0}
    )
    facts = [
        _fact("common.id", f"<code>{html.escape(skill_id)}</code>"),
        _fact(
            "common.source",
            f"<code>{html.escape(source_labels.get(source_type, source_type.upper()))}</code>",
        ),
        _fact("common.scope", f"<code>{html.escape(scope)}</code>"),
        _fact("common.format", "<code>SKILL.md</code>"),
        (
            _label("menu.skill.uses")
            + " · "
            + f"<code>{int(usage_stats.get('total', 0))}</code> "
            + html.escape(_tr("menu.skill.cumulative"))
            + " · "
            + f"<code>{int(usage_stats.get('agents', 0))}</code> "
            + html.escape(_tr("menu.skill.agents_suffix"))
        ),
        _fact("menu.skill.usage_log", "<code>state/skill_usage.jsonl</code>"),
        (
            _label("menu.skill.resources")
            + " · "
            + f"{html.escape(_tr('menu.skill.scripts'))} <code>{int(resources.get('scripts', 0))}</code> · "
            + f"{html.escape(_tr('menu.skill.references'))} <code>{int(resources.get('references', 0))}</code> · "
            + f"{html.escape(_tr('menu.skill.assets'))} <code>{int(resources.get('assets', 0))}</code>"
        ),
        _fact("menu.skill.job_references", f"<code>{len(dependencies)}</code>"),
    ]
    version = str(getattr(skill, "version", "") or "")
    if version:
        facts.append(_fact("common.version", f"<code>{html.escape(version[:120])}</code>"))
    author = str((getattr(skill, "metadata", {}) or {}).get("author") or "")
    if author:
        facts.append(_fact("common.author", f"<code>{html.escape(author[:120])}</code>"))
    license_name = str(getattr(skill, "license", "") or "")
    if license_name:
        facts.append(_fact("common.license", f"<code>{html.escape(license_name[:120])}</code>"))
    compatibility = str(getattr(skill, "compatibility", "") or "")
    if compatibility:
        facts.append(_fact("common.compatibility", html.escape(compatibility[:240])))
    allowed_tools = str(getattr(skill, "allowed_tools", "") or "")
    if allowed_tools:
        facts.append(
            _fact(
                "menu.skill.declared_tools",
                f"<code>{html.escape(allowed_tools[:180])}</code>",
            )
        )
    if source:
        facts.append(_fact("common.path", f"<code>{html.escape(source[:240])}</code>"))
    if enabled is None:
        current = _state("common.ready")
    else:
        current = _state("common.enabled_state" if enabled else "common.disabled_state")
    usage = f"/skill {skill_id} <request>"

    text = setting_card(
        "🧰",
        skill_name,
        current=current,
        facts=facts,
        consequence=html.escape(description),
        action=_tr("menu.skill.action", usage=html.escape(usage)),
    )
    body = str(getattr(skill, "body", "") or "").strip()
    if body:
        preview = (
            body
            if len(body) <= 700
            else body[:700].rstrip() + "\n\n" + _tr("menu.skill.truncated")
        )
        text += f"\n\n{_label('menu.skill.reference')}\n<pre>{html.escape(preview)}</pre>"
    return text


def skill_validation_text(skill: Any, *, manager: Any) -> str:
    skill_id = str(getattr(skill, "id", _tr("common.unknown")) or _tr("common.unknown"))
    validate = getattr(manager, "validate_skill", None)
    ok, errors = validate(skill_id) if callable(validate) else (True, [])
    lines = [
        card_title("🧪", "Skill validation"),
        "",
        _fact("common.current", _state("common.valid" if ok else "common.invalid")),
        _fact("menu.skill.skill_label", f"<code>{html.escape(skill_id)}</code>"),
        _fact("menu.skill.contract", _tr("menu.skill.contract_value")),
    ]
    if errors:
        lines.extend(["", _label("menu.skill.errors")])
        for error in errors[:8]:
            lines.append(f"• {html.escape(str(error)[:500])}")
    else:
        lines.extend(
            [
                "",
                _tr("menu.skill.valid_body"),
            ]
        )
    return "\n".join(lines)


def skill_invalid_packages_text(errors: Sequence[str]) -> str:
    lines = [
        card_title("⚠️", "Invalid Skill packages"),
        "",
        _fact(
            "common.current",
            _tr("menu.skill.invalid_count", count=f"<code>{len(errors)}</code>"),
        ),
        _tr("menu.skill.invalid_effect"),
    ]
    if not errors:
        lines.extend(["", _tr("menu.skill.no_validation_errors")])
    else:
        lines.extend(["", _label("menu.skill.errors")])
        used = 0
        for error in errors:
            rendered = f"• {html.escape(str(error)[:600])}"
            if used + len(rendered) > 3000:
                lines.append(
                    _tr("menu.skill.errors_clipped")
                )
                break
            lines.append(rendered)
            used += len(rendered)
    return "\n".join(lines)


def skill_install_help_text() -> str:
    return "\n".join(
        [
            card_title("➕", "Install Skill"),
            "",
            _fact("common.current", _state("common.ready")),
            _fact("common.scope", html.escape(_tr("menu.skill.install_scope"))),
            "",
            _tr("menu.skill.install_source"),
            _tr("menu.skill.install_recovery"),
            "",
            _label("common.use"),
            _command_key("/skill install <directory>", "menu.skill.action.install"),
            _command_key("/skill link <directory>", "menu.skill.action.link"),
        ]
    )


def skill_find_help_text() -> str:
    return setting_card(
        "🔎",
        "Find Skills",
        current=_state("common.ready"),
        facts=[
            _fact(
                "menu.skill.search_fields",
                html.escape(_tr("menu.skill.search_fields_value")),
            )
        ],
        action=_command_key("/skill find <text>", "menu.skill.action.search"),
    )


def skill_search_results_text(query: str, skills: Sequence[Any]) -> str:
    lines = [
        card_title("🔎", "Skill search"),
        "",
        _fact("common.query", f"<code>{html.escape(query)}</code>"),
        _fact("common.matches", f"<code>{len(skills)}</code>"),
        "",
    ]
    if not skills:
        lines.append(_tr("menu.skill.none_matching"))
    else:
        for skill in skills[:30]:
            skill_id = html.escape(str(getattr(skill, "id", _tr("common.unknown"))))
            description = html.escape(str(getattr(skill, "description", ""))[:160])
            lines.append(f"• <code>{skill_id}</code> · {description}")
    return "\n".join(lines)


def skill_disable_confirm_text(
    skill: Any, dependencies: Sequence[dict[str, Any]]
) -> str:
    skill_id = str(getattr(skill, "id", _tr("common.unknown")) or _tr("common.unknown"))
    consequence = _tr("menu.skill.disable_effect")
    if dependencies:
        labels = ", ".join(
            html.escape(str(item.get("id") or _tr("common.unknown")))
            for item in dependencies[:5]
        )
        consequence += " " + _tr("menu.skill.jobs_affected", jobs=labels)
    return confirm_card(
        "⏸",
        "Disable Skill",
        target=f"<code>{html.escape(skill_id)}</code>",
        consequence=consequence,
    )


def skill_uninstall_confirm_text(
    skill: Any, dependencies: Sequence[dict[str, Any]]
) -> str:
    skill_id = str(getattr(skill, "id", _tr("common.unknown")) or _tr("common.unknown"))
    source_type = str(getattr(skill, "source_type", "project") or "project")
    if dependencies:
        labels = ", ".join(
            html.escape(str(item.get("id") or _tr("common.unknown")))
            for item in dependencies[:5]
        )
        consequence = _tr("menu.skill.remove_blocked", jobs=labels)
    elif source_type == "linked":
        consequence = _tr("menu.skill.unlink_effect")
    elif source_type == "project":
        consequence = _tr("menu.skill.delete_builtin_effect")
    else:
        consequence = _tr("menu.skill.uninstall_effect")
    titles = {
        "project": "Delete Skill",
        "linked": "Unlink Skill",
        "installed": "Uninstall Skill",
    }
    return confirm_card(
        "🗑️",
        titles.get(source_type, "Delete Skill"),
        target=f"<code>{html.escape(skill_id)}</code>",
        consequence=consequence,
    )


def hchat_help_text() -> str:
    return "\n".join(
        [
            card_title("💬", "Hashi chat"),
            "",
            _fact("common.current", _state("common.ready")),
            _fact("common.scope", html.escape(_tr("menu.hchat.scope"))),
            "",
            _tr("menu.hchat.effect"),
            "",
            _label("common.use"),
            _command_key("/hchat <agent> <intent>", "menu.hchat.action.local"),
            _command_key(
                "/hchat <agent>@<INSTANCE> <intent>", "menu.hchat.action.remote"
            ),
            _command_key("/hchat @<group> <intent>", "menu.hchat.action.group"),
            _command_key("/hchat all <intent>", "menu.hchat.action.all"),
            "",
            _label("common.example"),
            "<code>/hchat arale review the latest test result</code>",
        ]
    )


def cos_menu_text(*, enabled: bool) -> str:
    return setting_card(
        "🌸",
        "Chief of Staff routing",
        current=f"<b>{status_label(enabled)}</b>",
        facts=[_fact("common.route", html.escape(_tr("menu.cos.route")))],
        consequence=(
            _tr("menu.cos.enabled")
            if enabled
            else _tr("menu.cos.disabled")
        ),
        action=_tr("menu.cos.action"),
    )


def safevoice_menu_text(*, enabled: bool) -> str:
    return setting_card(
        "🛡️",
        "Safe voice",
        current=f"<b>{status_label(enabled)}</b>",
        facts=[_fact("common.scope", html.escape(_tr("menu.safevoice.scope")))],
        consequence=(
            _tr("menu.safevoice.enabled")
            if enabled
            else _tr("menu.safevoice.disabled")
        ),
        action=_tr("menu.safevoice.action"),
    )


def safevoice_keyboard(*, enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    selected_label(_tr("common.on"), enabled),
                    callback_data="safevoice:set:on",
                ),
                InlineKeyboardButton(
                    selected_label(_tr("common.off"), not enabled),
                    callback_data="safevoice:set:off",
                ),
            ]
        ]
    )


def timeout_menu_text(
    *,
    agent_name: str,
    backend_name: str,
    idle_minutes: int,
    default_idle_minutes: int,
    idle_source: str,
) -> str:
    return setting_card(
        "⏱️",
        "Backend timeout",
        current=(
            f"{html.escape(_tr('menu.timeout.idle'))} "
            f"<code>{idle_minutes} {html.escape(_tr('menu.timeout.minutes'))}</code>"
        ),
        facts=[
            _fact(
                "menu.timeout.default_idle",
                f"<code>{default_idle_minutes} {html.escape(_tr('menu.timeout.minutes'))}</code>",
            ),
            _fact("common.agent", f"<code>{html.escape(agent_name)}</code>"),
            _fact("common.backend", f"<code>{html.escape(backend_name)}</code>"),
            _fact("common.source", f"<code>{html.escape(idle_source)}</code>"),
            _fact("common.scope", html.escape(_tr("menu.timeout.scope"))),
        ],
        consequence=_tr("menu.timeout.effect"),
        action=(
            _command_key("/timeout 60", "menu.timeout.action.set")
            + "\n"
            + _command_key("/timeout reset", "menu.timeout.action.reset")
        ),
    )


def wol_targets_text(
    targets: Sequence[dict[str, Any]], *, instance_id: str | None
) -> str:
    lines = [
        card_title("🪄", "Wake-on-LAN targets"),
        "",
        _fact(
            "common.current",
            _tr("menu.wol.current", count=f"<code>{len(targets)}</code>"),
        ),
        _fact("common.instance", f"<code>{html.escape(instance_id or 'local')}</code>"),
        _fact("common.changes", html.escape(_tr("menu.wol.changes"))),
    ]
    if targets:
        lines.extend(["", _label("menu.wol.section")])
        for row in targets:
            name = html.escape(str(row.get("name") or _tr("common.unknown")))
            label = html.escape(str(row.get("label") or name))
            description = html.escape(str(row.get("description") or ""))
            lines.append(
                f"<code>{name}</code> · <b>{label}</b>"
                + (f" · {description}" if description else "")
            )
    else:
        lines.extend(["", _tr("menu.wol.none")])
    lines.extend(["", _command_key("/wol <pc_name>", "menu.wol.action.send")])
    return "\n".join(lines)
