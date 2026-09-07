from __future__ import annotations

from pathlib import Path


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
    assert '"--port"' not in script
    assert "Stop-ScheduledTask" in script


def test_browser_uninstaller_is_instance_scoped() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = (
        repo_root / "tools" / "uninstall_browser_option_d_windows.ps1"
    ).read_text(encoding="utf-8")

    assert "BRIDGE_NAMESPACE" in script
    assert '"com.hashi.browser_bridge.$HostSuffix"' in script
    assert "StartsWith($ResolvedBase" in script
    assert "Remove-Item -LiteralPath $ResolvedInstallRoot" in script
