#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from orchestrator.habits import HabitStore


def _parse_ids(raw: list[str]) -> list[int]:
    ids: list[int] = []
    for item in raw:
        for part in item.split(","):
            token = part.strip()
            if not token:
                continue
            ids.append(int(token))
    return ids


def cmd_report(args: argparse.Namespace) -> int:
    report = HabitStore.generate_recommendation_report(
        project_root=PROJECT_ROOT,
        generated_by=args.generated_by,
        lookback_days=args.lookback_days,
        max_recommendations=args.max_recommendations,
    )
    payload = {
        "summary": HabitStore.summarize_recommendation_report(report),
        "json_path": str(report.json_path),
        "markdown_path": str(report.markdown_path),
        "dashboard_json_path": str(report.dashboard_json_path),
        "dashboard_markdown_path": str(report.dashboard_markdown_path),
        "copy_recommendations": len(report.copy_recommendations),
        "pending_copy_approvals": len([item for item in report.copy_recommendations if item.status == HabitStore.COPY_APPROVAL_STATUS_PENDING]),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    report = HabitStore.generate_recommendation_report(
        project_root=PROJECT_ROOT,
        generated_by=args.generated_by,
        lookback_days=args.lookback_days,
        max_recommendations=args.max_recommendations,
    )
    payload = {
        "summary": HabitStore.summarize_recommendation_report(report),
        "dashboard_json_path": str(report.dashboard_json_path),
        "dashboard_markdown_path": str(report.dashboard_markdown_path),
        "class_summaries": report.class_summaries,
        "backend_summaries": report.backend_summaries,
        "timestamp_source_summaries": report.timestamp_source_summaries,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = HabitStore.list_copy_recommendations(
        project_root=PROJECT_ROOT,
        status=args.status,
        limit=args.limit,
    )
    payload = [
        {
            "id": row.recommendation_id,
            "status": row.status,
            "source_agent_id": row.source_agent_id,
            "source_habit_id": row.source_habit_id,
            "target_agent_id": row.target_agent_id,
            "task_type": row.task_type,
            "summary": row.summary,
            "reviewed_by": row.reviewed_by,
            "reviewed_at": row.reviewed_at,
            "copied_habit_id": row.copied_habit_id,
        }
        for row in rows
    ]
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    rows = HabitStore.approve_copy_recommendations(
        project_root=PROJECT_ROOT,
        reviewer=args.reviewer,
        recommendation_ids=_parse_ids(args.ids) if args.ids else None,
        note=args.note,
    )
    print(json.dumps({"approved": len(rows)}, indent=2, ensure_ascii=False))
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    rows = HabitStore.reject_copy_recommendations(
        project_root=PROJECT_ROOT,
        reviewer=args.reviewer,
        recommendation_ids=_parse_ids(args.ids) if args.ids else None,
        note=args.note,
    )
    print(json.dumps({"rejected": len(rows)}, indent=2, ensure_ascii=False))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    rows = HabitStore.apply_approved_copy_recommendations(
        project_root=PROJECT_ROOT,
        reviewer=args.reviewer,
        limit=args.limit,
    )
    payload = [
        {
            "id": row.recommendation_id,
            "source_agent_id": row.source_agent_id,
            "target_agent_id": row.target_agent_id,
            "copied_habit_id": row.copied_habit_id,
            "status": row.status,
        }
        for row in rows
    ]
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_shared_list(args: argparse.Namespace) -> int:
    rows = HabitStore.list_shared_patterns(
        project_root=PROJECT_ROOT,
        status=args.status,
        agent_class=args.agent_class,
        limit=args.limit,
    )
    payload = [
        {
            "shared_pattern_id": row.shared_pattern_id,
            "kind": row.kind,
            "status": row.status,
            "title": row.title,
            "target_agent_class": row.target_agent_class,
            "owner": row.owner,
            "source_agent_id": row.source_agent_id,
            "source_habit_id": row.source_habit_id,
            "confidence": row.confidence,
            "helpful_recent": row.helpful_recent,
            "harmful_recent": row.harmful_recent,
            "triggered_recent": row.triggered_recent,
        }
        for row in rows
    ]
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_shared_promote(args: argparse.Namespace) -> int:
    try:
        row = HabitStore.promote_habit_to_shared_pattern(
            project_root=PROJECT_ROOT,
            reviewer=args.reviewer,
            source_agent_id=args.source_agent_id,
            habit_id=args.habit_id,
            kind=args.kind,
            target_agent_class=args.target_agent_class,
            owner=args.owner,
            note=args.note,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "shared_pattern_id": row.shared_pattern_id,
                "kind": row.kind,
                "status": row.status,
                "target_agent_class": row.target_agent_class,
                "owner": row.owner,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def cmd_shared_retire(args: argparse.Namespace) -> int:
    try:
        row = HabitStore.retire_shared_pattern(
            project_root=PROJECT_ROOT,
            reviewer=args.reviewer,
            shared_pattern_id=args.shared_pattern_id,
            note=args.note,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "shared_pattern_id": row.shared_pattern_id,
                "status": row.status,
                "retired_by": row.retired_by,
                "retired_at": row.retired_at,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Lily habit copy recommendations.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    report = sub.add_parser("report")
    report.add_argument("--generated-by", default="habit_recommendations.py")
    report.add_argument("--lookback-days", type=int, default=7)
    report.add_argument("--max-recommendations", type=int, default=12)
    report.set_defaults(func=cmd_report)

    dashboard = sub.add_parser("dashboard")
    dashboard.add_argument("--generated-by", default="habit_recommendations.py:dashboard")
    dashboard.add_argument("--lookback-days", type=int, default=7)
    dashboard.add_argument("--max-recommendations", type=int, default=12)
    dashboard.set_defaults(func=cmd_dashboard)

    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--status", default=None)
    list_cmd.add_argument("--limit", type=int, default=50)
    list_cmd.set_defaults(func=cmd_list)

    approve = sub.add_parser("approve")
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--id", dest="ids", action="append")
    approve.add_argument("--note", default=None)
    approve.set_defaults(func=cmd_approve)

    reject = sub.add_parser("reject")
    reject.add_argument("--reviewer", required=True)
    reject.add_argument("--id", dest="ids", action="append")
    reject.add_argument("--note", default=None)
    reject.set_defaults(func=cmd_reject)

    apply_cmd = sub.add_parser("apply")
    apply_cmd.add_argument("--reviewer", required=True)
    apply_cmd.add_argument("--limit", type=int, default=50)
    apply_cmd.set_defaults(func=cmd_apply)

    shared_list = sub.add_parser("shared-list")
    shared_list.add_argument("--status", default=None)
    shared_list.add_argument("--agent-class", default=None)
    shared_list.add_argument("--limit", type=int, default=50)
    shared_list.set_defaults(func=cmd_shared_list)

    shared_promote = sub.add_parser("shared-promote")
    shared_promote.add_argument("--reviewer", required=True)
    shared_promote.add_argument("--source-agent-id", required=True)
    shared_promote.add_argument("--habit-id", required=True)
    shared_promote.add_argument("--kind", default="pattern")
    shared_promote.add_argument("--target-agent-class", default=None)
    shared_promote.add_argument("--owner", default=None)
    shared_promote.add_argument("--note", default=None)
    shared_promote.set_defaults(func=cmd_shared_promote)

    shared_retire = sub.add_parser("shared-retire")
    shared_retire.add_argument("--reviewer", required=True)
    shared_retire.add_argument("--shared-pattern-id", required=True)
    shared_retire.add_argument("--note", default=None)
    shared_retire.set_defaults(func=cmd_shared_retire)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
