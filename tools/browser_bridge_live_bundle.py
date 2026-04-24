from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.browser_bridge_live_acceptance import write_live_acceptance_runbook
from tools.browser_bridge_live_executor import execute_live_probe_plan
from tools.browser_bridge_live_probe import write_live_probe_plan
from tools.browser_bridge_live_readiness import write_live_readiness_report


def build_live_bundle(
    root_dir: Path,
    *,
    repo_root: Path,
    rollback_commit: str,
) -> dict[str, Any]:
    runbook = write_live_acceptance_runbook(root_dir, rollback_commit=rollback_commit)
    probe_plan = write_live_probe_plan(root_dir, rollback_commit=rollback_commit)
    readiness = write_live_readiness_report(root_dir, repo_root=repo_root)
    probe_report = execute_live_probe_plan(root_dir, dry_run=True)

    bundle = {
        "root_dir": str(root_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rollback_commit": rollback_commit,
        "artifacts": {
            "runbook": str(root_dir / "state" / "live_acceptance_runbook.json"),
            "probe_plan": str(root_dir / "state" / "live_probe_plan.json"),
            "readiness_report": str(root_dir / "state" / "live_readiness_report.json"),
            "probe_report": str(root_dir / "state" / "live_probe_report.json"),
        },
        "ready_for_live_probe": readiness["ready_for_live_probe"],
        "probe_status": probe_report["status"],
        "probe_step_ids": [item["id"] for item in probe_report["results"]],
        "mode": runbook["mode"],
    }
    return bundle


def write_live_bundle(
    root_dir: Path,
    *,
    repo_root: Path,
    rollback_commit: str,
) -> dict[str, Any]:
    bundle = build_live_bundle(root_dir, repo_root=repo_root, rollback_commit=rollback_commit)
    path = root_dir / "state" / "live_bundle.json"
    path.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the full pre-live Option D evidence bundle")
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--rollback-commit", required=True)
    args = parser.parse_args()

    bundle = write_live_bundle(
        Path(args.root),
        repo_root=Path(args.repo_root),
        rollback_commit=args.rollback_commit,
    )
    print(json.dumps(bundle))
    return 0 if bundle["ready_for_live_probe"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
