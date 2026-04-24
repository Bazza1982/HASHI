from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def load_smoke_results(root_dir: Path) -> dict[str, Any]:
    path = root_dir / "state" / "smoke_results.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_stub_trace(root_dir: Path) -> list[dict[str, Any]]:
    path = root_dir / "logs" / "stub_bridge_trace.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def summarize_stub_trace(root_dir: Path) -> dict[str, Any] | None:
    events = load_stub_trace(root_dir)
    if not events:
        return None
    request_actions = [event["action"] for event in events if event.get("event") == "request"]
    expected_actions = ["ping", "ping", "active_tab", "get_text", "screenshot"]
    trace_ok = (
        events[0].get("event") == "server_started"
        and events[-1].get("event") == "server_stopped"
        and request_actions == expected_actions
    )
    return {
        "event_count": len(events),
        "request_actions": request_actions,
        "trace_ok": trace_ok,
        "expected_request_actions": expected_actions,
    }


def summarize_smoke_results(root_dir: Path) -> dict[str, Any]:
    results = load_smoke_results(root_dir)
    steps = results.get("results", [])
    counts = Counter(step.get("status", "unknown") for step in steps)
    failed_steps = [step["id"] for step in steps if step.get("status") == "failed"]
    manual_steps = [step["id"] for step in steps if step.get("status") == "manual_required"]
    passed_steps = [step["id"] for step in steps if step.get("status") == "passed"]
    trace_summary = summarize_stub_trace(root_dir)

    non_manual_steps = [step for step in steps if step.get("status") != "manual_required"]
    promotable = bool(non_manual_steps) and all(step.get("status") == "passed" for step in non_manual_steps)
    if trace_summary is not None:
        promotable = promotable and trace_summary["trace_ok"]

    return {
        "root_dir": str(root_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_status": results.get("status"),
        "counts": dict(counts),
        "passed_steps": passed_steps,
        "manual_required_steps": manual_steps,
        "failed_steps": failed_steps,
        "trace_summary": trace_summary,
        "promotable_to_live_acceptance": promotable and not failed_steps,
        "blockers": failed_steps,
    }


def write_acceptance_summary(root_dir: Path) -> dict[str, Any]:
    summary = summarize_smoke_results(root_dir)
    path = root_dir / "state" / "acceptance_summary.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize isolated Option D smoke results")
    parser.add_argument("command", choices=["summarize"])
    parser.add_argument("--root", required=True)
    args = parser.parse_args()

    root_dir = Path(args.root)
    summary = write_acceptance_summary(root_dir)
    print(json.dumps(summary))
    return 0 if summary["promotable_to_live_acceptance"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
