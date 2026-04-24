from __future__ import annotations

import json
from pathlib import Path

from tools.browser_bridge_handoff import (
    build_handoff_markdown,
    write_handoff_markdown,
)


def _write_inputs(root: Path) -> None:
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "maturity_report.json").write_text(
        json.dumps(
            {
                "stage": "pre_live_ready",
                "fully_working": False,
                "ready_for_live_probe": True,
                "probe_status": "dry_run",
                "next_actions": ["Keep rollback commit intact."],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (state / "live_bundle.json").write_text(
        json.dumps(
            {
                "rollback_commit": "deadbeef",
                "artifacts": {
                    "runbook": "/tmp/runbook.json",
                    "probe_plan": "/tmp/plan.json",
                    "readiness_report": "/tmp/readiness.json",
                    "probe_report": "/tmp/report.json",
                },
                "probe_step_ids": ["healthcheck", "ping"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_build_handoff_markdown(tmp_path: Path) -> None:
    _write_inputs(tmp_path)
    content = build_handoff_markdown(tmp_path)
    assert "Stage: `pre_live_ready`" in content
    assert "Rollback commit: `deadbeef`" in content
    assert "- `healthcheck`" in content


def test_write_handoff_markdown(tmp_path: Path) -> None:
    _write_inputs(tmp_path)
    content = write_handoff_markdown(tmp_path)
    saved = (tmp_path / "state" / "handoff_summary.md").read_text(encoding="utf-8")
    assert saved == content
