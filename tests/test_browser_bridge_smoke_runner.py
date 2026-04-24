from __future__ import annotations

import json
from pathlib import Path

from tools.browser_bridge_harness import (
    create_harness_layout,
    write_chrome_launch_script,
    write_harness_config,
    write_native_host_manifest,
    write_smoke_plan,
    write_wsl_host_wrapper,
)
from tools.browser_bridge_smoke_runner import (
    build_smoke_steps,
    load_harness_state,
    write_smoke_command_plan,
)


def _build_minimal_harness(root: Path) -> None:
    layout = create_harness_layout(root)
    (root / "extension" / "manifest.json").write_text("{}", encoding="utf-8")
    (root / "extension" / "service_worker.js").write_text("const HOST_NAME = \"x\";\n", encoding="utf-8")
    write_native_host_manifest(
        root / "native_host" / "com.hashi.browser_bridge.test.json",
        host_name="com.hashi.browser_bridge.test",
        host_command_path="C:\\test\\host.cmd",
        allowed_origins=[],
    )
    write_wsl_host_wrapper(
        root / "native_host" / "hashi_browser_bridge_test_host.cmd",
        distro_name="Ubuntu-22.04",
        repo_root="/home/lily/projects/hashi",
        socket_path="/tmp/harness.sock",
        log_path="/tmp/harness.log",
    )
    write_harness_config(
        Path(layout["state_dir"]) / "config.json",
        chrome_exe="C:\\Chrome\\chrome.exe",
        user_data_dir="C:\\Harness\\profile",
        extension_dir="C:\\Harness\\extension",
        native_host_manifest_path="C:\\Harness\\native_host\\host.json",
        socket_path="/tmp/harness.sock",
        log_path="/tmp/harness.log",
    )
    write_chrome_launch_script(
        root / "launch_chrome_test.cmd",
        chrome_exe="C:\\Chrome\\chrome.exe",
        user_data_dir="C:\\Harness\\profile",
        extension_dir="C:\\Harness\\extension",
        start_url="https://example.com",
    )
    write_smoke_plan(
        Path(layout["state_dir"]) / "smoke_plan.json",
        socket_path="/tmp/harness.sock",
        host_log_path="/tmp/harness.log",
        browser_action_log_path="/tmp/browser_action_audit.jsonl",
        start_url="https://example.com",
    )
    (root / "README.md").write_text("# test\n", encoding="utf-8")


def test_load_harness_state(tmp_path: Path) -> None:
    root = tmp_path / "harness"
    _build_minimal_harness(root)

    state = load_harness_state(root)
    assert state["validation"]["ok"] is True
    assert state["config"]["socket_path"] == "/tmp/harness.sock"
    assert state["smoke_plan"]["start_url"] == "https://example.com"


def test_build_smoke_steps(tmp_path: Path) -> None:
    root = tmp_path / "harness"
    _build_minimal_harness(root)

    steps = build_smoke_steps(root, repo_root=Path("/home/lily/projects/hashi"))
    assert [step["id"] for step in steps] == [
        "launch_chrome",
        "healthcheck",
        "ping",
        "get_text",
        "screenshot",
    ]
    assert steps[1]["argv"][1].endswith("tools/browser_bridge_smoke_runner.py")
    assert steps[-1]["argv"][-1].endswith("smoke_screenshot.png")


def test_write_smoke_command_plan(tmp_path: Path) -> None:
    root = tmp_path / "harness"
    _build_minimal_harness(root)

    plan = write_smoke_command_plan(root, repo_root=Path("/home/lily/projects/hashi"))
    saved = json.loads((root / "state" / "smoke_commands.json").read_text(encoding="utf-8"))
    assert saved == plan
    assert saved["steps"][0]["id"] == "launch_chrome"
