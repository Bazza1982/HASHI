#!/usr/bin/env python3
"""
backfill_codex_tokens.py — Backfill accurate token data from codex_exec_events.jsonl
into token_usage.jsonl for all agents that used codex-cli backend.

Usage:
    python scripts/backfill_codex_tokens.py --dry-run    # Preview changes
    python scripts/backfill_codex_tokens.py --apply       # Apply changes

Data flow:
    codex_exec_events.jsonl (turn.completed events with real usage)
    → matched 1:1 in sequential order with codex-cli entries in token_usage.jsonl
    → original entries updated with real input/output/cost values
    → backup created before any modification
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from tools import token_tracker  # noqa: E402

WORKSPACE_ROOT = CODE_ROOT / "workspaces"


def calc_cost_with_cache(input_tokens: int, cached_tokens: int,
                         output_tokens: int, model: str | None) -> float | None:
    """Use the shared table only for known models; unknown is never a price."""
    if not token_tracker.model_has_pricing(model):
        return None
    return token_tracker.calc_cost(
        input_tokens, output_tokens, model, cached_tokens=cached_tokens,
        thinking_in_output=True,
    )


def load_turn_completed_events(events_path: Path) -> list[dict]:
    """Extract all turn.completed events in order."""
    events = []
    with open(events_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict):
                    events.append(e["usage"])
            except (json.JSONDecodeError, KeyError):
                continue
    return events


def load_token_usage(usage_path: Path) -> list[dict]:
    """Load all token_usage.jsonl entries."""
    records = []
    with open(usage_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def backfill_agent(agent: str, dry_run: bool) -> dict:
    """Backfill one agent's token_usage.jsonl with real codex data.
    Returns stats dict."""
    events_path = WORKSPACE_ROOT / agent / "codex_exec_events.jsonl"
    usage_path = WORKSPACE_ROOT / agent / "token_usage.jsonl"

    stats = {
        "agent": agent,
        "events": 0,
        "codex_entries": 0,
        "matched": 0,
        "skipped": 0,
        "old_input_total": 0,
        "new_input_total": 0,
        "old_cost_total": 0.0,
        "new_cost_total": 0.0,
        "old_unknown_cost_requests": 0,
        "new_unknown_cost_requests": 0,
    }

    if not events_path.exists():
        return stats
    if not usage_path.exists():
        return stats

    # Load data
    events = load_turn_completed_events(events_path)
    all_records = load_token_usage(usage_path)

    stats["events"] = len(events)

    # Separate codex-cli entries (preserve indices for reconstruction)
    codex_indices = []
    for i, rec in enumerate(all_records):
        if rec.get("backend") == "codex-cli":
            codex_indices.append(i)

    stats["codex_entries"] = len(codex_indices)

    if not events or not codex_indices:
        return stats

    # Match 1:1 in order: event[0] → codex_entry[0], etc.
    match_count = min(len(events), len(codex_indices))
    modified = False

    for j in range(match_count):
        event_usage = events[j]
        rec_idx = codex_indices[j]
        rec = all_records[rec_idx]

        real_input = event_usage.get("input_tokens", 0)
        real_cached = event_usage.get("cached_input_tokens", 0)
        real_output = event_usage.get("output_tokens", 0)

        # Sanity check: if real_input is 0 or implausibly low, skip
        if real_input <= 0:
            stats["skipped"] += 1
            continue

        model = rec.get("model")
        old_input = rec.get("input", 0)
        old_cost = rec.get("cost_usd")

        # Token repair must not overwrite an explicitly provider-reported cost,
        # including a real zero, with a price-table estimate.
        if rec.get("cost_source") == "provider" and old_cost is not None:
            new_cost, cost_source = old_cost, "provider"
        else:
            new_cost = calc_cost_with_cache(real_input, real_cached, real_output, model)
            cost_source = "pricing_table" if new_cost is not None else "unknown"

        stats["old_input_total"] += old_input
        stats["new_input_total"] += real_input
        for label, cost in (("old", old_cost), ("new", new_cost)):
            if cost is None:
                stats[f"{label}_unknown_cost_requests"] += 1
            else:
                stats[f"{label}_cost_total"] += float(cost)
        stats["matched"] += 1

        # Update the record in place
        rec["input"] = real_input
        rec["output"] = real_output
        rec["cached_input"] = real_cached
        rec["cost_usd"] = new_cost
        rec["cost_known"] = new_cost is not None
        rec["cost_source"] = cost_source
        if cost_source != "provider":
            rec["pricing_revisions"] = (
                [token_tracker.PRICING_REVISION] if cost_source == "pricing_table" else []
            )
        rec["backfilled"] = True  # Mark as corrected
        all_records[rec_idx] = rec
        modified = True

    if not dry_run and modified:
        # Create backup
        backup_path = usage_path.with_suffix(".jsonl.bak")
        shutil.copy2(usage_path, backup_path)

        # Write updated file
        with open(usage_path, "w", encoding="utf-8") as f:
            for rec in all_records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    return stats


def format_cost_columns(stats: dict) -> str:
    """Keep unknown counts visible and compare costs only when both are complete."""
    old = token_tracker.format_usage_cost({
        "cost_usd": stats["old_cost_total"], "requests": stats["matched"],
        "unknown_cost_requests": stats["old_unknown_cost_requests"],
    })
    new = token_tracker.format_usage_cost({
        "cost_usd": stats["new_cost_total"], "requests": stats["matched"],
        "unknown_cost_requests": stats["new_unknown_cost_requests"],
    })
    delta = "n/a"
    if not (stats["old_unknown_cost_requests"] or stats["new_unknown_cost_requests"]):
        delta = f"${stats['new_cost_total'] - stats['old_cost_total']:+.4f}"
    return f"{old:>10s}  {new:>10s}  {delta:>10s}"


def main():
    parser = argparse.ArgumentParser(description="Backfill codex token data")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview without changes")
    group.add_argument("--apply", action="store_true", help="Apply backfill")
    args = parser.parse_args()

    agents = sorted([
        d.name for d in WORKSPACE_ROOT.iterdir()
        if d.is_dir() and (d / "codex_exec_events.jsonl").exists()
    ])

    print(f"{'Agent':12s}  {'Events':>7s}  {'Usage':>6s}  {'Match':>6s}  {'Skip':>5s}  "
          f"{'Old Input':>12s}  {'New Input':>12s}  {'Ratio':>7s}  "
          f"{'Old Cost':>10s}  {'New Cost':>10s}  {'Cost Δ':>10s}")
    print("─" * 120)

    total_stats = {
        "matched": 0, "old_input": 0, "new_input": 0,
        "old_cost_total": 0.0, "new_cost_total": 0.0,
        "old_unknown_cost_requests": 0, "new_unknown_cost_requests": 0,
    }

    for agent in agents:
        s = backfill_agent(agent, dry_run=args.dry_run)
        if s["matched"] == 0:
            continue

        ratio = s["new_input_total"] / max(1, s["old_input_total"])

        print(f"{s['agent']:12s}  {s['events']:7d}  {s['codex_entries']:6d}  {s['matched']:6d}  {s['skipped']:5d}  "
              f"{s['old_input_total']:12,d}  {s['new_input_total']:12,d}  {ratio:7.1f}x  "
              f"{format_cost_columns(s)}")

        total_stats["matched"] += s["matched"]
        total_stats["old_input"] += s["old_input_total"]
        total_stats["new_input"] += s["new_input_total"]
        for key in ("old_cost_total", "new_cost_total",
                    "old_unknown_cost_requests", "new_unknown_cost_requests"):
            total_stats[key] += s[key]

    print("─" * 120)
    t = total_stats
    ratio = t["new_input"] / max(1, t["old_input"])
    print(f"{'TOTAL':12s}  {'':7s}  {'':6s}  {t['matched']:6d}  {'':5s}  "
          f"{t['old_input']:12,d}  {t['new_input']:12,d}  {ratio:7.1f}x  "
          f"{format_cost_columns(t)}")

    if args.dry_run:
        print("\n⚠️  DRY RUN — no files modified. Run with --apply to execute.")
    else:
        print("\n✅ Backfill applied. Backups saved as *.jsonl.bak")


if __name__ == "__main__":
    main()
