from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_workbench_backend_uses_explicit_non_default_endpoint_without_host_constants():
    script = r"""
delete process.env.BRIDGE_U_API;
process.env.HASHI_BRIDGE_API_HOST = '172.29.144.7';
process.env.HASHI_BRIDGE_API_PORT = '18842';
const config = require('./workbench/ecosystem.config.cjs');
process.stdout.write(config.apps[0].env.BRIDGE_U_API);
"""
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "http://172.29.144.7:18842"
