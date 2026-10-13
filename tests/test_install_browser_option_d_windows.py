from __future__ import annotations

from pathlib import Path
import os
import queue
import struct
import subprocess
import sys
import threading

import pytest


def test_native_windows_browser_bridge_installer_is_wsl_independent() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = (repo_root / "tools" / "install_browser_option_d_windows.ps1").read_text(
        encoding="utf-8"
    )

    assert '"HASHI\\browser_bridge"' in script
    assert "OutputType WindowsApplication" in script
    assert "CreateNoWindow = true" in script
    assert "-m tools.browser_native_host --stdio --endpoint" in script
    assert "DEFAULT_WINDOWS_PIPE" in script
    assert "DEFAULT_WINDOWS_AUTH_FILE" in script
    assert "BRIDGE_NAMESPACE" in script
    assert "tools.browser_extension_identity" in script
    assert '"com.hashi.browser_bridge.$HostSuffix"' in script
    assert "$ValidateOnly" in script
    assert "Google\\Chrome\\NativeMessagingHosts" in script
    assert "Microsoft\\Edge\\NativeMessagingHosts" in script
    assert 'ValidateSet("Chrome", "Edge")' in script
    assert '$BridgeEndpoint += "-edge"' in script
    assert "chrome-extension://$ExtensionId/" in script
    assert "wsl.exe" not in script.lower()


def test_device_workers_install_as_windowless_dynamic_endpoint_tasks() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = (repo_root / "scripts" / "install_device_control_workers.ps1").read_text(
        encoding="utf-8"
    )

    assert ".venv\\Scripts\\pythonw.exe" in script
    assert '[string]$BindHost = "auto"' in script
    assert "New-ScheduledTaskTrigger -AtLogOn" in script
    assert "-LogonType Interactive" in script
    assert '"--host", $BindHost' in script
    assert '"--advertise-host", $AdvertiseHost' in script
    assert '[string]$CodeRoot = ""' in script
    assert "$Resolved.ProviderPath" in script
    assert '"--bridge-home", $BridgeHome' in script
    assert "-WorkingDirectory $CodeRoot" in script
    assert '"HASHI\\device_control\\$InstanceId\\logs"' in script
    assert '"--log-dir", $LogDir' in script
    assert '"--port"' not in script
    assert "Stop-ScheduledTask" in script
    assert '"--browser-id", $BrowserId' in script


def test_browser_uninstaller_is_instance_scoped() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = (
        repo_root / "tools" / "uninstall_browser_option_d_windows.ps1"
    ).read_text(encoding="utf-8")

    assert "BRIDGE_NAMESPACE" in script
    assert '"com.hashi.browser_bridge.$HostSuffix"' in script
    assert "StartsWith($ResolvedBase" in script
    assert "Remove-Item -LiteralPath $ResolvedInstallRoot" in script
    assert "Microsoft\\Edge\\NativeMessagingHosts" in script


@pytest.mark.skipif(os.name != "nt", reason="Exercises the real native Windows launcher")
def test_native_launcher_delivers_frames_while_stdin_remains_open(tmp_path) -> None:
    """A native messaging connection is persistent; EOF cannot flush its frames."""
    repo_root = Path(__file__).resolve().parents[1]
    installer = (repo_root / "tools/install_browser_option_d_windows.ps1").read_text(encoding="utf-8")
    launcher_source = installer.split('$LauncherSource = @"', 1)[1].split('\n"@', 1)[0]
    echo_root = tmp_path / "native echo"
    package = echo_root / "tools"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "browser_native_host.py").write_text(
        "import struct, sys\n"
        "while True:\n"
        "    header = sys.stdin.buffer.read(4)\n"
        "    if not header: break\n"
        "    size = struct.unpack('<I', header)[0]\n"
        "    body = sys.stdin.buffer.read(size)\n"
        "    sys.stdout.buffer.write(header + body)\n"
        "    sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    replacements = {
        "$PythonLiteral": sys.executable,
        "$RepoLiteral": str(echo_root),
        "$EndpointLiteral": "native-echo-test",
        "$AuthLiteral": str(tmp_path / "unused-auth"),
        "$LogLiteral": str(tmp_path / "native.log"),
        "$OriginLiteral": "chrome-extension://test/",
    }
    for field, value in replacements.items():
        launcher_source = launcher_source.replace(field, value.replace('"', '""'))
    source_path = tmp_path / "launcher.cs"
    source_path.write_text(launcher_source, encoding="utf-8-sig")
    executable = tmp_path / "native_launcher.exe"
    quoted = lambda value: "'" + str(value).replace("'", "''") + "'"
    compilation = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "Add-Type -TypeDefinition (Get-Content -LiteralPath " + quoted(source_path)
         + " -Raw) -Language CSharp -OutputAssembly " + quoted(executable)
         + " -OutputType WindowsApplication"],
        capture_output=True, text=True, timeout=30,
    )
    assert compilation.returncode == 0, compilation.stderr
    process = subprocess.Popen([str(executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    received = queue.Queue()
    def read_replies():
        for _ in range(2):
            header = process.stdout.read(4)
            received.put(process.stdout.read(struct.unpack('<I', header)[0]) if len(header) == 4 else None)
    reader = threading.Thread(target=read_replies, daemon=True)
    reader.start()
    try:
        for body in (b'{"type":"hello"}', b"next persistent message"):
            frame = struct.pack("<I", len(body)) + body
            process.stdin.write(frame)
            process.stdin.flush()
            try:
                actual = received.get(timeout=4)
            except queue.Empty:
                pytest.fail("Native launcher buffered a live frame until stdin EOF")
            assert actual == body
            assert process.poll() is None, "Reply must arrive before the connection closes"
    finally:
        process.stdin.close()
        try: process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=8)
        reader.join(timeout=2)
        process.stdout.close()
        process.stderr.close()
