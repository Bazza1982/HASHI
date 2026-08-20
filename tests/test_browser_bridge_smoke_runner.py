from __future__ import annotations

import json
import socketserver
import subprocess
import threading
import time
from pathlib import Path

import pytest

from tools.browser_bridge_harness import (
    create_harness_layout,
    write_chrome_launch_script,
    write_harness_config,
    write_native_host_manifest,
    write_smoke_plan,
    write_wsl_host_wrapper,
)
from tools.browser_bridge_smoke_runner import (
    execute_smoke_plan,
)
from tools.browser_bridge_stub_server import running_stub_bridge


ROOT = Path(__file__).resolve().parents[1]


requires_unix_stream_server = pytest.mark.skipif(
    not hasattr(socketserver, "UnixStreamServer"),
    reason="Unix domain socket server is unavailable on this platform",
)


def _build_minimal_harness(root: Path, *, socket_path: str = "/tmp/harness.sock") -> None:
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
        repo_root=str(ROOT),
        socket_path=socket_path,
        log_path="/tmp/harness.log",
    )
    write_harness_config(
        Path(layout["state_dir"]) / "config.json",
        chrome_exe="C:\\Chrome\\chrome.exe",
        user_data_dir="C:\\Harness\\profile",
        extension_dir="C:\\Harness\\extension",
        native_host_manifest_path="C:\\Harness\\native_host\\host.json",
        socket_path=socket_path,
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
        socket_path=socket_path,
        host_log_path="/tmp/harness.log",
        browser_action_log_path="/tmp/browser_action_audit.jsonl",
        start_url="https://example.com",
    )
    (root / "README.md").write_text("# test\n", encoding="utf-8")


@requires_unix_stream_server
def test_execute_smoke_plan_with_stub_bridge(tmp_path: Path) -> None:
    root = tmp_path / "harness"
    socket_path = tmp_path / "bridge.sock"
    trace_path = tmp_path / "stub_trace.jsonl"
    _build_minimal_harness(root, socket_path=str(socket_path))

    with running_stub_bridge(socket_path, trace_path=trace_path):
        report = execute_smoke_plan(
            root,
            repo_root=ROOT,
            runner=subprocess.run,
            stop_on_failure=True,
        )

    assert report["status"] == "manual_required"
    assert [item["status"] for item in report["results"][1:]] == ["passed", "passed", "passed", "passed", "passed"]
    assert (root / "logs" / "smoke_screenshot.png").exists()
    trace_lines = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert trace_lines[0]["event"] == "server_started"
    assert any(item["event"] == "request" and item["action"] == "ping" for item in trace_lines)
    assert trace_lines[-1]["event"] == "server_stopped"


@requires_unix_stream_server
def test_execute_smoke_plan_waits_for_delayed_stub_bridge(tmp_path: Path) -> None:
    root = tmp_path / "harness"
    socket_path = tmp_path / "delayed_bridge.sock"
    _build_minimal_harness(root, socket_path=str(socket_path))
    stop_event = threading.Event()

    def start_later() -> None:
        time.sleep(0.6)
        with running_stub_bridge(socket_path):
            stop_event.wait(3)

    thread = threading.Thread(target=start_later, daemon=True)
    thread.start()
    try:
        report = execute_smoke_plan(
            root,
            repo_root=ROOT,
            runner=subprocess.run,
            stop_on_failure=True,
        )
    finally:
        stop_event.set()
        thread.join(timeout=2)

    assert report["status"] == "manual_required"
    assert [item["status"] for item in report["results"][1:]] == ["passed", "passed", "passed", "passed", "passed"]
