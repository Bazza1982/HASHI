from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_workbench_backend_uses_stable_wsl_bridge_host_when_available():
    script = r"""
const os = require('node:os');
os.networkInterfaces = () => ({ stable: [{ address: '10.255.255.254' }] });
delete process.env.BRIDGE_U_API;
delete process.env.HASHI_BRIDGE_API_HOST;
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
    assert result.stdout == "http://10.255.255.254:18842"
