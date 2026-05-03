#!/usr/bin/env python3
"""Orchestrator for the HASHI wiki redesign pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.wiki.config import WikiConfig, default_config
    from scripts.wiki.fetcher import FetchResult, fetch_new_memories
    from scripts.wiki.state import WikiState
else:
    from .config import WikiConfig, default_config
    from .fetcher import FetchResult, fetch_new_memories
    from .state import WikiState


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the HASHI wiki redesign pipeline.")
    parser.add_argument("--daily", action="store_true", help="Run the normal daily pipeline.")
    parser.add_argument(
        "--weekly-if-saturday",
        action="store_true",
        help="Emit weekly digest when the local day is Saturday.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not classify or write pages.")
    parser.add_argument("--limit", type=int, default=None, help="Limit fetched consolidated rows.")
    parser.add_argument(
        "--skip-consolidation-check",
        action="store_true",
        help="Skip today's memory consolidation completion check.",
    )
    args = parser.parse_args(argv)

    config = default_config()
    report_lines = run_stage0(config, args)
    print("\n".join(report_lines))
    return 0


def run_stage0(config: WikiConfig, args: argparse.Namespace) -> list[str]:
    now = datetime.now(ZoneInfo(config.timezone))
    with WikiState(config.wiki_state_db) as state:
        state.init_schema()
        consolidation_ok = True
        consolidation_reason = "skipped by flag"
        if not args.skip_consolidation_check:
            consolidation_ok, consolidation_reason = check_today_consolidation(config, now)

        fetch_result = fetch_new_memories(config, state, limit=args.limit)
        lines = build_report(config, state, fetch_result, consolidation_ok, consolidation_reason, now, args)
        report_path = config.dry_run_report_latest if args.dry_run else config.report_latest
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return lines


def check_today_consolidation(config: WikiConfig, now: datetime) -> tuple[bool, str]:
    if not config.consolidation_log.exists():
        return False, f"missing consolidation log: {config.consolidation_log}"

    tz = ZoneInfo(config.timezone)
    today = now.date()
    latest_embed: datetime | None = None
    latest_scan: datetime | None = None
    latest_error: str | None = None

    for line in config.consolidation_log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            ts = datetime.fromisoformat(event["timestamp"]).astimezone(tz)
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            latest_error = f"bad log line: {exc}"
            continue
        if ts.date() != today:
            continue
        if event.get("phase") == "embed":
            latest_embed = ts
        elif "new_inserted" in event:
            latest_scan = ts

    if latest_embed:
        return True, f"today embed completed at {latest_embed.isoformat()}"
    if latest_scan:
        return False, f"today scan found at {latest_scan.isoformat()} but embed phase not complete"
    if latest_error:
        return False, latest_error
    return False, f"no consolidation event for local date {today.isoformat()}"


def build_report(
    config: WikiConfig,
    state: WikiState,
    fetch_result: FetchResult,
    consolidation_ok: bool,
    consolidation_reason: str,
    now: datetime,
    args: argparse.Namespace,
) -> list[str]:
    reason_counts = Counter(record.reason for record in fetch_result.skipped)
    domain_counts = Counter(record.domain for record in fetch_result.classifiable)
    weekly_due = bool(args.weekly_if_saturday and now.weekday() == 5)

    lines = [
        "# HASHI Wiki Pipeline Report",
        "",
        f"- Timestamp: {now.isoformat()}",
        f"- Mode: {'daily' if args.daily else 'manual'}",
        f"- Dry run: {bool(args.dry_run)}",
        f"- Consolidation check: {'ok' if consolidation_ok else 'blocked'} — {consolidation_reason}",
        f"- Weekly digest due: {weekly_due}",
        "",
        "## Stage 0 Fetch & Filter",
        "",
        f"- Last classified id: {state.get_last_classified_id()}",
        f"- Rows seen: {fetch_result.total_seen}",
        f"- Classifiable: {len(fetch_result.classifiable)}",
        f"- Redacted: {len(fetch_result.redacted)}",
        f"- Skipped: {len(fetch_result.skipped)}",
        f"- Max seen id: {fetch_result.max_seen_id}",
        "",
        "## Classifiable Domains",
    ]
    if domain_counts:
        lines.extend(f"- {domain}: {count}" for domain, count in sorted(domain_counts.items()))
    else:
        lines.append("- none")
    lines.extend(["", "## Skipped Reasons"])
    if reason_counts:
        lines.extend(f"- {reason}: {count}" for reason, count in sorted(reason_counts.items()))
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- No LLM classifier was called in Milestone 1.",
            "- No Obsidian vault pages were written.",
            "- `wiki_state.sqlite` schema was initialized only.",
        ]
    )
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
