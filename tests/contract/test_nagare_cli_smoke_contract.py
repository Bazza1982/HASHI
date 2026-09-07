from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SMOKE_FIXTURE = ROOT / "tests" / "fixtures" / "smoke_test.yaml"


def test_nagare_cli_smoke_handler_executes_fixture_end_to_end(tmp_path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    runs_root = tmp_path / "runs"
    output_path = tmp_path / "cli-smoke-result.json"
    run_id_prefix = "run-"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "nagare.cli",
            "run",
            str(SMOKE_FIXTURE),
            "--yes",
            "--silent",
            "--smoke-handler",
            "--runs-root",
            str(runs_root),
            "--repo-root",
            str(ROOT),
            "--output",
            str(output_path),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["success"] is True
    assert set(payload["completed_steps"]) == {"step_write", "step_check"}
    assert payload["run_id"].startswith(run_id_prefix)

    run_dir = runs_root / payload["run_id"]
    quote_path = run_dir / "deterministic-workers" / "writer_01" / "output.txt"
    review_path = run_dir / "deterministic-workers" / "checker_01" / "review.txt"
    assert quote_path.exists()
    assert review_path.exists()
    assert "deterministic-smoke" in quote_path.read_text(encoding="utf-8")
