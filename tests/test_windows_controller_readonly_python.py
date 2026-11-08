from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import venv

import pytest

from tests.test_remote_windows_supervisor import _powershell, _ps_string


ROOT = Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.platform,
    pytest.mark.skipif(sys.platform != "win32", reason="Native fixed Windows controller"),
]


def test_controller_identity_checks_never_write_interpreter_bytecode(tmp_path):
    code = tmp_path / "program"
    (code / "bin").mkdir(parents=True)
    (code / "scripts").mkdir()
    venv.EnvBuilder(with_pip=False).create(code / ".venv")
    shutil.copyfile(ROOT / "bin/bridge_ctl.ps1", code / "bin/bridge_ctl.ps1")
    home = tmp_path / "private home"
    home.mkdir()
    (home / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "READONLY-CHECK", "workbench_port": 19978}, "agents": []}),
        encoding="utf-8",
    )
    values = {"lock-path": str(home / "runtime.lock"),
              "pid-path": str(home / "runtime.pid"), "instance-id": "READONLY-CHECK"}
    (code / "scripts/resolve_instance_runtime.py").write_text(
        "import sys\n"
        "if not sys.dont_write_bytecode: raise RuntimeError('Interpreter file closure must stay readonly')\n"
        f"print({values!r}[sys.argv[-1]])\n",
        encoding="utf-8",
    )
    result = _powershell(f"""
function Get-CimInstance {{ @() }}
& {_ps_string(code / 'bin/bridge_ctl.ps1')} -Action status -BridgeHome {_ps_string(home)}
""")
    assert result.returncode == 0, result.stderr
    assert "Interpreter file closure must stay readonly" not in result.stderr
# HASHI-Fingerprint: hfp_22ad718db93047bf84a5985303b0a43b
