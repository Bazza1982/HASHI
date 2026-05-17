#!/usr/bin/env python3
"""Run a short memory+ auto-debug test against a HASHI Workbench agent."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parent.parent


def _json_post(url: str, payload: dict, *, timeout_s: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Connection failed for {url}: {exc}") from exc


def _json_get(url: str, *, timeout_s: int) -> dict:
    try:
        with urllib_request.urlopen(url, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Connection failed for {url}: {exc}") from exc


def _read_jsonl_tail(path: Path, start_size: int) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_bytes()
    if start_size > 0:
        raw = raw[start_size:]
    rows = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"parse_error": True, "raw": line})
    return rows


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _contains_any(text: str, values: list[str]) -> bool:
    lowered = text.lower()
    return any(value.lower() in lowered for value in values if value)


def _excerpt(text: str, limit: int = 500) -> str:
    compact = " ".join(text.split())
    return compact[:limit]


def _thinking_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        if row.get("role") != "thinking":
            continue
        text = str(row.get("text") or "")
        out.append(
            {
                "ts": row.get("ts"),
                "request_id": row.get("request_id"),
                "chars": len(text),
                "excerpt": _excerpt(text),
            }
        )
    return out


def _send_round(base_url: str, agent: str, text: str, *, timeout_s: int) -> dict:
    print(f"\n--- sending round to {agent} ---")
    print(text)
    result = _json_post(
        f"{base_url}/api/browser/chat/send",
        {"agent": agent, "text": text, "source": "memory-plus-auto-debug", "timeout_s": timeout_s},
        timeout_s=timeout_s + 10,
    )
    print(f"ok={result.get('ok')} request_id={result.get('request_id')} status={result.get('status')}")
    response = str(result.get("text") or "")
    print(f"response_preview={response[:240].replace(chr(10), ' ')}")
    return result


def _command(base_url: str, agent: str, command: str, *, timeout_s: int) -> dict:
    print(f"\n--- command {agent}: {command} ---")
    result = _json_post(
        f"{base_url}/api/agents/{agent}/command",
        {"command": command},
        timeout_s=timeout_s,
    )
    print(json.dumps(result, ensure_ascii=False)[:800])
    return result


def run(args: argparse.Namespace) -> int:
    base_url = args.base_url.rstrip("/")
    workspace = ROOT / "workspaces" / args.agent
    memory_dir = workspace / "memory"
    notepad_path = memory_dir / "memory_plus_notepad.md"
    diagnostics_path = memory_dir / "memory_plus_diagnostics.jsonl"
    transcript_path = workspace / "transcript.jsonl"
    report_dir = ROOT / "workspaces" / "lily" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"memory_plus_auto_debug_{args.agent}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    print("memory+ auto-debug runner")
    print(f"mode={args.mode}")
    print(f"root={ROOT}")
    print(f"agent={args.agent}")
    print(f"base_url={base_url}")
    print(f"workspace={workspace}")
    print(f"notepad={notepad_path}")
    print(f"diagnostics={diagnostics_path}")
    print(f"transcript={transcript_path}")
    print(f"dry_run={args.dry_run}")

    health = _json_get(f"{base_url}/api/health", timeout_s=5)
    if args.agent not in health.get("agents", []):
        raise RuntimeError(f"agent {args.agent!r} not listed in health agents: {health.get('agents')}")
    print(f"health_ok={health.get('ok')} instance={health.get('instance_id')}")

    if args.dry_run:
        print("dry-run complete; no messages sent.")
        return 0

    start_diag_size = _file_size(diagnostics_path)
    start_transcript_size = _file_size(transcript_path)
    start_notepad = notepad_path.read_text(encoding="utf-8", errors="replace") if notepad_path.exists() else ""

    label = args.label
    shelf = args.shelf
    rounds = [
        (
            "seed",
            f"Small household preference before the list: Dad usually prefers `{label}` for family dinners, "
            f"and he dislikes `{shelf}`. Could you make a practical grocery category list for a small family dinner?",
        ),
        (
            "distractor-1",
            "Please make a simple grocery category list for a family dinner. Keep it practical.",
        ),
        (
            "distractor-2",
            "Suggest a calm 20-minute evening routine after a busy workday.",
        ),
        (
            "distractor-3",
            "Draft a friendly message asking whether an item is ready for pickup.",
        ),
        (
            "final-probe",
            "I am planning another family dinner now. What rice should I buy for Dad, and which vegetable should I avoid? "
            "If you cannot identify them from your continuity, say you are not sure.",
        ),
    ]

    results = []
    if args.reset_before_seed:
        reset_before = _command(base_url, args.agent, "/new", timeout_s=30)
        results.append({"kind": "command-new-before-seed", **reset_before})
        time.sleep(args.reset_wait_s)

    results.append({"kind": rounds[0][0], **_send_round(base_url, args.agent, rounds[0][1], timeout_s=args.timeout_s)})

    if args.reset_after_seed:
        reset_result = _command(base_url, args.agent, "/new", timeout_s=30)
        results.append({"kind": "command-new", **reset_result})
        time.sleep(args.reset_wait_s)

    for kind, text in rounds[1:]:
        results.append({"kind": kind, **_send_round(base_url, args.agent, text, timeout_s=args.timeout_s)})

    diagnostics = _read_jsonl_tail(diagnostics_path, start_diag_size)
    transcript_delta_rows = _read_jsonl_tail(transcript_path, start_transcript_size)
    thinking_delta = _thinking_rows(transcript_delta_rows)
    final_text = str(results[-1].get("text") or "")
    notepad_after = notepad_path.read_text(encoding="utf-8", errors="replace") if notepad_path.exists() else ""
    notepad_delta = notepad_after[len(start_notepad) :] if notepad_after.startswith(start_notepad) else notepad_after

    label_values = [label, *args.label_alias]
    shelf_values = [shelf, *args.shelf_alias]
    final_has_label = _contains_any(final_text, label_values)
    final_has_shelf = _contains_any(final_text, shelf_values)
    final_denial = any(
        marker in final_text.lower()
        for marker in (
            "not sure",
            "unsure",
            "cannot identify",
            "can't identify",
            "没有",
            "不确定",
            "不能确定",
            "无法确认",
            "无法识别",
            "没有任何",
        )
    )
    notepad_has_label = _contains_any(notepad_after, label_values)
    notepad_has_shelf = _contains_any(notepad_after, shelf_values)
    hidden_leak = "<memory_plus_update>" in final_text or "</memory_plus_update>" in final_text
    written_count = sum(1 for row in diagnostics if row.get("reason") == "written")
    block_missing_count = sum(1 for row in diagnostics if row.get("reason") == "block_missing")

    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent": args.agent,
        "base_url": base_url,
        "rounds_total": 5,
        "reset_after_seed": args.reset_after_seed,
        "label": label,
        "shelf": shelf,
        "request_ids": [row.get("request_id") for row in results if row.get("request_id")],
        "final_has_label": final_has_label,
        "final_has_shelf": final_has_shelf,
        "final_denial": final_denial,
        "notepad_has_label": notepad_has_label,
        "notepad_has_shelf": notepad_has_shelf,
        "hidden_leak": hidden_leak,
        "diagnostics_rows": len(diagnostics),
        "diagnostics_written_count": written_count,
        "diagnostics_block_missing_count": block_missing_count,
        "diagnostics_reasons": [row.get("reason") for row in diagnostics],
        "thinking_rows": len(thinking_delta),
        "thinking_chars": sum(row.get("chars", 0) for row in thinking_delta),
        "pass": final_has_label and final_has_shelf and not final_denial and notepad_has_label and notepad_has_shelf and not hidden_leak,
        "final_text": final_text,
        "notepad_delta": notepad_delta,
        "diagnostics": diagnostics,
        "thinking_delta": thinking_delta,
        "results": results,
    }
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n--- summary ---")
    print(json.dumps({k: summary[k] for k in (
        "pass",
        "final_has_label",
        "final_has_shelf",
        "final_denial",
        "notepad_has_label",
        "notepad_has_shelf",
        "hidden_leak",
        "diagnostics_rows",
        "diagnostics_written_count",
        "diagnostics_block_missing_count",
        "diagnostics_reasons",
        "thinking_rows",
        "thinking_chars",
    )}, ensure_ascii=False, indent=2))
    print(f"report={report_path}")
    return 0 if summary["pass"] else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a 5-round memory+ auto-debug test.")
    parser.add_argument("--agent", default="akane")
    parser.add_argument("--base-url", default="http://10.255.255.254:18800")
    parser.add_argument("--mode", default="run", choices=["check", "run"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout-s", type=int, default=240)
    parser.add_argument("--reset-wait-s", type=int, default=5)
    parser.add_argument("--no-reset-before-seed", dest="reset_before_seed", action="store_false")
    parser.add_argument("--no-reset-after-seed", dest="reset_after_seed", action="store_false")
    parser.set_defaults(reset_before_seed=True, reset_after_seed=True)
    parser.add_argument("--label", default="jasmine rice")
    parser.add_argument("--shelf", default="eggplant")
    parser.add_argument("--label-alias", action="append", default=["茉莉香米"])
    parser.add_argument("--shelf-alias", action="append", default=["茄子"])
    args = parser.parse_args()
    if args.mode == "check":
        args.dry_run = True
    try:
        return run(args)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
