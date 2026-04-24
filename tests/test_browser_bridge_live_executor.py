from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.browser_bridge_live_executor import execute_live_probe_plan


def _write_probe_plan(root: Path) -> None:
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    payload = {
        "steps": [
            {"id": "healthcheck", "argv": ["python3", "-c", "print('ok')"]},
            {"id": "ping", "argv": ["python3", "-c", "print('pong')"]},
        ]
    }
    (state / "live_probe_plan.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_execute_live_probe_plan_dry_run(tmp_path: Path) -> None:
    _write_probe_plan(tmp_path)
    report = execute_live_probe_plan(tmp_path, dry_run=True)
    assert report["status"] == "dry_run"
    assert report["results"][0]["status"] == "planned"


def test_execute_live_probe_plan_real_run(tmp_path: Path) -> None:
    _write_probe_plan(tmp_path)
    report = execute_live_probe_plan(tmp_path, dry_run=False, confirm_live=True, runner=subprocess.run)
    assert report["status"] == "passed"
    assert [item["status"] for item in report["results"]] == ["passed", "passed"]
    saved = json.loads((tmp_path / "state" / "live_probe_report.json").read_text(encoding="utf-8"))
    assert saved == report


def test_execute_live_probe_plan_requires_confirmation(tmp_path: Path) -> None:
    _write_probe_plan(tmp_path)
    with pytest.raises(ValueError):
        execute_live_probe_plan(tmp_path, dry_run=False, runner=subprocess.run)
