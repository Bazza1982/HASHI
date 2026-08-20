from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.platform


def test_linux_remote_supervisor_script_is_valid_bash():
    script = ROOT / "bin" / "hashi-remote-ctl.sh"

    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
