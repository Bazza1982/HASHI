from __future__ import annotations

import json
from pathlib import Path

from tools.browser_bridge_harness import (
    build_extension_bundle,
    create_harness_layout,
    rewrite_manifest_name,
    rewrite_service_worker_host_name,
    validate_harness_artifacts,
    write_chrome_launch_script,
    write_harness_config,
    write_native_host_manifest,
    write_smoke_plan,
    write_wsl_host_wrapper,
)


def test_rewrite_service_worker_host_name() -> None:
    content = 'const HOST_NAME = "com.hashi.browser_bridge";\nconsole.log("ok");\n'
    rewritten = rewrite_service_worker_host_name(content, "com.hashi.browser_bridge.test")
    assert 'const HOST_NAME = "com.hashi.browser_bridge.test";' in rewritten
    assert 'com.hashi.browser_bridge";' not in rewritten


def test_rewrite_manifest_name() -> None:
    content = '{\n  "name": "HASHI Browser Bridge",\n  "version": "0.1.0"\n}\n'
    rewritten = rewrite_manifest_name(content, "HASHI Browser Bridge Test")
    assert '"name": "HASHI Browser Bridge Test"' in rewritten


def test_create_harness_layout(tmp_path: Path) -> None:
    layout = create_harness_layout(tmp_path / "harness")
    assert Path(layout["extension_dir"]).is_dir()
    assert Path(layout["native_host_dir"]).is_dir()
    assert Path(layout["logs_dir"]).is_dir()
    assert Path(layout["state_dir"]).is_dir()


def test_build_extension_bundle(tmp_path: Path) -> None:
    source = tmp_path / "src"
    target = tmp_path / "out"
    source.mkdir()
    (source / "manifest.json").write_text(
        '{\n  "name": "HASHI Browser Bridge",\n  "version": "0.1.0"\n}\n',
        encoding="utf-8",
    )
    (source / "service_worker.js").write_text(
        'const HOST_NAME = "com.hashi.browser_bridge";\n',
        encoding="utf-8",
    )

    build_extension_bundle(
        source,
        target,
        host_name="com.hashi.browser_bridge.test",
        extension_name="HASHI Browser Bridge Test",
    )

    assert '"name": "HASHI Browser Bridge Test"' in (target / "manifest.json").read_text(encoding="utf-8")
    assert 'const HOST_NAME = "com.hashi.browser_bridge.test";' in (
        target / "service_worker.js"
    ).read_text(encoding="utf-8")


def test_write_native_host_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "native_host" / "com.hashi.browser_bridge.test.json"
    manifest = write_native_host_manifest(
        manifest_path,
        host_name="com.hashi.browser_bridge.test",
        host_command_path="C:\\test\\host.cmd",
        allowed_origins=["chrome-extension://testid/"],
    )
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert saved == manifest
    assert saved["allowed_origins"] == ["chrome-extension://testid/"]


def test_write_harness_config(tmp_path: Path) -> None:
    config_path = tmp_path / "state" / "config.json"
    config = write_harness_config(
        config_path,
        chrome_exe="C:\\Chrome\\chrome.exe",
        user_data_dir="C:\\Harness\\profile",
        extension_dir="C:\\Harness\\extension",
        native_host_manifest_path="C:\\Harness\\native_host\\host.json",
        socket_path="/tmp/harness.sock",
        log_path="/tmp/harness.log",
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved == config
    assert saved["chrome_exe"] == "C:\\Chrome\\chrome.exe"


def test_write_chrome_launch_script(tmp_path: Path) -> None:
    script_path = tmp_path / "launch_chrome.cmd"
    script = write_chrome_launch_script(
        script_path,
        chrome_exe="C:\\Chrome\\chrome.exe",
        user_data_dir="C:\\Harness\\profile",
        extension_dir="C:\\Harness\\extension",
        start_url="https://example.com",
    )
    assert "--load-extension=\"C:\\Harness\\extension\"" in script
    assert "\"https://example.com\"" in script
    assert script_path.read_text(encoding="utf-8") == script


def test_write_wsl_host_wrapper(tmp_path: Path) -> None:
    script_path = tmp_path / "host.cmd"
    script = write_wsl_host_wrapper(
        script_path,
        distro_name="Ubuntu-22.04",
        repo_root="/home/lily/projects/hashi",
        socket_path="/tmp/harness.sock",
        log_path="/tmp/harness.log",
    )
    assert "cd /d %LOCALAPPDATA%" in script
    assert "python3 -m tools.browser_native_host" in script
    assert "--socket /tmp/harness.sock" in script
    assert script_path.read_text(encoding="utf-8") == script


def test_write_smoke_plan(tmp_path: Path) -> None:
    plan_path = tmp_path / "state" / "smoke_plan.json"
    plan = write_smoke_plan(
        plan_path,
        socket_path="/tmp/harness.sock",
        host_log_path="/tmp/harness.log",
        browser_action_log_path="/tmp/browser_action_audit.jsonl",
        start_url="https://example.com",
    )
    saved = json.loads(plan_path.read_text(encoding="utf-8"))
    assert saved == plan
    assert "ping" in saved["checks"]
    assert saved["artifacts"]["browser_action_log_path"] == "/tmp/browser_action_audit.jsonl"


def test_validate_harness_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "harness"
    layout = create_harness_layout(root)
    (root / "extension" / "manifest.json").write_text("{}", encoding="utf-8")
    (root / "extension" / "service_worker.js").write_text("const x = 1;\n", encoding="utf-8")
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
    )
    write_smoke_plan(
        Path(layout["state_dir"]) / "smoke_plan.json",
        socket_path="/tmp/harness.sock",
        host_log_path="/tmp/harness.log",
        browser_action_log_path="/tmp/browser_action_audit.jsonl",
        start_url="about:blank",
    )
    (root / "README.md").write_text("# test\n", encoding="utf-8")

    validation = validate_harness_artifacts(root)
    assert validation["ok"] is True
    assert validation["missing"] == []
    assert validation["config"]["socket_path"] == "/tmp/harness.sock"
    assert validation["smoke_plan"]["checks"][0] == "extension_loaded"
