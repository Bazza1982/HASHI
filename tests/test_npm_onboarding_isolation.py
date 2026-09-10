from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path


def test_compatibility_onboarding_noninteractive_preserves_existing_data(tmp_path):
    files = {tmp_path/'agents.json':b'{"agents":[]}',tmp_path/'secrets.json':b'{"keep":"secret"}'}
    for path,content in files.items():
        path.write_bytes(content)
    result = subprocess.run([sys.executable,'-m','onboarding.onboarding_main'],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ,'BRIDGE_HOME':str(tmp_path)},stdin=subprocess.DEVNULL,
        capture_output=True,text=True,timeout=10)
    assert result.returncode==78
    assert all(path.read_bytes()==content for path,content in files.items())
    assert not (tmp_path/'workspaces').exists()
