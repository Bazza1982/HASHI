from __future__ import annotations

import json
from pathlib import Path

from tools.browser_bridge_maturity import (
    build_maturity_report,
    write_maturity_report,
)


def _write_inputs(root: Path) -> None:
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "live_bundle.json").write_text(
        json.dumps(
            {
                "ready_for_live_probe": True,
                "probe_status": "dry_run",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (state / "acceptance_summary.json").write_text(
        json.dumps(
            {
                "promotable_to_live_acceptance": True,
                "trace_summary": {"trace_ok": True},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_build_maturity_report(tmp_path: Path) -> None:
    _write_inputs(tmp_path)
    report = build_maturity_report(tmp_path)
    assert report["stage"] == "pre_live_ready"
    assert report["fully_working"] is False


def test_write_maturity_report(tmp_path: Path) -> None:
    _write_inputs(tmp_path)
    report = write_maturity_report(tmp_path)
    saved = json.loads((tmp_path / "state" / "maturity_report.json").read_text(encoding="utf-8"))
    assert saved == report
