from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_live_readiness_report(root_dir: Path, *, repo_root: Path) -> dict[str, Any]:
    state_dir = root_dir / "state"
    acceptance = _load_json(state_dir / "acceptance_summary.json")
    runbook = _load_json(state_dir / "live_acceptance_runbook.json")
    probe_plan = _load_json(state_dir / "live_probe_plan.json")

    rollback_commit = runbook["rollback_commit"]
    rollback_ok = (
        subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", rollback_commit],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )

    files_ok = all(
        path.exists()
        for path in [
            state_dir / "acceptance_summary.json",
            state_dir / "live_acceptance_runbook.json",
            state_dir / "live_probe_plan.json",
        ]
    )
    benign_ok = probe_plan.get("benign_url") == "https://example.com"
    steps_ok = [step["id"] for step in probe_plan.get("steps", [])] == [
        "healthcheck",
        "ping",
        "active_tab",
        "get_text",
        "screenshot",
    ]

    ready = bool(
        acceptance.get("promotable_to_live_acceptance")
        and rollback_ok
        and files_ok
        and benign_ok
        and steps_ok
    )

    return {
        "root_dir": str(root_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rollback_commit": rollback_commit,
        "checks": {
            "acceptance_promotable": bool(acceptance.get("promotable_to_live_acceptance")),
            "rollback_commit_exists": rollback_ok,
            "required_files_present": files_ok,
            "benign_url_ok": benign_ok,
            "probe_steps_ok": steps_ok,
        },
        "ready_for_live_probe": ready,
    }


def write_live_readiness_report(root_dir: Path, *, repo_root: Path) -> dict[str, Any]:
    report = build_live_readiness_report(root_dir, repo_root=repo_root)
    path = root_dir / "state" / "live_readiness_report.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a readiness report for a non-destructive live probe")
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--repo-root", required=True)
    args = parser.parse_args()

    report = write_live_readiness_report(Path(args.root), repo_root=Path(args.repo_root))
    print(json.dumps(report))
    return 0 if report["ready_for_live_probe"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
