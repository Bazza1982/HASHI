from __future__ import annotations

import html
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator.habits import HabitStore
from orchestrator.runtime_common import QueuedRequest

HABIT_BROWSER_PAGE_SIZE = 5


def build_habit_sections(runtime: Any, item: QueuedRequest, prompt: str) -> tuple[list[tuple[str, str]], list[str]]:
    habits = runtime.habit_store.retrieve(prompt, source=item.source, summary=item.summary)
    if not habits:
        item.active_habits = []
        return [], []
    runtime.habit_store.mark_triggered(habits)
    item.active_habits = runtime.habit_store.serialize_habits(habits)
    habit_ids = [habit.habit_id for habit in habits]
    section = runtime.habit_store.render_prompt_section(habits)
    runtime.logger.info(
        f"Habit retrieval for {item.request_id}: {len(habit_ids)} matched ({', '.join(habit_ids)})"
    )
    runtime._log_maintenance(item, "habit_retrieval", habit_ids=",".join(habit_ids), habit_count=len(habit_ids))
    return ([section] if section else []), habit_ids


def record_habit_outcome(
    runtime: Any,
    item: QueuedRequest,
    *,
    success: bool,
    response_text: str | None = None,
    error_text: str | None = None,
) -> None:
    active_habits = item.active_habits or []
    if not active_habits:
        return
    try:
        runtime.habit_store.record_execution_outcome(
            request_id=item.request_id,
            prompt=item.prompt,
            source=item.source,
            summary=item.summary,
            active_habits=active_habits,
            response_text=response_text,
            error_text=error_text,
            success=success,
        )
    except Exception as exc:
        runtime.error_logger.warning(f"Failed to record habit outcome for {item.request_id}: {exc}")


def capture_followup_habit_feedback(runtime: Any, text: str) -> None:
    last_response = runtime.last_response or {}
    request_id = last_response.get("request_id")
    responded_at = last_response.get("responded_at")
    if not request_id:
        return
    try:
        result = runtime.habit_store.apply_user_feedback(
            request_id=request_id,
            feedback_text=text,
            responded_at=responded_at,
        )
    except Exception as exc:
        runtime.error_logger.warning(f"Failed to capture habit feedback for {request_id}: {exc}")
        return
    if not result:
        return
    runtime.maintenance_logger.info(
        f"Habit follow-up feedback for {request_id}: "
        f"sentiment={result.sentiment} updated_events={result.updated_events} "
        f"habits={','.join(result.updated_habits)}"
    )


def habit_db_path(runtime: Any) -> Path:
    return runtime.workspace_dir / "habits.sqlite"


def load_local_habit_counts(runtime: Any) -> dict[str, int]:
    db_path = runtime._habit_db_path()
    counts = {"total": 0, "active": 0, "candidate": 0, "paused": 0, "disabled": 0}
    if not db_path.exists():
        return counts
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0) AS active,
                COALESCE(SUM(CASE WHEN status = 'candidate' THEN 1 ELSE 0 END), 0) AS candidate,
                COALESCE(SUM(CASE WHEN status = 'paused' THEN 1 ELSE 0 END), 0) AS paused,
                COALESCE(SUM(CASE WHEN status = 'disabled' THEN 1 ELSE 0 END), 0) AS disabled
            FROM habits
            WHERE agent_id = ?
            """,
            (runtime.name,),
        ).fetchone()
    if row:
        counts = {key: int(row[key] or 0) for key in counts}
    return counts


def load_local_habit_rows(
    runtime: Any,
    *,
    offset: int = 0,
    limit: int = HABIT_BROWSER_PAGE_SIZE,
) -> tuple[int, list[sqlite3.Row]]:
    db_path = runtime._habit_db_path()
    if not db_path.exists():
        return 0, []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        total = int(
            conn.execute("SELECT COUNT(*) FROM habits WHERE agent_id = ?", (runtime.name,)).fetchone()[0] or 0
        )
        rows = conn.execute(
            """
            SELECT habit_id, status, enabled, habit_type, title, instruction, task_type, confidence
            FROM habits
            WHERE agent_id = ?
            ORDER BY
                CASE status WHEN 'active' THEN 0 WHEN 'candidate' THEN 1 WHEN 'paused' THEN 2 ELSE 3 END,
                confidence DESC,
                updated_at DESC
            LIMIT ? OFFSET ?
            """,
            (runtime.name, limit, max(offset, 0)),
        ).fetchall()
    return total, rows


def habit_status_button_label(current: str, target: str) -> str:
    return {
        "active": "✅ Active" if current == "active" else "Active",
        "paused": "⏸ Pause" if current == "paused" else "Pause",
        "disabled": "❌ Disable" if current == "disabled" else "Disable",
    }[target]


def build_habit_browser_view(
    runtime: Any,
    *,
    offset: int = 0,
    selected_habit_id: str | None = None,
    notice: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    counts = runtime._load_local_habit_counts()
    total, rows = runtime._load_local_habit_rows(offset=offset)
    lines = [
        "<b>🧠 Local Habits</b>",
        f"Agent: <code>{html.escape(runtime.name)}</code>",
        "",
        (
            f"📊 Total <b>{counts['total']}</b> • "
            f"🟢 Active <b>{counts['active']}</b> • "
            f"🟡 Candidate <b>{counts['candidate']}</b> • "
            f"⏸ Paused <b>{counts['paused']}</b> • "
            f"🔴 Disabled <b>{counts['disabled']}</b>"
        ),
    ]
    if notice:
        lines.extend(["", f"✨ {html.escape(notice)}"])
    lines.append("")
    buttons: list[list[InlineKeyboardButton]] = []
    if not rows:
        lines.append("No local habits yet.")
    for idx, row in enumerate(rows, start=offset + 1):
        title = str(row["title"] or "").strip()
        instruction = str(row["instruction"] or "").strip()
        label = title or instruction or "(untitled)"
        task_type = str(row["task_type"] or "general")
        status = str(row["status"] or "active")
        habit_type = str(row["habit_type"] or "do")
        confidence = float(row["confidence"] or 0.0)
        icon = {"active": "🟢", "candidate": "🟡", "paused": "⏸", "disabled": "🔴"}.get(status, "⚪")
        type_icon = {"do": "✅", "avoid": "🚫"}.get(habit_type, "•")
        lines.append(f"{idx}. {icon} <b>{html.escape(label[:80])}</b>")
        lines.append(
            f"   {type_icon} <code>{html.escape(habit_type)}</code> • "
            f"<code>{html.escape(task_type)}</code> • conf <b>{confidence:.2f}</b>"
        )
        if selected_habit_id == row["habit_id"]:
            lines.append(f"   💡 {html.escape(instruction[:280])}")
            lines.append(f"   🆔 <code>{html.escape(str(row['habit_id']))}</code>")
        lines.append("")
        habit_id = str(row["habit_id"])
        buttons.append([
            InlineKeyboardButton("🔍 Detail", callback_data=f"skill:habits:view:{habit_id}:{offset}"),
            InlineKeyboardButton(
                runtime._habit_status_button_label(status, "active"),
                callback_data=f"skill:habits:set:{habit_id}:active:{offset}",
            ),
        ])
        buttons.append([
            InlineKeyboardButton(
                runtime._habit_status_button_label(status, "paused"),
                callback_data=f"skill:habits:set:{habit_id}:paused:{offset}",
            ),
            InlineKeyboardButton(
                runtime._habit_status_button_label(status, "disabled"),
                callback_data=f"skill:habits:set:{habit_id}:disabled:{offset}",
            ),
        ])
    nav: list[InlineKeyboardButton] = []
    prev_offset = max(offset - HABIT_BROWSER_PAGE_SIZE, 0)
    next_offset = offset + HABIT_BROWSER_PAGE_SIZE
    if offset > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"skill:habits:list:{prev_offset}"))
    nav.append(InlineKeyboardButton("🔄 Refresh", callback_data=f"skill:habits:list:{offset}"))
    if next_offset < total:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"skill:habits:list:{next_offset}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("📋 Governance Queue", callback_data="skill:habits:queue:0")])
    return "\n".join(lines).strip(), InlineKeyboardMarkup(buttons)


def set_local_habit_status(runtime: Any, habit_id: str, target_status: str) -> tuple[bool, str]:
    db_path = runtime._habit_db_path()
    if not db_path.exists():
        return False, "Habit store not found."
    enabled = 1 if target_status == "active" else 0
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT habit_id, status FROM habits WHERE habit_id = ? AND agent_id = ?",
            (habit_id, runtime.name),
        ).fetchone()
        if row is None:
            return False, "Habit not found."
        old_status = str(row["status"] or "")
        conn.execute(
            "UPDATE habits SET status = ?, enabled = ?, updated_at = ? WHERE habit_id = ? AND agent_id = ?",
            (target_status, enabled, now, habit_id, runtime.name),
        )
        conn.execute(
            """
            INSERT INTO habit_state_changes (habit_id, change_type, old_value, new_value, reason, changed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (habit_id, "telegram_status", old_status, target_status, f"telegram:{runtime.name}", now),
        )
    return True, f"Habit set to {target_status}."


def build_habit_governance_view(runtime: Any) -> str:
    project_root = runtime.workspace_dir.parent.parent
    rows = HabitStore.list_copy_recommendations(project_root=project_root, limit=100)
    shared_rows = HabitStore.list_shared_patterns(project_root=project_root, limit=100)
    counts: dict[str, int] = {status: 0 for status in ("pending", "approved", "applied", "rejected", "obsolete")}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    lines = [
        "<b>📋 Habit Governance Queue</b>",
        f"Agent: <code>{html.escape(runtime.name)}</code>",
        "",
        (
            f"Pending <b>{counts['pending']}</b> • Approved <b>{counts['approved']}</b> • "
            f"Applied <b>{counts['applied']}</b> • Rejected <b>{counts['rejected']}</b> • "
            f"Obsolete <b>{counts['obsolete']}</b>"
        ),
        f"🤝 Active shared patterns <b>{len(shared_rows)}</b>",
    ]
    pending = [row for row in rows if row.status == "pending"][:5]
    lines.append("")
    lines.append("<b>Recent governance items</b>")
    if not pending:
        lines.append("• No copy recommendations right now.")
    else:
        for row in pending:
            lines.append(
                "• "
                f"<code>{html.escape(row.source_agent)}</code> → "
                f"<code>{html.escape(row.target_agent)}</code> "
                f"for <code>{html.escape(row.habit_id)}</code>"
            )
    lines.append("")
    lines.append("Tip: use <code>/skill habits</code> to return to local habits.")
    return "\n".join(lines)


async def handle_habit_callback(runtime: Any, query: Any, data: str) -> bool:
    if not data.startswith("skill:habits:"):
        return False
    parts = data.split(":", 5)
    action = parts[2] if len(parts) > 2 else "list"
    if action == "list":
        offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        text, markup = runtime._build_habit_browser_view(offset=offset)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer()
        return True
    if action == "view":
        habit_id = parts[3] if len(parts) > 3 else ""
        offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        text, markup = runtime._build_habit_browser_view(offset=offset, selected_habit_id=habit_id)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer()
        return True
    if action == "set":
        habit_id = parts[3] if len(parts) > 3 else ""
        target = parts[4] if len(parts) > 4 else ""
        offset = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
        ok, message = runtime._set_local_habit_status(habit_id, target)
        text, markup = runtime._build_habit_browser_view(
            offset=offset,
            selected_habit_id=habit_id if ok else None,
            notice=message,
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer(message, show_alert=not ok)
        return True
    if action == "queue":
        await query.edit_message_text(runtime._build_habit_governance_view(), parse_mode="HTML")
        await query.answer()
        return True
    return False
