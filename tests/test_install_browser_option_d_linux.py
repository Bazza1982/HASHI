from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def test_install_browser_option_d_linux_writes_default_and_isolated_manifests(tmp_path: Path) -> None:
    repo_root = Path("/home/lily/projects/hashi")
    script_path = repo_root / "tools" / "install_browser_option_d_linux.sh"

    home_dir = tmp_path / "home"
    xdg_config_home = home_dir / ".config"
    xdg_data_home = home_dir / ".local" / "share"
    isolated_profile = xdg_config_home / "google-chrome-wsl-bridge"

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home_dir),
            "XDG_CONFIG_HOME": str(xdg_config_home),
            "XDG_DATA_HOME": str(xdg_data_home),
        }
    )

    subprocess.run(
        ["bash", str(script_path)],
        cwd=repo_root,
        env=env,
        check=True,
    )

    default_manifest = xdg_config_home / "google-chrome" / "NativeMessagingHosts" / "com.hashi.browser_bridge.wsl.json"
    isolated_manifest = (
        isolated_profile / "NativeMessagingHosts" / "com.hashi.browser_bridge.wsl.json"
    )
    extension_dir = xdg_data_home / "hashi" / "browser_bridge_wsl" / "extension"
    wrapper_path = xdg_data_home / "hashi" / "browser_bridge_wsl" / "hashi_browser_bridge_host.sh"

    assert default_manifest.exists()
    assert isolated_manifest.exists()
    assert wrapper_path.exists()
    assert extension_dir.exists()

    default_data = json.loads(default_manifest.read_text(encoding="utf-8"))
    isolated_data = json.loads(isolated_manifest.read_text(encoding="utf-8"))

    assert default_data == isolated_data
    assert default_data["name"] == "com.hashi.browser_bridge.wsl"
    assert default_data["path"] == str(wrapper_path)
    assert default_data["allowed_origins"] == ["chrome-extension://jdeaedmoejdapldleofeggedgenogpka/"]

    service_worker = (extension_dir / "service_worker.js").read_text(encoding="utf-8")
    assert 'const HOST_NAME = "com.hashi.browser_bridge.wsl";' in service_worker
