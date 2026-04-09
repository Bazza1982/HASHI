#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _iter_workspaces(workspaces_root: Path) -> list[Path]:
    if not workspaces_root.exists():
        return []
    return sorted(path for path in workspaces_root.iterdir() if path.is_dir())


def _load_events(workspaces_root: Path, since_days: int | None) -> list[dict[str, Any]]:
    cutoff = None
    if since_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    rows: list[dict[str, Any]] = []
    for workspace in _iter_workspaces(workspaces_root):
        for row in _load_jsonl(workspace / "token_audit.jsonl"):
            ts = _parse_ts(row.get("ts"))
            if cutoff and ts and ts < cutoff:
                continue
            row.setdefault("agent", workspace.name)
            rows.append(row)
    return rows


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return round(values[0], 3)
    return round(statistics.quantiles(values, n=20, method="inclusive")[18], 3)


def _group(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "<none>")].append(row)
    out = []
    for group_key, items in grouped.items():
        input_tokens = sum(int(item.get("input_tokens", 0) or 0) for item in items)
        out.append(
            {
                key: group_key,
                "requests": len(items),
                "input_tokens": input_tokens,
                "output_tokens": sum(int(item.get("output_tokens", 0) or 0) for item in items),
                "thinking_tokens": sum(int(item.get("thinking_tokens", 0) or 0) for item in items),
                "avg_input_tokens": round(input_tokens / max(len(items), 1), 2),
                "p95_input_tokens": _p95([float(item.get("input_tokens", 0) or 0) for item in items]),
                "avg_context_expansion_ratio": _mean([float(item.get("context_expansion_ratio", 0) or 0) for item in items]),
                "tool_call_count": sum(int(item.get("tool_call_count", 0) or 0) for item in items),
                "tool_schema_tokens_est": sum(int(item.get("tool_schema_tokens_est", 0) or 0) for item in items),
            }
        )
    return sorted(out, key=lambda row: row["input_tokens"], reverse=True)


def _section_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, float]] = defaultdict(lambda: {"chars": 0, "tokens": 0, "events": 0})
    for row in rows:
        section_chars = row.get("section_chars") or {}
        section_tokens = row.get("section_tokens_est") or {}
        for key, value in section_chars.items():
            totals[key]["chars"] += int(value or 0)
            totals[key]["tokens"] += int(section_tokens.get(key, 0) or 0)
            totals[key]["events"] += 1
    return sorted(
        [
            {
                "section": key,
                "chars": int(value["chars"]),
                "tokens_est": int(value["tokens"]),
                "events": int(value["events"]),
                "avg_tokens_est": round(value["tokens"] / max(value["events"], 1), 2),
            }
            for key, value in totals.items()
        ],
        key=lambda row: row["tokens_est"],
        reverse=True,
    )


def _top(rows: list[dict[str, Any]], key: str, limit: int) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: row.get(key, 0), reverse=True)[:limit]


def _findings(rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    repeated: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        ctx_fp = row.get("context_fingerprint") or ""
        schema_fp = row.get("tool_schema_fingerprint") or ""
        if ctx_fp:
            composite_key = f"{ctx_fp}:{schema_fp}" if schema_fp else ctx_fp
            repeated[composite_key].append(row)

    repeated_contexts = []
    for composite_key, items in repeated.items():
        if len(items) < 3:
            continue
        input_tokens = [float(item.get("input_tokens", 0) or 0) for item in items]
        repeated_contexts.append(
            {
                "context_fingerprint": composite_key,
                "requests": len(items),
                "agents": sorted({str(item.get("agent")) for item in items}),
                "sources": sorted({str(item.get("source")) for item in items}),
                "avg_context_expansion_ratio": _mean(
                    [float(item.get("context_expansion_ratio", 0) or 0) for item in items]
                ),
                "input_tokens_stdev": round(statistics.stdev(input_tokens), 1) if len(input_tokens) > 1 else 0.0,
            }
        )
    repeated_contexts.sort(key=lambda row: (row["requests"], row["avg_context_expansion_ratio"]), reverse=True)

    tool_schema_bloat = []
    for row in rows:
        schema_tokens = int(row.get("tool_schema_tokens_est", 0) or 0)
        if schema_tokens < 200:
            continue
        if int(row.get("tool_call_count", 0) or 0) > 0:
            continue
        tool_schema_bloat.append(
            {
                "agent": row.get("agent"),
                "request_id": row.get("request_id"),
                "backend": row.get("backend"),
                "model": row.get("model"),
                "source": row.get("source"),
                "tool_catalog_count": row.get("tool_catalog_count"),
                "tool_schema_tokens_est": schema_tokens,
                "input_tokens": row.get("input_tokens"),
            }
        )
    tool_schema_bloat = sorted(tool_schema_bloat, key=lambda row: row["tool_schema_tokens_est"], reverse=True)[:limit]

    return {
        "highest_input_requests": _top(rows, "input_tokens", limit),
        "context_expansion_hotspots": _top(rows, "context_expansion_ratio", limit),
        "repeated_contexts": repeated_contexts[:limit],
        "tool_schema_bloat": tool_schema_bloat,
    }


def build_report(rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    input_tokens = sum(int(row.get("input_tokens", 0) or 0) for row in rows)
    output_tokens = sum(int(row.get("output_tokens", 0) or 0) for row in rows)
    thinking_tokens = sum(int(row.get("thinking_tokens", 0) or 0) for row in rows)
    return {
        "summary": {
            "requests": len(rows),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "thinking_tokens": thinking_tokens,
            "avg_input_tokens": round(input_tokens / max(len(rows), 1), 2),
            "p95_input_tokens": _p95([float(row.get("input_tokens", 0) or 0) for row in rows]),
            "avg_context_expansion_ratio": _mean([float(row.get("context_expansion_ratio", 0) or 0) for row in rows]),
            "tool_call_count": sum(int(row.get("tool_call_count", 0) or 0) for row in rows),
            "token_sources": dict(Counter(str(row.get("token_source") or "unknown") for row in rows)),
        },
        "by_agent": _group(rows, "agent")[:limit],
        "by_backend": _group(rows, "backend")[:limit],
        "by_model": _group(rows, "model")[:limit],
        "by_source": _group(rows, "source")[:limit],
        "section_summary": _section_summary(rows)[:limit],
        "findings": _findings(rows, limit),
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = ["# Token Audit Report", ""]
    summary = report["summary"]
    lines.append(
        f"Requests: {summary['requests']} | Input: {summary['input_tokens']} | "
        f"Output: {summary['output_tokens']} | Thinking: {summary['thinking_tokens']}"
    )
    lines.append(
        f"Avg input/request: {summary['avg_input_tokens']} | "
        f"P95 input/request: {summary['p95_input_tokens']} | "
        f"Avg expansion ratio: {summary['avg_context_expansion_ratio']}"
    )
    lines.append("")
    lines.append("## Top Agents")
    for row in report["by_agent"][:10]:
        lines.append(
            f"- {row['agent']}: requests={row['requests']} input={row['input_tokens']} "
            f"avg_input={row['avg_input_tokens']} expansion={row['avg_context_expansion_ratio']}"
        )
    lines.append("")
    lines.append("## Section Summary")
    for row in report["section_summary"][:10]:
        lines.append(
            f"- {row['section']}: total_tokens_est={row['tokens_est']} avg_tokens_est={row['avg_tokens_est']}"
        )
    lines.append("")
    lines.append("## Findings")
    for row in report["findings"]["tool_schema_bloat"][:10]:
        lines.append(
            f"- tool-bloat {row['agent']} {row['request_id']}: schema_tokens_est={row['tool_schema_tokens_est']} input={row['input_tokens']}"
        )
    for row in report["findings"]["repeated_contexts"][:10]:
        lines.append(
            f"- repeated-context {row['context_fingerprint']}: requests={row['requests']} expansion={row['avg_context_expansion_ratio']}"
        )
    for row in report["findings"]["context_expansion_hotspots"][:10]:
        lines.append(
            f"- expansion-hotspot {row.get('agent')} {row.get('request_id')}: ratio={row.get('context_expansion_ratio')} input={row.get('input_tokens')}"
        )
    return "\n".join(lines) + "\n"


def cmd_report(args: argparse.Namespace) -> int:
    rows = _load_events(args.workspaces_root, args.since_days)
    report = build_report(rows, args.limit)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.md_out:
        args.md_out.parent.mkdir(parents=True, exist_ok=True)
        args.md_out.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit HASHI token usage and prompt overhead.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    report = sub.add_parser("report")
    report.add_argument("--workspaces-root", type=Path, default=PROJECT_ROOT / "workspaces")
    report.add_argument("--since-days", type=int, default=None)
    report.add_argument("--limit", type=int, default=15)
    report.add_argument("--json-out", type=Path, default=None)
    report.add_argument("--md-out", type=Path, default=None)
    report.set_defaults(func=cmd_report)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
