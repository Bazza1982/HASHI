#!/usr/bin/env python3
from __future__ import annotations

import os
import shlex
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("BRIDGE_PROJECT_ROOT", Path(__file__).resolve().parent.parent.parent))
WORKSPACE_DIR = Path(os.environ.get("BRIDGE_WORKSPACE_DIR", PROJECT_ROOT / "workspaces" / "lily"))
AGENT_NAME = WORKSPACE_DIR.name

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from orchestrator.habits import HabitStore


def _usage() -> str:
    return "\n".join(
        [
            "Habit Commands",
            "",
            "Usage:",
            "  /skill habits                       # show this agent's local habits",
            "  /skill habits local [status] [limit]",
            "  /skill habits status                # governance queue summary",
            "  /skill habits report                # regenerate Lily governance report",
            "  /skill habits dashboard             # regenerate Lily governance dashboard",
            "  /skill habits list [pending|approved|applied|rejected|obsolete] [limit]",
            "  /skill habits approve <id[,id...]> [note...]",
            "  /skill habits reject <id[,id...]> [note...]",
            "  /skill habits apply [limit]",
            "  /skill habits shared list [active|retired] [limit]",
            "  /skill habits shared promote <source_agent> <habit_id> [pattern|protocol] [target_class]",
            "  /skill habits shared retire <shared_pattern_id> [note...]",
            "",
            "Notes:",
            "  approve / reject / apply / shared promote / shared retire are Lily-only operations.",
            "  Use /good or /bad to record user signals (processed during dream).",
        ]
    )


def _parse_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        ids.append(int(token))
    return ids


def _format_row(row) -> str:
    title = row.source_title or row.summary or "(untitled)"
    metrics = f"conf={row.confidence:.2f} helpful={row.helpful_recent} harmful={row.harmful_recent} triggered={row.triggered_recent}"
    review = ""
    if row.reviewed_by or row.review_note:
        parts: list[str] = []
        if row.reviewed_by:
            parts.append(f"by {row.reviewed_by}")
        if row.review_note:
            parts.append(row.review_note)
        review = f" | review: {'; '.join(parts)}"
    copied = f" | copied_habit={row.copied_habit_id}" if row.copied_habit_id else ""
    return (
        f"- #{row.recommendation_id} [{row.status}] {row.source_agent_id} -> {row.target_agent_id} "
        f"({row.task_type or 'general'})\n"
        f"  {title}\n"
        f"  {metrics}{review}{copied}"
    )


def _format_shared_pattern(row) -> str:
    source = f"{row.source_agent_id}/{row.source_habit_id}" if row.source_agent_id and row.source_habit_id else "n/a"
    return (
        f"- {row.shared_pattern_id} [{row.kind}/{row.status}] class={row.target_agent_class} owner={row.owner}\n"
        f"  {row.title}\n"
        f"  source={source} conf={row.confidence:.2f} helpful={row.helpful_recent} harmful={row.harmful_recent} triggered={row.triggered_recent}"
    )


def _format_local_habit(row) -> str:
    title = str(row["title"] or "").strip()
    instruction = str(row["instruction"] or "").strip()
    label = title or instruction or "(untitled)"
    task_type = str(row["task_type"] or "general")
    return (
        f"- {row['habit_id']} [{row['status']}/{row['habit_type']}] ({task_type}) conf={float(row['confidence'] or 0):.2f}\n"
        f"  {label}"
    )


def _local_habit_counts() -> dict[str, int]:
    db_path = WORKSPACE_DIR / "habits.sqlite"
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
            (AGENT_NAME,),
        ).fetchone()
    if row:
        counts = {key: int(row[key] or 0) for key in counts}
    return counts


def cmd_local(argv: list[str]) -> str:
    db_path = WORKSPACE_DIR / "habits.sqlite"
    counts = _local_habit_counts()
    status = None
    limit = 20
    valid_statuses = {"active", "candidate", "paused", "disabled"}
    if argv:
        first = argv[0].lower()
        if first in valid_statuses:
            status = first
            argv = argv[1:]
    if argv:
        try:
            limit = int(argv[0])
        except ValueError as exc:
            raise SystemExit(f"Invalid limit: {argv[0]}") from exc

    lines = [
        "Local Habits",
        f"Agent: {AGENT_NAME}",
        (
            f"Total: {counts['total']} | Active: {counts['active']} | "
            f"Candidate: {counts['candidate']} | Paused: {counts['paused']} | "
            f"Disabled: {counts['disabled']}"
        ),
        "",
    ]
    if not db_path.exists():
        lines.append("No local habit store yet.")
        lines.extend(["", _usage()])
        return "\n".join(lines)

    query = """
        SELECT habit_id, status, habit_type, title, instruction, task_type, confidence
        FROM habits
        WHERE agent_id = ?
    """
    params: list[object] = [AGENT_NAME]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += """
        ORDER BY
            CASE status WHEN 'active' THEN 0 WHEN 'candidate' THEN 1 WHEN 'paused' THEN 2 ELSE 3 END,
            confidence DESC,
            updated_at DESC
        LIMIT ?
    """
    params.append(limit)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()

    if status:
        lines.append(f"Showing: {status}")
        lines.append("")
    if rows:
        lines.extend(_format_local_habit(row) for row in rows)
    else:
        lines.append("No local habits found.")
    lines.extend(["", "Tip: use /skill habits status to see governance queue.", "", _usage()])
    return "\n".join(lines)


def _status_text(project_root: Path) -> str:
    rows = HabitStore.list_copy_recommendations(project_root=project_root, limit=100)
    shared_rows = HabitStore.list_shared_patterns(project_root=project_root, limit=100)
    local_counts = _local_habit_counts()
    counts: dict[str, int] = {status: 0 for status in ("pending", "approved", "applied", "rejected", "obsolete")}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    lines = [
        "Habit Status",
        f"Agent: {AGENT_NAME}",
        "",
        "Local habits on this agent:",
        (
            f"Total: {local_counts['total']} | Active: {local_counts['active']} | "
            f"Candidate: {local_counts['candidate']} | Paused: {local_counts['paused']} | "
            f"Disabled: {local_counts['disabled']}"
        ),
        "",
        "Global governance queue (not the same as local habit counts):",
        (
            f"Pending copy approvals: {counts['pending']} | Approved: {counts['approved']} | "
            f"Applied: {counts['applied']} | Rejected: {counts['rejected']} | Obsolete: {counts['obsolete']}"
        ),
        f"Active shared patterns: {len([item for item in shared_rows if item.status == HabitStore.SHARED_PATTERN_STATUS_ACTIVE])}",
        "",
        "Recent governance items:",
    ]
    if rows:
        lines.extend(_format_row(row) for row in rows[:5])
    else:
        lines.append("- No copy recommendations right now.")
    lines.extend(["", _usage()])
    return "\n".join(lines)


def _require_lily() -> str | None:
    if AGENT_NAME.lower() != "lily":
        return (
            f"Mutating habit approvals are Lily-only. Current agent is '{AGENT_NAME}'.\n"
            "Use Lily to run approve / reject / apply / shared promote / shared retire."
        )
    return None


def cmd_report(project_root: Path, argv: list[str]) -> str:
    lookback_days = 7
    max_recommendations = 12
    if argv:
        try:
            lookback_days = int(argv[0])
        except ValueError as exc:
            raise SystemExit(f"Invalid lookback days: {argv[0]}") from exc
    if len(argv) > 1:
        try:
            max_recommendations = int(argv[1])
        except ValueError as exc:
            raise SystemExit(f"Invalid max recommendations: {argv[1]}") from exc
    report = HabitStore.generate_recommendation_report(
        project_root=project_root,
        generated_by=f"skill:habits:{AGENT_NAME}",
        lookback_days=lookback_days,
        max_recommendations=max_recommendations,
    )
    summary = HabitStore.summarize_recommendation_report(report)
    pending = len([item for item in report.copy_recommendations if item.status == HabitStore.COPY_APPROVAL_STATUS_PENDING])
    return "\n".join(
        [
            "Habit report regenerated.",
            summary,
            f"Pending copy approvals: {pending}",
            f"Active shared patterns: {len(report.shared_patterns)}",
            f"Markdown: {report.markdown_path}",
            f"JSON: {report.json_path}",
            f"Dashboard markdown: {report.dashboard_markdown_path}",
            f"Dashboard JSON: {report.dashboard_json_path}",
        ]
    )


def cmd_dashboard(project_root: Path, argv: list[str]) -> str:
    lookback_days = 7
    max_recommendations = 12
    if argv:
        try:
            lookback_days = int(argv[0])
        except ValueError as exc:
            raise SystemExit(f"Invalid lookback days: {argv[0]}") from exc
    if len(argv) > 1:
        try:
            max_recommendations = int(argv[1])
        except ValueError as exc:
            raise SystemExit(f"Invalid max recommendations: {argv[1]}") from exc
    report = HabitStore.generate_recommendation_report(
        project_root=project_root,
        generated_by=f"skill:habits:dashboard:{AGENT_NAME}",
        lookback_days=lookback_days,
        max_recommendations=max_recommendations,
    )
    top_class = report.class_summaries[0]["name"] if report.class_summaries else "n/a"
    top_backend = report.backend_summaries[0]["name"] if report.backend_summaries else "n/a"
    return "\n".join(
        [
            "Habit dashboard regenerated.",
            HabitStore.summarize_recommendation_report(report),
            f"Top class bucket: {top_class}",
            f"Top backend bucket: {top_backend}",
            f"Dashboard markdown: {report.dashboard_markdown_path}",
            f"Dashboard JSON: {report.dashboard_json_path}",
        ]
    )


def cmd_list(project_root: Path, argv: list[str]) -> str:
    status = None
    limit = 20
    valid_statuses = {"pending", "approved", "applied", "rejected", "obsolete"}
    if argv:
        first = argv[0].lower()
        if first in valid_statuses:
            status = first
            argv = argv[1:]
    if argv:
        try:
            limit = int(argv[0])
        except ValueError as exc:
            raise SystemExit(f"Invalid limit: {argv[0]}") from exc
    rows = HabitStore.list_copy_recommendations(project_root=project_root, status=status, limit=limit)
    lines = [f"Habit copy recommendations{f' [{status}]' if status else ''}"]
    if not rows:
        lines.append("")
        lines.append("No rows found.")
        return "\n".join(lines)
    lines.append("")
    lines.extend(_format_row(row) for row in rows)
    return "\n".join(lines)


def cmd_approve(project_root: Path, argv: list[str]) -> str:
    error = _require_lily()
    if error:
        return error
    if not argv:
        raise SystemExit("Usage: /skill habits approve <id[,id...]> [note...]")
    ids = _parse_ids(argv[0])
    note = " ".join(argv[1:]).strip() or None
    rows = HabitStore.approve_copy_recommendations(
        project_root=project_root,
        reviewer=AGENT_NAME,
        recommendation_ids=ids,
        note=note,
    )
    lines = [f"Approved: {len(rows)}"]
    if rows:
        lines.append("")
        lines.extend(_format_row(row) for row in rows)
    return "\n".join(lines)


def cmd_reject(project_root: Path, argv: list[str]) -> str:
    error = _require_lily()
    if error:
        return error
    if not argv:
        raise SystemExit("Usage: /skill habits reject <id[,id...]> [note...]")
    ids = _parse_ids(argv[0])
    note = " ".join(argv[1:]).strip() or None
    rows = HabitStore.reject_copy_recommendations(
        project_root=project_root,
        reviewer=AGENT_NAME,
        recommendation_ids=ids,
        note=note,
    )
    lines = [f"Rejected: {len(rows)}"]
    if rows:
        lines.append("")
        lines.extend(_format_row(row) for row in rows)
    return "\n".join(lines)


def cmd_apply(project_root: Path, argv: list[str]) -> str:
    error = _require_lily()
    if error:
        return error
    limit = 50
    if argv:
        try:
            limit = int(argv[0])
        except ValueError as exc:
            raise SystemExit(f"Invalid limit: {argv[0]}") from exc
    rows = HabitStore.apply_approved_copy_recommendations(
        project_root=project_root,
        reviewer=AGENT_NAME,
        limit=limit,
    )
    lines = [f"Applied: {len(rows)}"]
    if rows:
        lines.append("")
        lines.extend(_format_row(row) for row in rows)
    return "\n".join(lines)


def cmd_shared(project_root: Path, argv: list[str]) -> str:
    if not argv:
        rows = HabitStore.list_shared_patterns(project_root=project_root, limit=20)
        lines = ["Shared patterns and protocols"]
        if rows:
            lines.append("")
            lines.extend(_format_shared_pattern(row) for row in rows)
        else:
            lines.extend(["", "No shared patterns yet."])
        return "\n".join(lines)

    action = argv[0].lower()
    rest = argv[1:]
    if action == "list":
        status = None
        limit = 20
        if rest and rest[0].lower() in {"active", "retired"}:
            status = rest[0].lower()
            rest = rest[1:]
        if rest:
            try:
                limit = int(rest[0])
            except ValueError as exc:
                raise SystemExit(f"Invalid limit: {rest[0]}") from exc
        rows = HabitStore.list_shared_patterns(project_root=project_root, status=status, limit=limit)
        lines = [f"Shared patterns{f' [{status}]' if status else ''}"]
        if rows:
            lines.append("")
            lines.extend(_format_shared_pattern(row) for row in rows)
        else:
            lines.extend(["", "No rows found."])
        return "\n".join(lines)

    error = _require_lily()
    if error:
        return error
    if action == "promote":
        if len(rest) < 2:
            raise SystemExit("Usage: /skill habits shared promote <source_agent> <habit_id> [pattern|protocol] [target_class]")
        source_agent_id = rest[0]
        habit_id = rest[1]
        kind = rest[2] if len(rest) > 2 else "pattern"
        target_class = rest[3] if len(rest) > 3 else None
        try:
            row = HabitStore.promote_habit_to_shared_pattern(
                project_root=project_root,
                reviewer=AGENT_NAME,
                source_agent_id=source_agent_id,
                habit_id=habit_id,
                kind=kind,
                target_agent_class=target_class,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        registry_path = HabitStore.export_shared_pattern_registry(project_root=project_root)
        return "\n".join(
            [
                "Shared pattern promoted.",
                _format_shared_pattern(row),
                f"Registry: {registry_path}",
            ]
        )
    if action == "retire":
        if not rest:
            raise SystemExit("Usage: /skill habits shared retire <shared_pattern_id> [note...]")
        shared_pattern_id = rest[0]
        note = " ".join(rest[1:]).strip() or None
        try:
            row = HabitStore.retire_shared_pattern(
                project_root=project_root,
                reviewer=AGENT_NAME,
                shared_pattern_id=shared_pattern_id,
                note=note,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        registry_path = HabitStore.export_shared_pattern_registry(project_root=project_root)
        return "\n".join(
            [
                "Shared pattern retired.",
                _format_shared_pattern(row),
                f"Registry: {registry_path}",
            ]
        )
    raise SystemExit(f"Unknown habits shared action: {action}\n\n{_usage()}")


def _read_transcript_context() -> str:
    """Read the full transcript.jsonl, including thinking tokens."""
    if not TRANSCRIPT_PATH.exists():
        return ""
    lines: list[str] = []
    try:
        raw = TRANSCRIPT_PATH.read_text(encoding="utf-8")
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                import json as _json
                entry = _json.loads(line)
            except Exception:
                continue
            role = entry.get("role", "")
            source = entry.get("source", "")
            text = entry.get("text", "")
            ts = entry.get("ts", "")
            # Skip pure system noise
            if source in ("startup", "handoff", "fyi", "system"):
                continue
            if source == "think":
                prefix = f"[THINKING{' @ ' + ts if ts else ''}]"
            elif role == "user":
                prefix = f"[USER{' @ ' + ts if ts else ''}]"
            else:
                prefix = f"[AGENT{' @ ' + ts if ts else ''}]"
            lines.append(f"{prefix} {text}")
    except Exception:
        pass
    return "\n".join(lines)


def cmd_signal(project_root: Path, signal: str, argv: list[str]) -> str:
    """Record a /good or /bad user signal with optional comment."""
    comment = " ".join(argv).strip() or None
    context = _read_transcript_context()
    if not context:
        return f"/{signal} recorded (no transcript context available). Will be processed during dream."

    store = HabitStore(
        workspace_dir=WORKSPACE_DIR,
        project_root=project_root,
        agent_id=AGENT_NAME,
    )
    signal_id = store.record_user_signal(signal=signal, comment=comment, context=context)
    comment_note = f" — \"{comment}\"" if comment else ""
    return (
        f"/{signal} signal recorded (id={signal_id}){comment_note}.\n"
        f"Context captured: {len(context.split())} words from transcript.\n"
        f"Will be processed into habits during next dream. 🌙"
    )


def main() -> int:
    raw = " ".join(sys.argv[1:]).strip()
    if not raw:
        print(cmd_local([]))
        return 0

    argv = shlex.split(raw)
    action = argv[0].lower()
    rest = argv[1:]

    if action == "help":
        print(_usage())
        return 0
    if action == "status":
        print(_status_text(PROJECT_ROOT))
        return 0
    if action == "local":
        print(cmd_local(rest))
        return 0
    if action == "report":
        print(cmd_report(PROJECT_ROOT, rest))
        return 0
    if action == "dashboard":
        print(cmd_dashboard(PROJECT_ROOT, rest))
        return 0
    if action == "list":
        print(cmd_list(PROJECT_ROOT, rest))
        return 0
    if action == "approve":
        print(cmd_approve(PROJECT_ROOT, rest))
        return 0
    if action == "reject":
        print(cmd_reject(PROJECT_ROOT, rest))
        return 0
    if action == "apply":
        print(cmd_apply(PROJECT_ROOT, rest))
        return 0
    if action == "shared":
        print(cmd_shared(PROJECT_ROOT, rest))
        return 0
    if action in {"good", "bad"}:
        print(cmd_signal(PROJECT_ROOT, action, rest))
        return 0

    raise SystemExit(f"Unknown habits action: {action}\n\n{_usage()}")


if __name__ == "__main__":
    raise SystemExit(main())
