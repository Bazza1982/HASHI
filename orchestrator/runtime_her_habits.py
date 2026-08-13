from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
from dataclasses import dataclass
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from adapters import her_habits as _her_habits
from orchestrator.command_ui import (
    BACK_LABEL,
    REFRESH_LABEL,
    card_title,
    confirm_card,
    selected_label,
    setting_card,
    status_label,
)

HABIT_PAGE_SIZE = 5


@dataclass(frozen=True)
class HabitControlStatus:
    effective: bool
    source: str
    override: bool | None
    global_default: bool
    environment_locked: bool
    environment_value: str | None


def _parse_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().casefold()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _active_engine(runtime: Any) -> str:
    engine = getattr(getattr(runtime, "config", None), "active_backend", None)
    if not engine:
        backend = getattr(
            getattr(runtime, "backend_manager", None), "current_backend", None
        )
        engine = getattr(getattr(backend, "config", None), "engine", None)
    return str(engine or "").strip().casefold()


def _her_adapter(runtime: Any) -> Any | None:
    if _active_engine(runtime) != "her":
        return None
    backend = getattr(
        getattr(runtime, "backend_manager", None), "current_backend", None
    )
    if str(getattr(getattr(backend, "config", None), "engine", "")).casefold() != "her":
        return None
    return backend


def _habit_store(runtime: Any, adapter: Any) -> _her_habits.HERHabitStore:
    getter = getattr(adapter, "_her_habit_store", None)
    if callable(getter):
        return getter()
    return _her_habits.HERHabitStore(runtime.workspace_dir, logger=runtime.logger)


def _habit_journal(runtime: Any, adapter: Any) -> _her_habits.HERMeditationJournal:
    getter = getattr(adapter, "_her_meditation_journal", None)
    if callable(getter):
        return getter()
    return _her_habits.HERMeditationJournal(
        runtime.workspace_dir, logger=runtime.logger
    )


def _control_status(runtime: Any, adapter: Any) -> HabitControlStatus:
    manager = runtime.backend_manager
    override_getter = getattr(manager, "get_habit_meditation_override", None)
    override = override_getter() if callable(override_getter) else None
    global_her = (
        getattr(runtime.global_config, "her_providers", None)
        or getattr(runtime.global_config, "claw_providers", None)
        or {}
    )
    global_raw = (
        global_her.get("habit_meditation") if isinstance(global_her, dict) else None
    )
    global_default = _parse_bool(
        global_raw.get("enabled") if isinstance(global_raw, dict) else global_raw,
        default=False,
    )
    extra = dict(getattr(getattr(adapter, "config", None), "extra", None) or {})
    backend_raw = extra.get("habit_meditation")
    env_value = os.environ.get(_her_habits.HABIT_MEDITATION_ENV)
    env_locked = str(env_value or "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
        "0",
        "false",
        "no",
        "off",
        "disabled",
    }
    effective = bool(adapter._habit_meditation_config().enabled)
    if env_locked:
        source = "environment override"
    elif override is not None:
        source = "agent override"
    elif "habit_meditation_enabled" in extra:
        source = "HER backend config"
    elif isinstance(backend_raw, dict) and "enabled" in backend_raw:
        source = "HER backend config"
    elif isinstance(backend_raw, bool):
        source = "HER backend config"
    elif global_raw is not None:
        source = "HASHI global default"
    else:
        source = "built-in default"
    return HabitControlStatus(
        effective=effective,
        source=source,
        override=override,
        global_default=global_default,
        environment_locked=env_locked,
        environment_value=env_value,
    )


def _habit_token(habit_id: str) -> str:
    return hashlib.sha256(habit_id.encode("utf-8")).hexdigest()[:16]


def _habit_version_token(habit: _her_habits.HERHabit) -> str:
    payload = json.dumps(
        habit.to_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _resolve_token(
    habits: list[_her_habits.HERHabit], token: str
) -> _her_habits.HERHabit | None:
    matches = [habit for habit in habits if _habit_token(habit.habit_id) == token]
    return matches[0] if len(matches) == 1 else None


def _resolve_version_token(
    habits: list[_her_habits.HERHabit], token: str
) -> _her_habits.HERHabit | None:
    matches = [habit for habit in habits if _habit_version_token(habit) == token]
    return matches[0] if len(matches) == 1 else None


def _sorted_habits(store: _her_habits.HERHabitStore) -> list[_her_habits.HERHabit]:
    return sorted(
        store.load(),
        key=lambda habit: (habit.updated_at, habit.habit_id),
        reverse=True,
    )


def _bounded_offset(offset: int, total: int) -> int:
    if total <= 0:
        return 0
    maximum = ((total - 1) // HABIT_PAGE_SIZE) * HABIT_PAGE_SIZE
    return min(maximum, max(0, int(offset)))


def _short(value: str, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _html_short(value: str, limit: int) -> str:
    """Escape text while bounding the final Telegram HTML payload."""

    escaped: list[str] = []
    size = 0
    for character in str(value or "").strip():
        fragment = html.escape(character)
        if size + len(fragment) > max(1, limit - 1):
            return "".join(escaped).rstrip() + "…"
        escaped.append(fragment)
        size += len(fragment)
    return "".join(escaped)


def _audit_context(runtime: Any, update: Any, *, source: str) -> dict[str, Any]:
    query = getattr(update, "callback_query", None)
    user = getattr(update, "effective_user", None) or getattr(query, "from_user", None)
    chat = getattr(update, "effective_chat", None)
    if chat is None and query is not None:
        chat = getattr(getattr(query, "message", None), "chat", None)
    return {
        "source": source,
        "agent_id": str(getattr(runtime, "name", "")),
        "actor_id": getattr(user, "id", None),
        "chat_id": getattr(chat, "id", None),
    }


def _append_audit(runtime: Any, event: str, **fields: Any) -> None:
    try:
        _her_habits.append_habit_audit(
            runtime.workspace_dir,
            event,
            agent_id=str(runtime.name),
            **fields,
        )
    except Exception as exc:  # noqa: BLE001 - command remains usable if audit is degraded
        runtime.logger.warning(
            "HER Habit command audit failed: event=%s error=%s",
            event,
            type(exc).__name__,
        )


def _unavailable_view(runtime: Any) -> tuple[str, InlineKeyboardMarkup]:
    backend = _active_engine(runtime) or "unknown"
    return (
        setting_card(
            "🧠",
            "HER habits",
            current="<b>UNAVAILABLE</b>",
            facts=[
                f"<b>Backend</b> · <code>{html.escape(backend)}</code>",
                "<b>Scope</b> · <code>/habit</code> is available only for <code>her</code>",
            ],
            consequence="No dormant HER Habit data was read or changed.",
            action="Switch to HER to view and manage this agent's Habits.",
        ),
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Switch to HER", callback_data="backend:her:plain"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "← Keep current backend", callback_data="habit:keep"
                    )
                ],
            ]
        ),
    )


def _home_view(
    runtime: Any,
    adapter: Any,
    *,
    offset: int = 0,
    notice: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    store = _habit_store(runtime, adapter)
    journal = _habit_journal(runtime, adapter)
    habits = _sorted_habits(store)
    offset = _bounded_offset(offset, len(habits))
    page = habits[offset : offset + HABIT_PAGE_SIZE]
    status = _control_status(runtime, adapter)
    pending = len(journal.pending_jobs(limit=10_000))
    pending_notifications = len(journal.pending_notifications(limit=10_000))
    journal_total = (
        len(list(journal.root.glob("*.json"))) if journal.root.is_dir() else 0
    )
    lines = [
        card_title("🧠", "HER habits"),
        "",
        f"<b>Current</b> · <b>{status_label(status.effective)}</b>",
        f"<b>Source</b> · {html.escape(status.source)}",
        f"<b>Active</b> · <code>{len(habits)}</code>",
        f"<b>Archived</b> · <code>{store.archived_count()}</code>",
        f"<b>Meditation jobs</b> · <code>{journal_total}</code> total · <code>{pending}</code> pending",
        f"<b>Habit notices</b> · <code>{pending_notifications}</code> pending delivery",
        "<b>Changes</b> · immediate and persistent for this agent's HER backend",
    ]
    if status.environment_locked:
        lines.extend(
            [
                "",
                "⚠️ The ON/OFF state is locked by "
                f"<code>{html.escape(_her_habits.HABIT_MEDITATION_ENV)}</code>="
                f"<code>{html.escape(str(status.environment_value or ''))}</code>.",
            ]
        )
    if notice:
        lines.extend(["", notice])
    lines.extend(["", "<b>HABITS</b>"])
    if not page:
        lines.append("No active Habits are stored for this agent.")
    else:
        for index, habit in enumerate(page, start=offset + 1):
            lines.append(
                f"<code>{index}</code> · <b>{html.escape(_short(habit.title, 58))}</b>"
            )
            lines.append(
                f"   <code>{html.escape(habit.habit_id)}</code> · updated "
                f"<code>{html.escape(habit.updated_at)}</code>"
            )
    lines.extend(
        [
            "",
            "<b>Use</b>",
            "<code>/habit view &lt;habit-id&gt;</code> · view details",
            "<code>/habit on|off|default</code> · control learning",
            "<code>/habit delete &lt;habit-id&gt;</code> · archive one Habit",
            "<code>/habit delete all</code> · archive every active Habit",
            "<code>/habit reset</code> · snapshot and clear all Habit state",
        ]
    )

    rows: list[list[InlineKeyboardButton]] = []
    if status.environment_locked:
        rows.append(
            [
                InlineKeyboardButton("🔒 On", callback_data="habit:locked"),
                InlineKeyboardButton("🔒 Off", callback_data="habit:locked"),
                InlineKeyboardButton("🔒 Default", callback_data="habit:locked"),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    selected_label("On", status.override is True),
                    callback_data="habit:set:on",
                ),
                InlineKeyboardButton(
                    selected_label("Off", status.override is False),
                    callback_data="habit:set:off",
                ),
                InlineKeyboardButton(
                    selected_label("Default", status.override is None),
                    callback_data="habit:set:default",
                ),
            ]
        )
    for habit in page:
        rows.append(
            [
                InlineKeyboardButton(
                    _short(habit.title, 42),
                    callback_data=f"habit:view:{_habit_token(habit.habit_id)}:{offset}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                "← Previous",
                callback_data=f"habit:home:{max(0, offset - HABIT_PAGE_SIZE)}",
            )
        )
    nav.append(
        InlineKeyboardButton(REFRESH_LABEL, callback_data=f"habit:home:{offset}")
    )
    if offset + HABIT_PAGE_SIZE < len(habits):
        nav.append(
            InlineKeyboardButton(
                "Next →",
                callback_data=f"habit:home:{offset + HABIT_PAGE_SIZE}",
            )
        )
    rows.append(nav)
    if habits:
        rows.append(
            [InlineKeyboardButton("Delete all", callback_data="habit:delete_all")]
        )
    rows.append(
        [InlineKeyboardButton("Reset Habit state", callback_data="habit:reset")]
    )
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _detail_view(
    habit: _her_habits.HERHabit,
    *,
    offset: int,
) -> tuple[str, InlineKeyboardMarkup]:
    title = _html_short(habit.title, 500)
    metadata = _html_short(habit.metadata, 600)
    body = _html_short(habit.body, 1_500)
    text = "\n".join(
        [
            card_title("🧠", "HER Habit detail"),
            "",
            "<b>Current</b> · <b>ACTIVE</b>",
            "<b>Scope</b> · this agent · <code>her</code> only",
            f"<b>ID</b> · <code>{html.escape(habit.habit_id)}</code>",
            f"<b>Created</b> · <code>{html.escape(habit.created_at)}</code>",
            f"<b>Updated</b> · <code>{html.escape(habit.updated_at)}</code>",
            "",
            f"<b>{title}</b>",
            "",
            "<b>WHEN RELEVANT</b>",
            metadata,
            "",
            "<b>BEHAVIOUR</b>",
            body,
            "",
            "Delete moves this Habit into the recoverable archive and removes it from future HER Planning.",
        ]
    )
    token = _habit_token(habit.habit_id)
    return (
        text,
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Show full content",
                        callback_data=f"habit:full:{token}:{offset}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Delete", callback_data=f"habit:delete:{token}:{offset}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        BACK_LABEL, callback_data=f"habit:home:{offset}"
                    ),
                    InlineKeyboardButton(
                        REFRESH_LABEL, callback_data=f"habit:view:{token}:{offset}"
                    ),
                ],
            ]
        ),
    )


def _full_detail_text(habit: _her_habits.HERHabit) -> str:
    return "\n".join(
        [
            f"HER Habit: {habit.title}",
            f"ID: {habit.habit_id}",
            f"Created: {habit.created_at}",
            f"Updated: {habit.updated_at}",
            "",
            "WHEN RELEVANT",
            habit.metadata,
            "",
            "BEHAVIOUR",
            habit.body,
        ]
    )


def _active_fingerprint(habits: list[_her_habits.HERHabit]) -> str:
    payload = json.dumps(
        sorted(
            (habit.to_payload() for habit in habits),
            key=lambda item: item["id"],
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _delete_confirm_view(
    habit: _her_habits.HERHabit,
    *,
    offset: int,
) -> tuple[str, InlineKeyboardMarkup]:
    token = _habit_token(habit.habit_id)
    version_token = _habit_version_token(habit)
    return (
        confirm_card(
            "⚠️",
            "Delete HER Habit",
            target=(
                f"<code>{html.escape(habit.habit_id)}</code> · "
                f"{html.escape(habit.title)}"
            ),
            consequence=(
                "This Habit will stop influencing future HER tasks immediately. "
                "It will be moved to the recoverable archive."
            ),
        ),
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Delete",
                        callback_data=f"habit:confirm_delete:{version_token}:{offset}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "← Keep Habit", callback_data=f"habit:view:{token}:{offset}"
                    )
                ],
            ]
        ),
    )


def _delete_all_confirm_view(
    runtime: Any,
    habits: list[_her_habits.HERHabit],
) -> tuple[str, InlineKeyboardMarkup]:
    fingerprint = _active_fingerprint(habits)
    text = confirm_card(
        "⚠️",
        "Delete all HER Habits",
        target=f"<code>{html.escape(str(runtime.name))}</code> · all <code>{len(habits)}</code> active Habits",
        consequence=(
            "Every active Habit will move to the recoverable archive. Meditation stays "
            "configured and may form new Habits later."
        ),
    )
    return (
        text,
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Delete all", callback_data=f"habit:confirm_all:{fingerprint}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "← Keep all Habits", callback_data="habit:home:0"
                    )
                ],
            ]
        ),
    )


def _reset_confirm_view(
    runtime: Any,
    store: _her_habits.HERHabitStore,
    journal: _her_habits.HERMeditationJournal,
) -> tuple[str, InlineKeyboardMarkup]:
    active = len(store.load())
    archived = store.archived_count()
    jobs = len(list(journal.root.glob("*.json"))) if journal.root.is_dir() else 0
    fingerprint = _her_habits.habit_state_fingerprint(runtime.workspace_dir)
    text = confirm_card(
        "⚠️",
        "Reset HER Habit state",
        target=f"<code>{html.escape(str(runtime.name))}</code> · HER Habit state",
        consequence=(
            "A recoverable snapshot will be created, then active Habits, archived Habits, "
            "and Meditation jobs will be cleared. The ON/OFF setting and audit log remain."
        ),
    )
    text += (
        f"\n\n<b>Active</b> · <code>{active}</code>"
        f"\n<b>Archived</b> · <code>{archived}</code>"
        f"\n<b>Meditation jobs</b> · <code>{jobs}</code>"
    )
    return (
        text,
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Reset Habit state",
                        callback_data=f"habit:confirm_reset:{fingerprint}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "← Keep current state", callback_data="habit:home:0"
                    )
                ],
            ]
        ),
    )


def _mutation_busy(runtime: Any, adapter: Any) -> bool:
    busy = getattr(runtime, "_backend_busy", None)
    if callable(busy) and busy():
        return True
    lock = getattr(adapter, "_habit_execution_lock", None)
    if lock is not None and lock.locked():
        return True
    return any(
        not task.done()
        for task in (
            list(getattr(adapter, "_habit_meditation_tasks", set()) or set())
            + list(getattr(adapter, "_habit_notification_tasks", set()) or set())
        )
    )


def _notification_view(job: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    changes = [item for item in (job.get("changes") or []) if isinstance(item, dict)]
    shown_changes = changes[:5]
    notification = (
        job.get("notification") if isinstance(job.get("notification"), dict) else {}
    )
    operation_labels = {
        "created": ("🌱", "FORMED"),
        "updated": ("✏️", "CHANGED"),
        "deleted": ("📦", "ARCHIVED"),
    }
    lines = [
        card_title("🧠", "HER Habit update"),
        "",
        f"<b>Current</b> · <b>{len(changes)} CHANGE{'S' if len(changes) != 1 else ''}</b>",
        f"<b>Meditation job</b> · <code>{html.escape(str(job.get('job_id') or 'unknown'))}</code>",
    ]
    summary = _html_short(str(notification.get("request_summary") or ""), 500)
    if summary:
        lines.append(f"<b>Task</b> · {summary}")
    lines.extend(["", "<b>CHANGES</b>"])
    for change in shown_changes:
        operation = str(change.get("operation") or "changed").casefold()
        icon, label = operation_labels.get(operation, ("•", "CHANGED"))
        payload = change.get("after") or change.get("before") or {}
        title = _html_short(
            str(payload.get("title") or change.get("habit_id") or "Habit"),
            300,
        )
        habit_id = str(change.get("habit_id") or "")
        lines.append(f"{icon} <b>{label}</b> · {title}")
        if habit_id:
            lines.append(f"   <code>{html.escape(habit_id)}</code>")
    if len(changes) > len(shown_changes):
        lines.append(
            f"• <b>PLUS</b> · <code>{len(changes) - len(shown_changes)}</code> more changes"
        )
    lines.extend(
        [
            "",
            "This notice was sent because Verbose was ON when the task started.",
            "Open <code>/habit</code> to inspect or manage the full HER Habit state.",
        ]
    )
    rows: list[list[InlineKeyboardButton]] = []
    if len(changes) == 1 and str(changes[0].get("operation") or "") in {
        "created",
        "updated",
    }:
        habit_id = str(changes[0].get("habit_id") or "")
        if habit_id:
            rows.append(
                [
                    InlineKeyboardButton(
                        "View Habit",
                        callback_data=f"habit:view:{_habit_token(habit_id)}:0",
                    )
                ]
            )
    rows.append([InlineKeyboardButton("Open Habit menu", callback_data="habit:home:0")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def deliver_habit_notification(runtime: Any, job: dict[str, Any]) -> bool | None:
    notification = (
        job.get("notification") if isinstance(job.get("notification"), dict) else {}
    )
    chat_id = notification.get("chat_id")
    if not bool(getattr(runtime, "telegram_connected", False)) or chat_id is None:
        return None
    text, markup = _notification_view(job)
    _append_audit(
        runtime,
        "habit_notification_delivery_started",
        job_id=job.get("job_id"),
        request_id=job.get("request_id"),
        changes=job.get("changes") or [],
        notification=notification,
    )
    sent = await runtime._send_text(
        int(chat_id),
        text,
        parse_mode="HTML",
        reply_markup=markup,
        _request_id=job.get("request_id"),
        _purpose="her_habit_notification",
    )
    if sent is None:
        _append_audit(
            runtime,
            "habit_notification_delivery_deferred",
            job_id=job.get("job_id"),
            request_id=job.get("request_id"),
            changes=job.get("changes") or [],
            notification=notification,
        )
        return None
    return True


def resume_pending_habit_notifications(runtime: Any) -> int:
    adapter = _her_adapter(runtime)
    resume = getattr(adapter, "_resume_pending_habit_notifications", None)
    return int(resume() or 0) if callable(resume) else 0


async def _pause_meditations(adapter: Any) -> int:
    tasks = [
        task
        for task in list(getattr(adapter, "_habit_meditation_tasks", set()) or set())
        if not task.done()
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return len(tasks)


async def _set_control(
    runtime: Any,
    adapter: Any,
    value: str,
    *,
    update: Any,
    callback: bool,
) -> tuple[str, InlineKeyboardMarkup]:
    before = _control_status(runtime, adapter)
    if before.environment_locked:
        _append_audit(
            runtime,
            "habit_control_blocked",
            requested=value,
            reason="environment_override",
            environment={
                "name": _her_habits.HABIT_MEDITATION_ENV,
                "value": before.environment_value,
            },
            context=_audit_context(
                runtime,
                update,
                source="callback" if callback else "command",
            ),
        )
        return _home_view(
            runtime,
            adapter,
            notice="⚠️ The environment override locks this setting; no persistent value changed.",
        )
    override = {"on": True, "off": False, "default": None}[value]
    runtime.backend_manager.set_habit_meditation_override(override)
    after = _control_status(runtime, adapter)
    cancelled = 0
    resumed = 0
    if after.effective:
        resume = getattr(adapter, "_resume_pending_habit_meditations", None)
        if callable(resume):
            resumed = int(resume() or 0)
    else:
        cancelled = await _pause_meditations(adapter)
    context = _audit_context(
        runtime, update, source="callback" if callback else "command"
    )
    _append_audit(
        runtime,
        "habit_control_changed",
        requested=value,
        before={
            "effective": before.effective,
            "source": before.source,
            "override": before.override,
        },
        after={
            "effective": after.effective,
            "source": after.source,
            "override": after.override,
        },
        cancelled_meditations=cancelled,
        resumed_meditations=resumed,
        context=context,
    )
    notice = (
        f"✅ Habit–Meditation is now <b>{status_label(after.effective)}</b> "
        f"via {html.escape(after.source)}."
    )
    return _home_view(runtime, adapter, notice=notice)


async def _reply_view(
    runtime: Any, update: Any, view: tuple[str, InlineKeyboardMarkup]
) -> None:
    text, markup = view
    await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=markup)


async def cmd_habit(runtime: Any, update: Any, context: Any) -> None:
    user = getattr(update, "effective_user", None)
    if callable(
        getattr(runtime, "_is_authorized_user", None)
    ) and not runtime._is_authorized_user(getattr(user, "id", None)):
        return
    args = [str(arg) for arg in (getattr(context, "args", None) or [])]
    _append_audit(
        runtime,
        "habit_command_received",
        backend=_active_engine(runtime),
        args=args,
        context=_audit_context(runtime, update, source="command"),
    )
    adapter = _her_adapter(runtime)
    if adapter is None:
        _append_audit(
            runtime,
            "habit_command_blocked",
            reason="backend_not_her",
            backend=_active_engine(runtime),
            args=args,
            context=_audit_context(runtime, update, source="command"),
        )
        await _reply_view(runtime, update, _unavailable_view(runtime))
        return

    lowered = [arg.casefold() for arg in args]
    store = _habit_store(runtime, adapter)
    if not args:
        active_habits = store.load()
        view = _home_view(runtime, adapter)
        _append_audit(
            runtime,
            "habit_menu_viewed",
            active_count=len(active_habits),
            archived_count=store.archived_count(),
            active_habits=[habit.to_payload() for habit in active_habits],
            context=_audit_context(runtime, update, source="command"),
        )
        await _reply_view(runtime, update, view)
        return
    if len(args) == 1 and lowered[0] in {"on", "off", "default"}:
        await _reply_view(
            runtime,
            update,
            await _set_control(
                runtime, adapter, lowered[0], update=update, callback=False
            ),
        )
        return
    if len(args) == 2 and lowered[0] == "view":
        habit = store.get(args[1])
        if habit is None:
            await _reply_view(
                runtime,
                update,
                _home_view(
                    runtime, adapter, notice="❌ Habit not found; no state changed."
                ),
            )
            return
        _append_audit(
            runtime,
            "habit_detail_viewed",
            habit=habit.to_payload(),
            context=_audit_context(runtime, update, source="command"),
        )
        await _reply_view(runtime, update, _detail_view(habit, offset=0))
        return
    if lowered[:1] == ["delete"]:
        if len(args) == 2 and lowered[1] == "all":
            habits = _sorted_habits(store)
            if not habits:
                await _reply_view(
                    runtime,
                    update,
                    _home_view(
                        runtime,
                        adapter,
                        notice="ℹ️ There are no active Habits to delete.",
                    ),
                )
                return
            await _reply_view(
                runtime, update, _delete_all_confirm_view(runtime, habits)
            )
            return
        if len(args) == 2:
            habit = store.get(args[1])
            if habit is None:
                await _reply_view(
                    runtime,
                    update,
                    _home_view(
                        runtime, adapter, notice="❌ Habit not found; no state changed."
                    ),
                )
                return
            await _reply_view(runtime, update, _delete_confirm_view(habit, offset=0))
            return
    if len(args) == 1 and lowered[0] == "reset":
        await _reply_view(
            runtime,
            update,
            _reset_confirm_view(runtime, store, _habit_journal(runtime, adapter)),
        )
        return
    await _reply_view(
        runtime,
        update,
        _home_view(
            runtime,
            adapter,
            notice="⚠️ Unsupported syntax; choose an action below or use the shown commands.",
        ),
    )


async def callback_habit(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    command_allowed = getattr(runtime, "_is_command_allowed", None)
    if callable(command_allowed) and not command_allowed("habit"):
        await query.answer("/habit is disabled for this agent.", show_alert=True)
        return
    data = str(query.data or "")
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "home"
    _append_audit(
        runtime,
        "habit_callback_received",
        backend=_active_engine(runtime),
        callback_data=data,
        context=_audit_context(runtime, update, source="callback"),
    )
    if action == "keep":
        await query.answer("Current backend kept.")
        return
    adapter = _her_adapter(runtime)
    if adapter is None:
        text, markup = _unavailable_view(runtime)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer("HER backend is required.")
        return
    store = _habit_store(runtime, adapter)
    habits = _sorted_habits(store)

    if action == "locked":
        await query.answer(
            "The environment override locks this setting.", show_alert=True
        )
        return
    if action == "home":
        offset = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        text, markup = _home_view(runtime, adapter, offset=offset)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer()
        return
    if action == "set" and len(parts) == 3 and parts[2] in {"on", "off", "default"}:
        text, markup = await _set_control(
            runtime,
            adapter,
            parts[2],
            update=update,
            callback=True,
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer()
        return
    if action in {"view", "full", "delete", "confirm_delete"}:
        token = parts[2] if len(parts) > 2 else ""
        offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        habit = (
            _resolve_version_token(habits, token)
            if action == "confirm_delete"
            else _resolve_token(habits, token)
        )
        if habit is None:
            text, markup = _home_view(
                runtime,
                adapter,
                offset=offset,
                notice="⚠️ That Habit changed or no longer exists; review the refreshed list.",
            )
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            await query.answer(
                "Habit changed; confirm deletion again."
                if action == "confirm_delete"
                else "Habit not found.",
                show_alert=True,
            )
            return
        context_fields = _audit_context(runtime, update, source="callback")
        if action == "view":
            _append_audit(
                runtime,
                "habit_detail_viewed",
                habit=habit.to_payload(),
                context=context_fields,
            )
            text, markup = _detail_view(habit, offset=offset)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            await query.answer()
            return
        if action == "full":
            await runtime.send_long_message(
                query.message.chat_id,
                _full_detail_text(habit),
                purpose="habit_detail",
            )
            _append_audit(
                runtime,
                "habit_full_detail_sent",
                habit=habit.to_payload(),
                context=context_fields,
            )
            await query.answer("Full Habit content sent.")
            return
        if action == "delete":
            text, markup = _delete_confirm_view(habit, offset=offset)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            await query.answer()
            return
        if _mutation_busy(runtime, adapter):
            await query.answer(
                "HER is busy; try again when the current work finishes.",
                show_alert=True,
            )
            return
        lock = adapter._habit_execution_lock
        async with lock:
            outcomes, changes = store.apply_actions_with_changes(
                [{"operation": "delete", "habit_id": habit.habit_id}],
                max_actions=1,
                audit_context=context_fields,
            )
        _append_audit(
            runtime,
            "habit_command_delete_completed",
            target=habit.to_payload(),
            outcomes=outcomes,
            changes=[change.to_payload() for change in changes],
            context=context_fields,
        )
        if not changes:
            text, markup = _home_view(
                runtime,
                adapter,
                offset=offset,
                notice="⚠️ The Habit changed before deletion; nothing was archived.",
            )
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            await query.answer("Habit was not archived.", show_alert=True)
            return
        text, markup = _home_view(
            runtime,
            adapter,
            offset=offset,
            notice=f"✅ Archived <code>{html.escape(habit.habit_id)}</code>.",
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer("Habit archived.")
        return

    if action == "delete_all":
        if not habits:
            await query.answer("There are no active Habits.", show_alert=True)
            return
        text, markup = _delete_all_confirm_view(runtime, habits)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer()
        return
    if action == "confirm_all":
        expected = parts[2] if len(parts) > 2 else ""
        if expected != _active_fingerprint(habits):
            await query.answer(
                "Habit state changed; review the refreshed list first.", show_alert=True
            )
            text, markup = _home_view(runtime, adapter)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            return
        if _mutation_busy(runtime, adapter):
            await query.answer(
                "HER is busy; try again when the current work finishes.",
                show_alert=True,
            )
            return
        context_fields = _audit_context(runtime, update, source="callback")
        lock = adapter._habit_execution_lock
        async with lock:
            outcomes, changes = store.archive_all(audit_context=context_fields)
        _append_audit(
            runtime,
            "habit_command_delete_all_completed",
            targets=[habit.to_payload() for habit in habits],
            outcomes=outcomes,
            changes=[change.to_payload() for change in changes],
            context=context_fields,
        )
        text, markup = _home_view(
            runtime,
            adapter,
            notice=(
                f"✅ Archived all <code>{len(changes)}</code> active Habits."
                if len(changes) == len(habits)
                else (
                    f"⚠️ Archived <code>{len(changes)}</code> of <code>{len(habits)}</code> "
                    "Habits; review the audit log for skipped targets."
                )
            ),
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer(
            "All active Habits archived."
            if len(changes) == len(habits)
            else "Some Habits were not archived.",
            show_alert=len(changes) != len(habits),
        )
        return
    if action == "reset":
        text, markup = _reset_confirm_view(
            runtime, store, _habit_journal(runtime, adapter)
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer()
        return
    if action == "confirm_reset":
        expected = parts[2] if len(parts) > 2 else ""
        if expected != _her_habits.habit_state_fingerprint(runtime.workspace_dir):
            await query.answer(
                "Habit state changed; review the refreshed state first.",
                show_alert=True,
            )
            text, markup = _home_view(runtime, adapter)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            return
        if _mutation_busy(runtime, adapter):
            await query.answer("HER is busy; reset was not started.", show_alert=True)
            return
        context_fields = _audit_context(runtime, update, source="callback")
        lock = adapter._habit_execution_lock
        try:
            async with lock:
                result = _her_habits.reset_habit_state(
                    runtime.workspace_dir,
                    audit_context=context_fields,
                )
        except Exception as exc:  # noqa: BLE001 - reset helper performs rollback
            _append_audit(
                runtime,
                "habit_command_reset_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                context=context_fields,
            )
            await query.answer(
                "Habit reset failed and was rolled back; review the audit log.",
                show_alert=True,
            )
            return
        text, markup = _home_view(
            runtime,
            adapter,
            notice=(
                "✅ Habit state reset into recoverable snapshot "
                f"<code>{html.escape(result.snapshot_id)}</code>."
            ),
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer("HER Habit state reset.")
        return

    await query.answer("Unsupported Habit action.", show_alert=True)
