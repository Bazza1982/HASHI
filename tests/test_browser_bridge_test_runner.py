from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tools.browser_bridge_test_runner import (
    materialize_option_d_test_harness,
    run_option_d_isolated_acceptance,
)


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

    result = materialize_option_d_test_harness(
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

    layout = result["layout"]
    root = Path(layout["root"])
    assert (root / "extension" / "manifest.json").exists()
    assert (root / "extension" / "service_worker.js").exists()
    assert (root / "native_host" / "com.hashi.browser_bridge.test.json").exists()
    assert (root / "native_host" / "hashi_browser_bridge_test_host.cmd").exists()
    assert (root / "launch_chrome_test.cmd").exists()
    assert (root / "state" / "config.json").exists()
    assert (root / "state" / "smoke_plan.json").exists()
    assert (root / "state" / "smoke_commands.json").exists()
    assert (root / "README.md").exists()

    config = json.loads((root / "state" / "config.json").read_text(encoding="utf-8"))
    assert config["chrome_exe"] == "C:\\Chrome\\chrome.exe"
    assert config["socket_path"] == "/tmp/harness.sock"
    smoke_plan = json.loads((root / "state" / "smoke_plan.json").read_text(encoding="utf-8"))
    assert smoke_plan["start_url"] == "https://example.com"
    assert result["validation"]["ok"] is True
    assert result["smoke_commands"]["steps"][1]["id"] == "healthcheck"


def test_run_option_d_isolated_acceptance(tmp_path: Path) -> None:
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

    def fake_runner(argv, capture_output, text):
        command = argv[2]
        if command == "healthcheck":
            return subprocess.CompletedProcess(argv, 0, stdout='{"connected": true}\n', stderr="")
        if command == "ping":
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}\n', stderr="")
        if command == "get_text":
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true, "output": "Example"}\n', stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true, "saved_to": "x"}\n', stderr="")

    result = run_option_d_isolated_acceptance(
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
        smoke_runner=fake_runner,
    )

    root = tmp_path / "harness"
    assert (root / "state" / "smoke_results.json").exists()
    assert (root / "state" / "acceptance_summary.json").exists()
    assert result["smoke_results"]["status"] == "manual_required"
    assert result["acceptance_summary"]["promotable_to_live_acceptance"] is True
