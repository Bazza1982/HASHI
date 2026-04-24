from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def load_live_probe_plan(root_dir: Path) -> dict[str, Any]:
    return json.loads((root_dir / "state" / "live_probe_plan.json").read_text(encoding="utf-8"))


def execute_live_probe_plan(
    root_dir: Path,
    *,
    runner: Any = subprocess.run,
    dry_run: bool = True,
    confirm_live: bool = False,
) -> dict[str, Any]:
    plan = load_live_probe_plan(root_dir)
    if not dry_run and not confirm_live:
        raise ValueError("live probe execution requires explicit confirm_live=True")
    results: list[dict[str, Any]] = []
    overall = "dry_run" if dry_run else "passed"

    for step in plan["steps"]:
        if dry_run:
            results.append(
                {
                    "id": step["id"],
                    "status": "planned",
                    "argv": step["argv"],
                }
            )
            continue

        completed = runner(step["argv"], capture_output=True, text=True)
        status = "passed" if completed.returncode == 0 else "failed"
        results.append(
            {
                "id": step["id"],
                "status": status,
                "argv": step["argv"],
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )
        if status == "failed":
            overall = "failed"
            break

    report = {
        "root_dir": str(root_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "status": overall,
        "results": results,
    }
    path = root_dir / "state" / "live_probe_report.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute or dry-run a conservative live probe plan")
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()

    report = execute_live_probe_plan(
        Path(args.root),
        dry_run=not args.execute,
        confirm_live=args.confirm_live,
    )
    print(json.dumps(report))
    return 0 if report["status"] in {"dry_run", "passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
