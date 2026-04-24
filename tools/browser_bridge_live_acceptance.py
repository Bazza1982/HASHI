from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.browser_bridge_acceptance import write_acceptance_summary


def build_live_acceptance_runbook(root_dir: Path, *, rollback_commit: str) -> dict[str, Any]:
    summary = write_acceptance_summary(root_dir)
    if not summary["promotable_to_live_acceptance"]:
        raise ValueError(f"isolated acceptance is not promotable: {summary['blockers']}")

    runbook = {
        "root_dir": str(root_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rollback_commit": rollback_commit,
        "mode": "non_destructive_live_acceptance",
        "preconditions": [
            "Do not use personal data pages or destructive actions.",
            "Confirm the known-good rollback commit exists locally.",
            "Only test benign pages such as about:blank or https://example.com.",
            "Stop immediately if the live bridge shows unexpected navigation or errors.",
        ],
        "steps": [
            "Verify the installed live extension path and native host manifest are unchanged before testing.",
            "Open a benign browser tab and avoid logged-in or sensitive pages.",
            "Run healthcheck and ping only after confirming the browser is idle.",
            "Run active_tab, get_text, and screenshot against the benign page only.",
            "Record results and compare them with the isolated acceptance summary before any broader test.",
        ],
        "success_criteria": [
            "healthcheck passes",
            "ping passes",
            "active_tab passes",
            "get_text passes",
            "screenshot passes",
            "No unexpected tab switches or navigation occur",
        ],
        "abort_conditions": [
            "Any failed live smoke step",
            "Unexpected navigation away from the benign target page",
            "Any sign that a sensitive session or page is being touched",
        ],
    }
    return runbook


def write_live_acceptance_runbook(root_dir: Path, *, rollback_commit: str) -> dict[str, Any]:
    runbook = build_live_acceptance_runbook(root_dir, rollback_commit=rollback_commit)
    path = root_dir / "state" / "live_acceptance_runbook.json"
    path.write_text(json.dumps(runbook, indent=2) + "\n", encoding="utf-8")
    return runbook


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a conservative live acceptance runbook for Option D")
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--rollback-commit", required=True)
    args = parser.parse_args()

    runbook = write_live_acceptance_runbook(Path(args.root), rollback_commit=args.rollback_commit)
    print(json.dumps(runbook))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
