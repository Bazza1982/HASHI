from __future__ import annotations

import json
from pathlib import Path

from tools.browser_bridge_test_runner import materialize_option_d_test_harness


def test_materialize_option_d_test_harness(tmp_path: Path) -> None:
    source = tmp_path / "source_extension"
    source.mkdir()
    (source / "manifest.json").write_text(
        '{\n  "name": "HASHI Browser Bridge Recovery",\n  "version": "0.1.0"\n}\n',
        encoding="utf-8",
    )
    (source / "service_worker.js").write_text(
        'const HOST_NAME = "com.hashi.browser_bridge";\n',
        encoding="utf-8",
    )

    layout = materialize_option_d_test_harness(
        tmp_path / "harness",
        source_extension_dir=source,
        chrome_exe="C:\\Chrome\\chrome.exe",
        windows_user_data_dir="C:\\Harness\\profile",
        windows_extension_dir="C:\\Harness\\extension",
        windows_native_host_manifest_path="C:\\Harness\\native_host\\com.hashi.browser_bridge.test.json",
        windows_host_command_path="C:\\Harness\\native_host\\hashi_browser_bridge_test_host.cmd",
        repo_root="/home/lily/projects/hashi",
        distro_name="Ubuntu-22.04",
        socket_path="/tmp/harness.sock",
        log_path="/tmp/harness.log",
        start_url="https://example.com",
    )

    root = Path(layout["root"])
    assert (root / "extension" / "manifest.json").exists()
    assert (root / "extension" / "service_worker.js").exists()
    assert (root / "native_host" / "com.hashi.browser_bridge.test.json").exists()
    assert (root / "native_host" / "hashi_browser_bridge_test_host.cmd").exists()
    assert (root / "launch_chrome_test.cmd").exists()
    assert (root / "state" / "config.json").exists()
    assert (root / "README.md").exists()

    config = json.loads((root / "state" / "config.json").read_text(encoding="utf-8"))
    assert config["chrome_exe"] == "C:\\Chrome\\chrome.exe"
    assert config["socket_path"] == "/tmp/harness.sock"
