from pathlib import Path
import json, sys
import pytest
from tests.test_remote_windows_supervisor import _powershell, _ps_string

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.platform, pytest.mark.skipif(sys.platform != "win32", reason="Native PowerShell binding")]

def test_explicit_runtime_root_overrides_inherited_catalogue_before_controller_work(tmp_path):
    # Run the actual parameter/environment binding, before any process or task access.
    binding = (ROOT / "bin/bridge_ctl.ps1").read_text(encoding="utf-8").split("# Constants", 1)[0]
    selected = tmp_path / "instance runtime with spaces"
    output = tmp_path / "selected.json"
    result = _powershell(f"""
$env:HASHI_DATA_ROOT = 'inherited catalogue'
& {{
{binding}
}} -RuntimeDataRoot {_ps_string(selected)}
@{{root=$env:HASHI_DATA_ROOT}} | ConvertTo-Json | Set-Content -Encoding UTF8 {_ps_string(output)}
""")
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8-sig"))["root"] == str(selected)
# HASHI-Fingerprint: hfp_81fc30997827415d9859db555af3aa7c
