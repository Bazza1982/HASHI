from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def build_handoff_markdown(root_dir: Path) -> str:
    maturity = json.loads((root_dir / "state" / "maturity_report.json").read_text(encoding="utf-8"))
    bundle = json.loads((root_dir / "state" / "live_bundle.json").read_text(encoding="utf-8"))

    lines = [
        "# Browser Bridge Handoff",
        "",
        f"- Stage: `{maturity['stage']}`",
        f"- Fully working: `{maturity['fully_working']}`",
        f"- Ready for live probe: `{maturity['ready_for_live_probe']}`",
        f"- Probe status: `{maturity['probe_status']}`",
        f"- Rollback commit: `{bundle['rollback_commit']}`",
        "",
        "## Artifacts",
        f"- Runbook: `{bundle['artifacts']['runbook']}`",
        f"- Probe plan: `{bundle['artifacts']['probe_plan']}`",
        f"- Readiness report: `{bundle['artifacts']['readiness_report']}`",
        f"- Probe report: `{bundle['artifacts']['probe_report']}`",
        "",
        "## Next Actions",
    ]
    for action in maturity["next_actions"]:
        lines.append(f"- {action}")
    lines.append("")
    lines.append("## Probe Steps")
    for step_id in bundle["probe_step_ids"]:
        lines.append(f"- `{step_id}`")
    lines.append("")
    return "\n".join(lines)


def write_handoff_markdown(root_dir: Path) -> str:
    content = build_handoff_markdown(root_dir)
    path = root_dir / "state" / "handoff_summary.md"
    path.write_text(content, encoding="utf-8")
    return content


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a human-readable Browser Bridge handoff summary")
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--root", required=True)
    args = parser.parse_args()

    content = write_handoff_markdown(Path(args.root))
    print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
