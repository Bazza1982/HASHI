from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNS_ROOT = ROOT / "flow" / "runs"
SMOKE_FIXTURE = ROOT / "tests" / "fixtures" / "smoke_test.yaml"


def test_nagare_cli_smoke_handler_executes_fixture_end_to_end(monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    output_path = ROOT / "flow" / "runs" / "cli-smoke-result.json"
    run_id_prefix = "run-"
    if output_path.exists():
        output_path.unlink()

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

    run_dir = RUNS_ROOT / payload["run_id"]
    quote_path = run_dir / "deterministic-workers" / "writer_01" / "output.txt"
    review_path = run_dir / "deterministic-workers" / "checker_01" / "review.txt"
    assert quote_path.exists()
    assert review_path.exists()
    assert "deterministic-smoke" in quote_path.read_text(encoding="utf-8")

    shutil.rmtree(run_dir, ignore_errors=True)
    output_path.unlink(missing_ok=True)
