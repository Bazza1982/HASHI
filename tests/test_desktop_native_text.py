"""Compile the real Windows host and exercise a hidden, in-process RichEdit.

No foreground window, system keystroke, mouse or clipboard operation is used.
"""
from pathlib import Path
import json
import os
import subprocess

import pytest

pytestmark = [pytest.mark.platform, pytest.mark.skipif(os.name != "nt", reason="native Windows adapter")]


def test_native_host_replaces_one_selection_without_packet_replay(tmp_path):
    root = Path(__file__).resolve().parents[1]
    framework = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319"
    compiler = framework / "csc.exe"
    module = Path(os.environ["ProgramFiles"]) / "Microsoft Office/root/vfs/ProgramFilesCommonX64/Microsoft Shared/OFFICE16/RICHED20.DLL"
    if not module.is_file():
        pytest.skip("qualified RichEditD2DPT test component is not installed")
    host = tmp_path / "Host.dll"
    probe = tmp_path / "NativeTextRegression.exe"
    commands = [
        [str(compiler), "/nologo", "/target:library", "/platform:x64",
         "/reference:System.ServiceProcess.dll", "/reference:System.Drawing.dll",
         "/reference:System.Web.Extensions.dll", "/out:" + str(host),
         str(root / "tools/windows_helper/secure_desktop_host.cs")],
        [str(compiler), "/nologo", "/target:exe", "/platform:x64",
         "/reference:System.Web.Extensions.dll", "/out:" + str(probe),
         str(root / "tests/native_desktop_text_probe.cs")],
    ]
    for command in commands:
        built = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert built.returncode == 0, built.stdout + built.stderr
    result = subprocess.run([str(probe), str(host), str(module)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["ok"] and receipt["visible_windows"] == 0
    assert receipt["timeout_not_replayed"] and receipt["oversize_rejected"]
# HASHI-Fingerprint: hfp_580d7783dbf84d50b2216b06e4ce349a
