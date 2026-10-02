"""Safety checks for the separate Privacy Level 2 dependency runtime."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROVISIONER = ROOT / "scripts" / "provision_privacy_runtime.py"


def test_privacy_provisioner_refuses_to_target_the_active_core_interpreter():
    result = subprocess.run(
        [sys.executable, str(PROVISIONER), "--runtime-dir", sys.prefix],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "must not overlap" in result.stderr


def test_privacy_readiness_fails_closed_when_the_detector_is_absent(tmp_path):
    result = subprocess.run(
        [sys.executable, str(PROVISIONER), "--runtime-dir", str(tmp_path / "missing"), "--check"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "missing" in result.stderr
    assert not (tmp_path / "missing").exists()
