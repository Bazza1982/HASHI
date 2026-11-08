from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys

import pytest

from tests.test_remote_windows_supervisor import _powershell, _ps_string

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.platform,
    pytest.mark.skipif(sys.platform != "win32", reason="Native Windows lifecycle"),
]


@pytest.mark.parametrize("fault", [None, "wrong_instance", "discovery_failed"])
def test_untrusted_neighbors_do_not_disable_healthy_local_remote(tmp_path, fault):
    home = tmp_path / "independent home"
    (home / "remote").mkdir(parents=True)
    (home / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "LOCAL-READY"}}), encoding="utf-8",
    )
    shutil.copyfile(ROOT / "remote/supervisor_identity.py",
                    home / "remote/supervisor_identity.py")
    instance = "FOREIGN" if fault == "wrong_instance" else "LOCAL-READY"
    advertising = "$false" if fault == "discovery_failed" else "$true"
    status = "degraded" if fault == "discovery_failed" else "ready"
    result = _powershell(f"""
$ErrorActionPreference = 'Stop'
function Get-NetFirewallRule {{ @() }}
function Get-NetTCPConnection {{ @() }}
function Invoke-RestMethod {{
    param($Method, $Uri, $TimeoutSec)
    [pscustomobject]@{{
        ok=$true; status='{status}';
        instance=[pscustomobject]@{{instance_id='{instance}'}};
        discovery=[pscustomobject]@{{
            state='rejected'; readiness='ready'; peer_count=4;
            trusted_peer_count=0; trust_state='rejected';
            backends=@([pscustomobject]@{{advertising={advertising}; browsing=$true}})
        }}
    }}
}}
& {_ps_string(ROOT / 'bin/hashi_remote_ctl.ps1')} doctor -HashiRoot {_ps_string(home)} -Python {_ps_string(sys.executable)} -NoTls
""")
    if fault:
        assert result.returncode != 0, result.stdout
    else:
        assert result.returncode == 0, result.stderr
        assert "local_ready_untrusted_peers" in result.stdout
        assert "0 trusted" in result.stdout
# HASHI-Fingerprint: hfp_9d8c3f8a8f9f4f84b48e82411447e4a9
