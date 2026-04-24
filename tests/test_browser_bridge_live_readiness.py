from __future__ import annotations

import json
from pathlib import Path

from tools.browser_bridge_live_readiness import (
    build_live_readiness_report,
    write_live_readiness_report,
)


def _write_inputs(root: Path, rollback_commit: str) -> None:
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "acceptance_summary.json").write_text(
        json.dumps({"promotable_to_live_acceptance": True}, indent=2) + "\n",
        encoding="utf-8",
    )
    (state / "live_acceptance_runbook.json").write_text(
        json.dumps({"rollback_commit": rollback_commit}, indent=2) + "\n",
        encoding="utf-8",
    )
    (state / "live_probe_plan.json").write_text(
        json.dumps(
            {
                "benign_url": "https://example.com",
                "steps": [
                    {"id": "healthcheck"},
                    {"id": "ping"},
                    {"id": "active_tab"},
                    {"id": "get_text"},
                    {"id": "screenshot"},
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_build_live_readiness_report(tmp_path: Path) -> None:
    _write_inputs(tmp_path, "HEAD")
    report = build_live_readiness_report(tmp_path, repo_root=Path("/home/lily/projects/hashi"))
    assert report["checks"]["acceptance_promotable"] is True
    assert report["checks"]["rollback_commit_exists"] is True
    assert report["ready_for_live_probe"] is True


def test_write_live_readiness_report(tmp_path: Path) -> None:
    _write_inputs(tmp_path, "HEAD")
    report = write_live_readiness_report(tmp_path, repo_root=Path("/home/lily/projects/hashi"))
    saved = json.loads((tmp_path / "state" / "live_readiness_report.json").read_text(encoding="utf-8"))
    assert saved == report
