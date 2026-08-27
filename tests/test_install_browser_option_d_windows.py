from __future__ import annotations

from pathlib import Path


def test_native_windows_browser_bridge_installer_is_wsl_independent() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = (repo_root / "tools" / "install_browser_option_d_windows.ps1").read_text(
        encoding="utf-8"
    )

    assert '"HASHI\\browser_bridge"' in script
    assert "-m tools.browser_native_host --stdio %*" in script
    assert "Google\\Chrome\\NativeMessagingHosts" in script
    assert "chrome-extension://$ExtensionId/" in script
    assert "wsl.exe" not in script.lower()
