from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def build_maturity_report(root_dir: Path) -> dict[str, Any]:
    bundle = json.loads((root_dir / "state" / "live_bundle.json").read_text(encoding="utf-8"))
    acceptance = json.loads((root_dir / "state" / "acceptance_summary.json").read_text(encoding="utf-8"))

    if bundle.get("ready_for_live_probe") and bundle.get("probe_status") == "dry_run":
        stage = "pre_live_ready"
    elif acceptance.get("promotable_to_live_acceptance"):
        stage = "isolated_verified"
    else:
        stage = "isolated_incomplete"

    return {
        "root_dir": str(root_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "fully_working": False,
        "ready_for_live_probe": bool(bundle.get("ready_for_live_probe")),
        "probe_status": bundle.get("probe_status"),
        "trace_ok": bool((acceptance.get("trace_summary") or {}).get("trace_ok")),
        "next_actions": [
            "Keep current rollback commit and bundle artifacts intact.",
            "If desired, run only the non-destructive live probe steps.",
            "Do not claim fully working until a real live probe passes.",
        ],
    }


def write_maturity_report(root_dir: Path) -> dict[str, Any]:
    report = build_maturity_report(root_dir)
    path = root_dir / "state" / "maturity_report.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Assess current Option D maturity stage")
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--root", required=True)
    args = parser.parse_args()

    report = write_maturity_report(Path(args.root))
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
