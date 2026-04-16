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
import os
import shutil
from datetime import datetime
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent / "workspaces"

# Pricing for cost recalculation (must match token_tracker.py)
# "cached" = per-million price for cached/prompt-cache input tokens.
# Sources: OpenAI API pricing (2026-04), OpenRouter.
PRICING = {
    "gpt-5.4":           {"input": 2.50, "cached": 0.25,  "output": 15.00},
    "gpt-5.3-codex":     {"input": 1.75, "cached": 0.175, "output": 14.00},
    "gpt-5.2-codex":     {"input": 1.75, "cached": 0.175, "output": 14.00},
    "gpt-5.2":           {"input": 1.75, "cached": 0.175, "output": 14.00},
    "gpt-5.1-codex-max": {"input": 1.25, "cached": 0.125, "output": 10.00},
    "gpt-5.1-codex-mini":{"input": 0.25, "cached": 0.025, "output": 2.00},
    "default":           {"input": 2.50, "cached": 0.25,  "output": 15.00},
}


def get_price(model: str) -> dict:
    model_lower = model.lower().strip()
    if model_lower in PRICING:
        return PRICING[model_lower]
    for key, prices in PRICING.items():
        if key in model_lower or model_lower in key:
            return prices
    return PRICING["default"]


def calc_cost_with_cache(input_tokens: int, cached_tokens: int,
                         output_tokens: int, model: str) -> float:
    """Calculate cost with real cached pricing per model."""
    prices = get_price(model)
    non_cached = max(0, input_tokens - cached_tokens)
    cached_price = prices.get("cached", prices["input"] * 0.1)
    cost = (
        non_cached * prices["input"] / 1_000_000
        + cached_tokens * cached_price / 1_000_000
        + output_tokens * prices["output"] / 1_000_000
    )
    return round(cost, 6)


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

        model = rec.get("model", "gpt-5.4")
        old_input = rec.get("input", 0)
        old_output = rec.get("output", 0)
        old_cost = rec.get("cost_usd", 0.0)

        new_cost = calc_cost_with_cache(real_input, real_cached, real_output, model)

        stats["old_input_total"] += old_input
        stats["new_input_total"] += real_input
        stats["old_cost_total"] += old_cost
        stats["new_cost_total"] += new_cost
        stats["matched"] += 1

        # Update the record in place
        rec["input"] = real_input
        rec["output"] = real_output
        rec["cached_input"] = real_cached
        rec["cost_usd"] = new_cost
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
        "old_cost": 0.0, "new_cost": 0.0,
    }

    for agent in agents:
        s = backfill_agent(agent, dry_run=args.dry_run)
        if s["matched"] == 0:
            continue

        ratio = s["new_input_total"] / max(1, s["old_input_total"])
        cost_delta = s["new_cost_total"] - s["old_cost_total"]

        print(f"{s['agent']:12s}  {s['events']:7d}  {s['codex_entries']:6d}  {s['matched']:6d}  {s['skipped']:5d}  "
              f"{s['old_input_total']:12,d}  {s['new_input_total']:12,d}  {ratio:7.1f}x  "
              f"${s['old_cost_total']:9.4f}  ${s['new_cost_total']:9.4f}  ${cost_delta:+9.4f}")

        total_stats["matched"] += s["matched"]
        total_stats["old_input"] += s["old_input_total"]
        total_stats["new_input"] += s["new_input_total"]
        total_stats["old_cost"] += s["old_cost_total"]
        total_stats["new_cost"] += s["new_cost_total"]

    print("─" * 120)
    t = total_stats
    ratio = t["new_input"] / max(1, t["old_input"])
    delta = t["new_cost"] - t["old_cost"]
    print(f"{'TOTAL':12s}  {'':7s}  {'':6s}  {t['matched']:6d}  {'':5s}  "
          f"{t['old_input']:12,d}  {t['new_input']:12,d}  {ratio:7.1f}x  "
          f"${t['old_cost']:9.4f}  ${t['new_cost']:9.4f}  ${delta:+9.4f}")

    if args.dry_run:
        print(f"\n⚠️  DRY RUN — no files modified. Run with --apply to execute.")
    else:
        print(f"\n✅ Backfill applied. Backups saved as *.jsonl.bak")


if __name__ == "__main__":
    main()
