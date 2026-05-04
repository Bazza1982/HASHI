#!/usr/bin/env python3
"""Orchestrator for the HASHI wiki redesign pipeline."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.wiki.classifier import ClassificationDryRunResult, classify_memories_dry_run
    from scripts.wiki.config import WikiConfig, default_config
    from scripts.wiki.fetcher import FetchResult, fetch_new_memories
    from scripts.wiki.page_generator import PageDraft, generate_dry_run_pages
    from scripts.wiki.state import WikiState
else:
    from .classifier import ClassificationDryRunResult, classify_memories_dry_run
    from .config import WikiConfig, default_config
    from .fetcher import FetchResult, fetch_new_memories
    from .page_generator import PageDraft, generate_dry_run_pages
    from .state import WikiState


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the HASHI wiki redesign pipeline.")
    parser.add_argument("--daily", action="store_true", help="Run the normal daily pipeline.")
    parser.add_argument(
        "--weekly-if-saturday",
        action="store_true",
        help="Emit weekly digest when the local day is Saturday.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not persist classifications or write pages.")
    parser.add_argument("--classify", action="store_true", help="Run the classifier stage.")
    parser.add_argument(
        "--classify-dry-run",
        action="store_true",
        help="Alias for --classify --dry-run.",
    )
    parser.add_argument(
        "--mock-classifier",
        action="store_true",
        help="Use deterministic local mock classifications for tests and pipeline smoke checks.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit fetched consolidated rows.")
    parser.add_argument(
        "--max-classify",
        type=int,
        default=None,
        help="Limit classifiable rows sent to the classifier during dry-run.",
    )
    parser.add_argument(
        "--persist-classifications",
        action="store_true",
        help="Persist classifier assignments and advance the safe watermark.",
    )
    parser.add_argument(
        "--pages-dry-run",
        action="store_true",
        help="Generate topic page drafts to the local dry-run directory.",
    )
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
        fetch_result = drop_existing_completed_runs(state, fetch_result)
        classifier_result = None
        page_drafts: list[PageDraft] = []
        run_classifier = bool(args.classify or args.classify_dry_run)
        if args.classify_dry_run:
            args.dry_run = True
        if args.persist_classifications and args.dry_run:
            raise ValueError("--persist-classifications cannot be combined with --dry-run")
        if run_classifier:
            if consolidation_ok:
                records_to_classify = fetch_result.classifiable[: args.max_classify]
                classifier_result = classify_memories_dry_run(
                    records_to_classify,
                    config,
                    mock=args.mock_classifier,
                )
                if args.persist_classifications:
                    persist_classification_state(
                        state,
                        fetch_result,
                        records_to_classify,
                        classifier_result,
                    )
        if args.pages_dry_run:
            page_drafts = generate_dry_run_pages(config)
        lines = build_report(
            config,
            state,
            fetch_result,
            consolidation_ok,
            consolidation_reason,
            now,
            args,
            classifier_result,
            page_drafts,
        )
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
    classifier_result: ClassificationDryRunResult | None = None,
    page_drafts: list[PageDraft] | None = None,
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
        f"- Classifier stage: {bool(args.classify or args.classify_dry_run)}",
        f"- Persist classifications: {bool(args.persist_classifications)}",
        f"- Pages dry run: {bool(args.pages_dry_run)}",
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
    lines.extend(["", "## Classifier Dry Run"])
    if classifier_result is None:
        lines.append("- not run")
    else:
        topic_counts = Counter()
        low_confidence = 0
        for assignment in classifier_result.assignments:
            for topic in assignment.topics:
                topic_counts[topic] += 1
            if assignment.confidence < 0.7:
                low_confidence += 1
        lines.extend(
            [
                f"- Backend: {classifier_result.backend}",
                f"- Model: {classifier_result.model}",
                f"- Assignments: {len(classifier_result.assignments)}",
                f"- Low confidence: {low_confidence}",
                f"- Raw response chars: {classifier_result.raw_chars}",
            ]
        )
        lines.append("")
        lines.append("### Topic Counts")
        if topic_counts:
            lines.extend(f"- {topic}: {count}" for topic, count in sorted(topic_counts.items()))
        else:
            lines.append("- none")
    lines.extend(["", "## Page Drafts"])
    if not page_drafts:
        lines.append("- not generated")
    else:
        lines.extend(
            f"- {draft.topic_id}: {draft.memory_count} memories -> {draft.path}"
            for draft in page_drafts
        )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Classifier assignments are persisted only when `--persist-classifications` is set.",
            "- No Obsidian vault pages were written.",
            "- `last_classified_id` advances only across real source rows with ok/skipped/redacted state.",
        ]
    )
    return lines


def persist_classification_state(
    state: WikiState,
    fetch_result: FetchResult,
    records_to_classify,
    classifier_result: ClassificationDryRunResult,
) -> int:
    batch_id = f"wiki-classify-{uuid.uuid4().hex[:12]}"
    state.record_skipped_runs(fetch_result.skipped, batch_id=batch_id, status="skipped")
    state.record_skipped_runs(fetch_result.redacted, batch_id=batch_id, status="redacted")
    # Rows beyond --max-classify are intentionally left unrecorded so they are refetched next run.
    state.record_assignments(
        records_to_classify,
        classifier_result.assignments,
        batch_id=batch_id,
        classifier_model=f"{classifier_result.backend}/{classifier_result.model}",
    )
    return state.advance_watermark(fetch_result.source_ids)


def drop_existing_completed_runs(state: WikiState, fetch_result: FetchResult) -> FetchResult:
    all_ids = [
        record.id
        for records in (fetch_result.classifiable, fetch_result.skipped, fetch_result.redacted)
        for record in records
    ]
    completed = state.existing_completed_runs(all_ids)
    if not completed:
        return fetch_result
    return FetchResult(
        classifiable=[record for record in fetch_result.classifiable if record.id not in completed],
        skipped=[record for record in fetch_result.skipped if record.id not in completed],
        redacted=[record for record in fetch_result.redacted if record.id not in completed],
        max_seen_id=fetch_result.max_seen_id,
        source_ids=fetch_result.source_ids,
    )


if __name__ == "__main__":
    raise SystemExit(main())
