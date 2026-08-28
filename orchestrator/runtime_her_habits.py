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
from orchestrator import ui_language
from orchestrator.command_ui import (
    back_label,
    card_title,
    confirm_card,
    refresh_label,
    selected_label,
    setting_card,
    status_label,
)
from orchestrator.flexible_backend_registry import canonical_backend_engine

HABIT_PAGE_SIZE = 5
HER_HABIT_ENGINES = frozenset({"her-v2"})


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
    return canonical_backend_engine(str(engine or "")).casefold()


def _her_adapter(runtime: Any) -> Any | None:
    if _active_engine(runtime) not in HER_HABIT_ENGINES:
        return None
    backend = getattr(
        getattr(runtime, "backend_manager", None), "current_backend", None
    )
    if (
        canonical_backend_engine(
            str(getattr(getattr(backend, "config", None), "engine", ""))
        ).casefold()
        not in HER_HABIT_ENGINES
    ):
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


def _resolve_reference(
    habits: list[_her_habits.HERHabit],
    reference: str,
) -> _her_habits.HERHabit | None:
    return _her_habits.resolve_habit_reference(habits, reference)


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
    adapter = _her_adapter(runtime)
    v2_audit = getattr(adapter, "_record_learning_audit", None)
    if callable(v2_audit):
        try:
            v2_audit(event, stage="habit_command", payload=fields)
        except Exception as exc:  # noqa: BLE001 - legacy audit remains available
            runtime.logger.warning(
                "HER v2 Habit command audit failed: event=%s error=%s",
                event,
                type(exc).__name__,
            )


def _unavailable_view(runtime: Any) -> tuple[str, InlineKeyboardMarkup]:
    backend = _active_engine(runtime) or "unknown"
    return (
        setting_card(
            "🧠",
            "HER habits",
            current=f"<b>{html.escape(ui_language.tr('common.unavailable'))}</b>",
            facts=[
                f"<b>{html.escape(ui_language.tr('common.backend'))}</b> · <code>{html.escape(backend)}</code>",
                f"<b>{html.escape(ui_language.tr('common.scope'))}</b> · "
                f"{ui_language.tr('habit.unavailable.scope')}",
            ],
            consequence=ui_language.tr("habit.unavailable.effect"),
            action=ui_language.tr("habit.unavailable.action"),
        ),
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        ui_language.tr("habit.button.switch_her"), callback_data="backend:her:plain"
                    )
                ],
                [
                    InlineKeyboardButton(
                        ui_language.tr("habit.button.keep_backend"), callback_data="habit:keep"
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
    short_references = _her_habits.habit_short_references(habits)
    status = _control_status(runtime, adapter)
    pending = len(journal.pending_jobs(limit=10_000))
    pending_notifications = len(journal.pending_notifications(limit=10_000))
    journal_total = (
        len(list(journal.root.glob("*.json"))) if journal.root.is_dir() else 0
    )
    lines = [
        card_title("🧠", "HER habits"),
        "",
        f"<b>{html.escape(ui_language.tr('common.current'))}</b> · <b>{status_label(status.effective)}</b>",
        f"<b>{html.escape(ui_language.tr('common.backend'))}</b> · "
        f"<code>{html.escape(_active_engine(runtime) or ui_language.tr('common.unknown'))}</code>",
        f"<b>{html.escape(ui_language.tr('common.source'))}</b> · <code>{html.escape(status.source)}</code>",
        f"<b>{html.escape(ui_language.tr('habit.active'))}</b> · <code>{len(habits)}</code>",
        f"<b>{html.escape(ui_language.tr('habit.archived'))}</b> · <code>{store.archived_count()}</code>",
        f"<b>{html.escape(ui_language.tr('habit.meditation_jobs'))}</b> · "
        + ui_language.tr(
            "habit.total_pending",
            total=f"<code>{journal_total}</code>",
            pending=f"<code>{pending}</code>",
        ),
        f"<b>{html.escape(ui_language.tr('habit.notices'))}</b> · "
        + ui_language.tr("habit.pending_delivery", count=f"<code>{pending_notifications}</code>"),
        f"<b>{html.escape(ui_language.tr('common.changes'))}</b> · "
        f"{html.escape(ui_language.tr('habit.changes'))}",
    ]
    if status.environment_locked:
        lines.extend(
            [
                "",
                ui_language.tr(
                    "habit.environment_locked",
                    variable=html.escape(_her_habits.HABIT_MEDITATION_ENV),
                    value=html.escape(str(status.environment_value or "")),
                ),
            ]
        )
    if notice:
        lines.extend(["", notice])
    lines.extend(["", f"<b>{html.escape(ui_language.tr('habit.section').upper())}</b>"])
    if not page:
        lines.append(ui_language.tr("habit.none"))
    else:
        for index, habit in enumerate(page, start=offset + 1):
            lock_marker = "🔒 " if habit.protected else ""
            lines.append(
                f"<code>{index}</code> · {lock_marker}<b>{html.escape(_short(habit.title, 58))}</b>"
            )
            lines.append(
                f"   <code>{html.escape(short_references[habit.habit_id])}</code> · "
                f"<code>{html.escape(habit.habit_id)}</code> · {ui_language.tr('habit.updated')} "
                f"<code>{html.escape(habit.updated_at)}</code>"
            )
    lines.extend(
        [
            "",
            f"<b>{html.escape(ui_language.tr('common.use'))}</b>",
            f"<code>/habit view &lt;number|short-ref|habit-id&gt;</code> · {ui_language.tr('habit.use.view')}",
            f"<code>/habit on|off|default</code> · {ui_language.tr('habit.use.control')}",
            f"<code>/habit protect|unprotect &lt;reference&gt;</code> · {ui_language.tr('habit.use.protection')}",
            f"<code>/habit delete &lt;reference&gt;</code> · {ui_language.tr('habit.use.delete')}",
            f"<code>/habit delete all</code> · {ui_language.tr('habit.use.delete_all')}",
            f"<code>/habit reset</code> · {ui_language.tr('habit.use.reset')}",
        ]
    )

    rows: list[list[InlineKeyboardButton]] = []
    if status.environment_locked:
        rows.append(
            [
                InlineKeyboardButton(ui_language.tr("habit.button.locked_on"), callback_data="habit:locked"),
                InlineKeyboardButton(ui_language.tr("habit.button.locked_off"), callback_data="habit:locked"),
                InlineKeyboardButton(ui_language.tr("habit.button.locked_default"), callback_data="habit:locked"),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    selected_label(ui_language.tr("habit.button.on"), status.override is True),
                    callback_data="habit:set:on",
                ),
                InlineKeyboardButton(
                    selected_label(ui_language.tr("habit.button.off"), status.override is False),
                    callback_data="habit:set:off",
                ),
                InlineKeyboardButton(
                    selected_label(ui_language.tr("habit.button.default"), status.override is None),
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
                ui_language.tr("habit.button.previous"),
                callback_data=f"habit:home:{max(0, offset - HABIT_PAGE_SIZE)}",
            )
        )
    nav.append(
        InlineKeyboardButton(refresh_label(), callback_data=f"habit:home:{offset}")
    )
    if offset + HABIT_PAGE_SIZE < len(habits):
        nav.append(
            InlineKeyboardButton(
                ui_language.tr("habit.button.next"),
                callback_data=f"habit:home:{offset + HABIT_PAGE_SIZE}",
            )
        )
    rows.append(nav)
    if habits:
        rows.append(
            [InlineKeyboardButton(ui_language.tr("habit.button.delete_all"), callback_data="habit:delete_all")]
        )
    rows.append(
        [InlineKeyboardButton(ui_language.tr("habit.button.reset"), callback_data="habit:reset")]
    )
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _detail_view(
    habit: _her_habits.HERHabit,
    *,
    offset: int,
    habits: list[_her_habits.HERHabit] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    catalogue = habits or [habit]
    short_reference = _her_habits.habit_short_references(catalogue)[habit.habit_id]
    title = _html_short(habit.title, 500)
    metadata = _html_short(habit.metadata, 600)
    body = _html_short(habit.body, 1_500)
    text = "\n".join(
        [
            card_title("🧠", "HER Habit detail"),
            "",
            f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
            f"<b>{html.escape(ui_language.tr('habit.state.active'))}</b> · "
            f"<b>{html.escape(ui_language.tr('habit.state.protected' if habit.protected else 'habit.state.unprotected'))}</b>",
            f"<b>{html.escape(ui_language.tr('common.scope'))}</b> · {ui_language.tr('habit.scope')}",
            f"<b>{html.escape(ui_language.tr('habit.reference'))}</b> · <code>{html.escape(short_reference)}</code>",
            f"<b>{html.escape(ui_language.tr('common.id'))}</b> · <code>{html.escape(habit.habit_id)}</code>",
            f"<b>{html.escape(ui_language.tr('habit.created'))}</b> · <code>{html.escape(habit.created_at)}</code>",
            f"<b>{html.escape(ui_language.tr('habit.updated'))}</b> · <code>{html.escape(habit.updated_at)}</code>",
            "",
            f"<b>{title}</b>",
            "",
            f"<b>{html.escape(ui_language.tr('habit.when_relevant').upper())}</b>",
            metadata,
            "",
            f"<b>{html.escape(ui_language.tr('habit.behaviour').upper())}</b>",
            body,
            "",
            ui_language.tr("habit.detail_effect"),
        ]
    )
    token = _habit_token(habit.habit_id)
    return (
        text,
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        ui_language.tr("habit.button.full"),
                        callback_data=f"habit:full:{token}:{offset}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        ui_language.tr("habit.button.delete"), callback_data=f"habit:delete:{token}:{offset}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        ui_language.tr(
                            "habit.button.unprotect" if habit.protected else "habit.button.protect"
                        ),
                        callback_data=(
                            f"habit:{'unprotect' if habit.protected else 'protect'}:{token}:{offset}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        back_label(), callback_data=f"habit:home:{offset}"
                    ),
                    InlineKeyboardButton(
                        refresh_label(), callback_data=f"habit:view:{token}:{offset}"
                    ),
                ],
            ]
        ),
    )


def _full_detail_text(habit: _her_habits.HERHabit) -> str:
    return "\n".join(
        [
            f"HER Habit: {habit.title}",
            f"{ui_language.tr('common.id')}: {habit.habit_id}",
            f"{ui_language.tr('habit.created')}: {habit.created_at}",
            f"{ui_language.tr('habit.updated')}: {habit.updated_at}",
            f"{ui_language.tr('habit.full.protected')}: "
            f"{ui_language.tr('habit.yes' if habit.protected else 'habit.no')}",
            "",
            ui_language.tr("habit.when_relevant").upper(),
            habit.metadata,
            "",
            ui_language.tr("habit.behaviour").upper(),
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
                ui_language.tr("habit.delete_effect")
            ),
        ),
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        ui_language.tr("habit.button.delete"),
                        callback_data=f"habit:confirm_delete:{version_token}:{offset}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        ui_language.tr("habit.button.keep"), callback_data=f"habit:view:{token}:{offset}"
                    )
                ],
            ]
        ),
    )


def _protection_confirm_view(
    habit: _her_habits.HERHabit,
    *,
    protected: bool,
    offset: int,
) -> tuple[str, InlineKeyboardMarkup]:
    token = _habit_token(habit.habit_id)
    version_token = _habit_version_token(habit)
    action = ui_language.tr(
        "habit.button.protect" if protected else "habit.button.unprotect"
    )
    consequence = ui_language.tr(
        "habit.protect_effect" if protected else "habit.unprotect_effect"
    )
    return (
        confirm_card(
            "🔒" if protected else "🔓",
            ui_language.tr(
                "habit.protect_title" if protected else "habit.unprotect_title"
            ),
            target=(
                f"<code>{html.escape(habit.habit_id)}</code> · "
                f"{html.escape(habit.title)}"
            ),
            consequence=consequence,
        ),
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        action,
                        callback_data=(
                            f"habit:confirm_{'protect' if protected else 'unprotect'}:"
                            f"{version_token}:{offset}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        ui_language.tr("habit.button.keep_protection"),
                        callback_data=f"habit:view:{token}:{offset}",
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
        target=ui_language.tr(
            "habit.delete_all.target",
            agent=f"<code>{html.escape(str(runtime.name))}</code>",
            count=f"<code>{len(habits)}</code>",
        ),
        consequence=ui_language.tr("habit.delete_all.effect"),
    )
    return (
        text,
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        ui_language.tr("habit.button.delete_all"), callback_data=f"habit:confirm_all:{fingerprint}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        ui_language.tr("habit.button.keep_all"), callback_data="habit:home:0"
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
        target=ui_language.tr(
            "habit.reset.target",
            agent=f"<code>{html.escape(str(runtime.name))}</code>",
        ),
        consequence=ui_language.tr("habit.reset.effect"),
    )
    text += (
        f"\n\n<b>{html.escape(ui_language.tr('habit.active'))}</b> · <code>{active}</code>"
        f"\n<b>{html.escape(ui_language.tr('habit.archived'))}</b> · <code>{archived}</code>"
        f"\n<b>{html.escape(ui_language.tr('habit.meditation_jobs'))}</b> · <code>{jobs}</code>"
    )
    return (
        text,
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        ui_language.tr("habit.button.reset"),
                        callback_data=f"habit:confirm_reset:{fingerprint}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        ui_language.tr("habit.button.keep_state"), callback_data="habit:home:0"
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
        "created": ("🌱", ui_language.tr("habit.update.formed")),
        "updated": ("✏️", ui_language.tr("habit.update.changed")),
        "deleted": ("📦", ui_language.tr("habit.update.archived")),
    }
    lines = [
        card_title("🧠", "HER Habit update"),
        "",
        f"<b>{html.escape(ui_language.tr('common.current'))}</b> · <b>"
        f"{ui_language.tr('habit.update.count.one' if len(changes) == 1 else 'habit.update.count.many', count=len(changes))}</b>",
        f"<b>{html.escape(ui_language.tr('habit.update.meditation_job'))}</b> · "
        f"<code>{html.escape(str(job.get('job_id') or ui_language.tr('common.unknown')))}</code>",
    ]
    summary = _html_short(str(notification.get("request_summary") or ""), 500)
    if summary:
        lines.append(f"<b>{html.escape(ui_language.tr('habit.update.task'))}</b> · {summary}")
    lines.extend(["", f"<b>{html.escape(ui_language.tr('habit.update.changes').upper())}</b>"])
    for change in shown_changes:
        operation = str(change.get("operation") or "changed").casefold()
        icon, label = operation_labels.get(
            operation,
            ("•", ui_language.tr("habit.update.changed")),
        )
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
            f"• <b>{html.escape(ui_language.tr('habit.update.plus'))}</b> · "
            + ui_language.tr(
                "habit.update.more",
                count=f"<code>{len(changes) - len(shown_changes)}</code>",
            )
        )
    lines.extend(
        [
            "",
            ui_language.tr("habit.update.verbose_notice"),
            ui_language.tr("habit.update.open_notice"),
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
                        ui_language.tr("habit.button.view"),
                        callback_data=f"habit:view:{_habit_token(habit_id)}:0",
                    )
                ]
            )
    rows.append([
        InlineKeyboardButton(
            ui_language.tr("habit.button.open"),
            callback_data="habit:home:0",
        )
    ])
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
            notice=ui_language.tr("habit.notice.environment_locked"),
        )
    override = {"on": True, "off": False, "default": None}[value]
    runtime.backend_manager.set_habit_meditation_override(override)
    after = _control_status(runtime, adapter)
    cancelled = 0
    resumed = 0
    if after.effective:
        from orchestrator.fresh_context import resume_habit_context

        resume_habit_context(runtime)
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
    notice = ui_language.tr(
        "habit.notice.control_changed",
        status=status_label(after.effective),
        source=html.escape(after.source),
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
        habits = _sorted_habits(store)
        habit = _resolve_reference(habits, args[1])
        if habit is None:
            await _reply_view(
                runtime,
                update,
                _home_view(
                    runtime,
                    adapter,
                    notice=ui_language.tr("habit.notice.not_found"),
                ),
            )
            return
        _append_audit(
            runtime,
            "habit_detail_viewed",
            habit=habit.to_payload(),
            context=_audit_context(runtime, update, source="command"),
        )
        await _reply_view(
            runtime,
            update,
            _detail_view(habit, offset=0, habits=habits),
        )
        return
    if len(args) == 2 and lowered[0] in {"protect", "unprotect"}:
        habits = _sorted_habits(store)
        habit = _resolve_reference(habits, args[1])
        if habit is None:
            await _reply_view(
                runtime,
                update,
                _home_view(
                    runtime,
                    adapter,
                    notice=ui_language.tr("habit.notice.reference_not_found"),
                ),
            )
            return
        desired = lowered[0] == "protect"
        if habit.protected is desired:
            await _reply_view(
                runtime,
                update,
                _home_view(
                    runtime,
                    adapter,
                    notice=(
                        ui_language.tr("habit.notice.already_protected")
                        if desired
                        else ui_language.tr("habit.notice.already_unprotected")
                    ),
                ),
            )
            return
        await _reply_view(
            runtime,
            update,
            _protection_confirm_view(habit, protected=desired, offset=0),
        )
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
                        notice=ui_language.tr("habit.notice.none_to_delete"),
                    ),
                )
                return
            await _reply_view(
                runtime, update, _delete_all_confirm_view(runtime, habits)
            )
            return
        if len(args) == 2:
            habit = _resolve_reference(_sorted_habits(store), args[1])
            if habit is None:
                await _reply_view(
                    runtime,
                    update,
                    _home_view(
                        runtime,
                        adapter,
                        notice=ui_language.tr("habit.notice.not_found"),
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
            notice=ui_language.tr("habit.notice.unsupported_syntax"),
        ),
    )


async def callback_habit(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    command_allowed = getattr(runtime, "_is_command_allowed", None)
    if callable(command_allowed) and not command_allowed("habit"):
        await query.answer(ui_language.tr("habit.command_disabled"), show_alert=True)
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
        await query.answer(ui_language.tr("habit.backend_kept"))
        return
    adapter = _her_adapter(runtime)
    if adapter is None:
        text, markup = _unavailable_view(runtime)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer(ui_language.tr("habit.backend_required"))
        return
    store = _habit_store(runtime, adapter)
    habits = _sorted_habits(store)

    if action == "locked":
        await query.answer(
            ui_language.tr("habit.environment_override_locked"), show_alert=True
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
    habit_actions = {
        "view",
        "full",
        "delete",
        "protect",
        "unprotect",
        "confirm_delete",
        "confirm_protect",
        "confirm_unprotect",
    }
    if action in habit_actions:
        token = parts[2] if len(parts) > 2 else ""
        offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        habit = (
            _resolve_version_token(habits, token)
            if action.startswith("confirm_")
            else _resolve_token(habits, token)
        )
        if habit is None:
            text, markup = _home_view(
                runtime,
                adapter,
                offset=offset,
                notice=ui_language.tr("habit.notice.stale"),
            )
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            await query.answer(
                (
                    ui_language.tr("habit.confirm_delete_again")
                    if action == "confirm_delete"
                    else ui_language.tr("habit.confirm_protection_again")
                )
                if action.startswith("confirm_")
                else ui_language.tr("habit.not_found"),
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
            text, markup = _detail_view(habit, offset=offset, habits=habits)
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
            await query.answer(ui_language.tr("habit.full_sent"))
            return
        if action == "delete":
            text, markup = _delete_confirm_view(habit, offset=offset)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            await query.answer()
            return
        if action in {"protect", "unprotect"}:
            desired = action == "protect"
            if habit.protected is desired:
                await query.answer(
                    ui_language.tr("habit.already_protected")
                    if desired
                    else ui_language.tr("habit.already_unprotected"),
                    show_alert=True,
                )
                return
            text, markup = _protection_confirm_view(
                habit,
                protected=desired,
                offset=offset,
            )
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            await query.answer()
            return
        if _mutation_busy(runtime, adapter):
            await query.answer(
                ui_language.tr("habit.busy"),
                show_alert=True,
            )
            return
        lock = adapter._habit_execution_lock
        if action in {"confirm_protect", "confirm_unprotect"}:
            desired = action == "confirm_protect"
            async with lock:
                change = store.set_protected(
                    habit.habit_id,
                    desired,
                    audit_context=context_fields,
                )
            _append_audit(
                runtime,
                "habit_command_protection_completed",
                target=habit.to_payload(),
                requested="protect" if desired else "unprotect",
                change=change.to_payload() if change is not None else None,
                context=context_fields,
            )
            if change is None:
                await query.answer(
                    ui_language.tr("habit.protection_unchanged"),
                    show_alert=True,
                )
                return
            text, markup = _home_view(
                runtime,
                adapter,
                offset=offset,
                notice=(
                    ui_language.tr(
                        "habit.notice.protected",
                        habit_id=html.escape(habit.habit_id),
                    )
                    if desired
                    else ui_language.tr(
                        "habit.notice.unprotected",
                        habit_id=html.escape(habit.habit_id),
                    )
                ),
            )
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            await query.answer(
                ui_language.tr("habit.protected" if desired else "habit.unprotected")
            )
            return
        async with lock:
            outcomes, changes = store.apply_actions_with_changes(
                [{"operation": "delete", "habit_id": habit.habit_id}],
                max_actions=1,
                audit_context=context_fields,
                allow_protected=True,
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
                notice=ui_language.tr("habit.notice.changed_before_delete"),
            )
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            await query.answer(ui_language.tr("habit.not_archived"), show_alert=True)
            return
        text, markup = _home_view(
            runtime,
            adapter,
            offset=offset,
            notice=ui_language.tr(
                "habit.notice.archived",
                habit_id=html.escape(habit.habit_id),
            ),
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer(ui_language.tr("habit.archived_done"))
        return

    if action == "delete_all":
        if not habits:
            await query.answer(ui_language.tr("habit.none_active"), show_alert=True)
            return
        text, markup = _delete_all_confirm_view(runtime, habits)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer()
        return
    if action == "confirm_all":
        expected = parts[2] if len(parts) > 2 else ""
        if expected != _active_fingerprint(habits):
            await query.answer(
                ui_language.tr("habit.state_changed_list"), show_alert=True
            )
            text, markup = _home_view(runtime, adapter)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            return
        if _mutation_busy(runtime, adapter):
            await query.answer(
                ui_language.tr("habit.busy"),
                show_alert=True,
            )
            return
        context_fields = _audit_context(runtime, update, source="callback")
        lock = adapter._habit_execution_lock
        async with lock:
            outcomes, changes = store.archive_all(
                audit_context=context_fields,
                allow_protected=True,
            )
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
                ui_language.tr("habit.notice.archived_all", count=len(changes))
                if len(changes) == len(habits)
                else ui_language.tr(
                    "habit.notice.archived_some",
                    changed=len(changes),
                    total=len(habits),
                )
            ),
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer(
            ui_language.tr("habit.all_archived")
            if len(changes) == len(habits)
            else ui_language.tr("habit.some_not_archived"),
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
                ui_language.tr("habit.state_changed"),
                show_alert=True,
            )
            text, markup = _home_view(runtime, adapter)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            return
        if _mutation_busy(runtime, adapter):
            await query.answer(ui_language.tr("habit.reset_busy"), show_alert=True)
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
                ui_language.tr("habit.reset_failed"),
                show_alert=True,
            )
            return
        text, markup = _home_view(
            runtime,
            adapter,
            notice=ui_language.tr(
                "habit.notice.reset",
                snapshot_id=html.escape(result.snapshot_id),
            ),
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer(ui_language.tr("habit.reset_done"))
        return

    await query.answer(ui_language.tr("habit.unsupported_action"), show_alert=True)
