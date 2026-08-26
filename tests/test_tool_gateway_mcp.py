from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from tools.gateway.context import (
    live_workbench_api_base_url,
    load_gateway_context,
    write_gateway_context,
)
from tools.gateway.mcp_stdio import (
    ToolGateway,
    _bridge_legacy_screenshot_output,
    _dispatch,
    _read_frame,
    _write_frame,
)
from tools.registry import ToolRegistry, ToolResult


def _registry(tmp_path: Path) -> ToolRegistry:
    return ToolRegistry(
        allowed_tools=["file_read", "browser_get_text", "browser_click"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={"brave_api_key": "secret-value"},
        max_loops=2,
        audit_context={"agent_name": "momo", "global_config": object()},
    )


def test_gateway_context_is_owner_only_and_reconstructs_registry(tmp_path):
    path = tmp_path / "context.json"
    context = write_gateway_context(_registry(tmp_path), path)

    assert (path.stat().st_mode & 0o777) == 0o600
    assert context.agent == "momo"
    # The retired HER v1 gateway owns its isolated historical circuit breaker;
    # ToolRegistry.max_loops is no longer a source of active execution limits.
    assert context.max_calls == 100
    loaded = load_gateway_context(path)
    assert loaded.build_registry().is_allowed("browser_click")
    assert "global_config" not in loaded.audit


def test_gateway_context_uses_running_workbench_bind_host(tmp_path):
    global_config = SimpleNamespace(api_host="127.0.0.1", workbench_port=18800)
    server = SimpleNamespace(
        bind_host="10.255.255.254",
        global_config=global_config,
    )
    runtime = SimpleNamespace(
        orchestrator=SimpleNamespace(workbench_api=server),
    )
    registry = ToolRegistry(
        allowed_tools=["background_job_list"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={"agent_name": "momo", "_runtime": runtime},
    )

    base_url = live_workbench_api_base_url(registry, global_config)
    assert base_url == "http://10.255.255.254:18800"

    context_path = tmp_path / "live-workbench-context.json"
    write_gateway_context(
        registry,
        context_path,
        workbench_api_base_url=base_url,
    )
    loaded = load_gateway_context(context_path)
    rebuilt = loaded.build_registry()
    assert loaded.schema_version == 5
    assert loaded.workbench_api_base_url == base_url
    assert rebuilt.audit_context["workbench_api_base_url"] == base_url
    assert "_runtime" not in loaded.audit


def test_gateway_namespaces_hashi_filesystem_and_hides_unqualified_authorities(tmp_path):
    gateway = ToolGateway(load_gateway_context(_write_context(_registry(tmp_path), tmp_path)))

    definitions = {item["name"]: item for item in gateway.tool_definitions()}

    assert "hashi_file_read" in definitions
    assert "file_read" not in definitions
    assert "CronList" not in definitions
    assert "access_root scoped" in definitions["hashi_file_read"]["description"]


def test_gateway_context_rejects_group_readable_secret_snapshot(tmp_path):
    path = tmp_path / "context.json"
    write_gateway_context(_registry(tmp_path), path)
    path.chmod(0o640)

    with pytest.raises(PermissionError, match="owner-only"):
        load_gateway_context(path)


def test_gateway_context_loads_schema_v3_scheduler_endpoint_as_workbench_compat(tmp_path):
    path = tmp_path / "legacy-context.json"
    write_gateway_context(
        _registry(tmp_path),
        path,
        workbench_api_base_url="http://127.0.0.1:18800",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 3
    payload.pop("workbench_api_base_url")
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)

    loaded = load_gateway_context(path)
    registry = loaded.build_registry()

    assert registry.audit_context["workbench_api_base_url"] == "http://127.0.0.1:18800"


def test_mcp_lsp_frame_round_trip():
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    stream = io.BytesIO()
    _write_frame(stream, payload)
    stream.seek(0)

    assert _read_frame(stream) == payload


@pytest.mark.asyncio
async def test_mcp_lists_hashi_browser_tools_and_validates_arguments(tmp_path):
    gateway = ToolGateway(load_gateway_context(_write_context(_registry(tmp_path), tmp_path)))

    listed = await _dispatch(gateway, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert {"browser_get_text", "browser_click"} <= names

    invalid = await _dispatch(
        gateway,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "browser_click", "arguments": {}},
        },
    )
    assert invalid["result"]["isError"] is True
    assert "invalid arguments" in invalid["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_gateway_exposes_authoritative_hashi_scheduler_tools(tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    context_path = tmp_path / "scheduler-context.json"
    write_gateway_context(
        registry,
        context_path,
        additional_allowed_tools={
            "hashi_scheduler_list",
            "hashi_scheduler_status",
            "hashi_scheduler_run_history",
            "hashi_scheduler_rerun",
        },
        workbench_api_base_url="http://10.255.255.254:18800",
    )
    gateway = ToolGateway(load_gateway_context(context_path))
    calls = []

    async def fake_request(method, url, *, payload=None):
        calls.append((method, url, payload))
        return 200, {"ok": True, "jobs": [{"job_id": "daily"}]}

    monkeypatch.setattr("tools.hashi_scheduler._request_json", fake_request)

    names = {item["name"] for item in gateway.tool_definitions()}
    assert {
        "hashi_scheduler_list",
        "hashi_scheduler_status",
        "hashi_scheduler_run_history",
        "hashi_scheduler_rerun",
    } <= names
    listed = await gateway.call("hashi_scheduler_list", {"kind": "cron"}, "list-1")
    assert listed["isError"] is False
    assert '"authority": "HASHI Scheduler"' in listed["content"][0]["text"]
    assert calls[-1] == (
        "GET",
        "http://10.255.255.254:18800/api/agents/momo/scheduler/jobs?kind=cron",
        None,
    )

    rerun = await gateway.call(
        "hashi_scheduler_rerun",
        {
            "kind": "cron",
            "job_id": "daily",
            "authorization": "explicit_user_authorization",
        },
        "run-1",
    )
    assert rerun["isError"] is False
    assert calls[-1] == (
        "POST",
        "http://10.255.255.254:18800/api/agents/momo/jobs/run",
        {
            "kind": "cron",
            "job_id": "daily",
            "requested_by": "hashi_tool_gateway",
            "authorization": "explicit_user_authorization",
        },
    )


@pytest.mark.asyncio
async def test_gateway_background_jobs_use_serialized_workbench_api(tmp_path, monkeypatch):
    registry = ToolRegistry(
        allowed_tools=[
            "background_job_start",
            "background_job_status",
            "background_job_tail",
            "background_job_cancel",
            "background_job_list",
        ],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={"agent_name": "momo"},
    )
    context_path = tmp_path / "background-context.json"
    write_gateway_context(
        registry,
        context_path,
        workbench_api_base_url="http://10.255.255.254:18800",
    )
    gateway = ToolGateway(load_gateway_context(context_path))
    calls = []
    record = {
        "job_id": "job-http",
        "state": "running",
        "returncode": None,
        "created_at": "2026-08-17T09:00:00Z",
        "updated_at": "2026-08-17T09:00:00Z",
        "ended_at": None,
        "error": None,
        "command": {"display": "python wiki.py"},
        "logs": {"stdout_path": "/tmp/stdout", "stderr_path": "/tmp/stderr"},
        "notification": {"delivered": False},
    }

    async def fake_request(method, url, *, payload=None, timeout_seconds=30):
        calls.append((method, url, payload, timeout_seconds))
        if url.endswith("/tail?stream=stdout&lines=25"):
            return 200, {"ok": True, "tail": "wiki completed"}
        if url.endswith("/cancel"):
            return 200, {"ok": True, "job": {**record, "state": "cancelled"}}
        if url.endswith("/job-http"):
            return 200, {"ok": True, "job": record}
        if method == "POST":
            return 201, {"ok": True, "job": record}
        return 200, {"ok": True, "jobs": [record]}

    monkeypatch.setattr("tools.builtins.request_workbench_json", fake_request)

    started = await gateway.call(
        "background_job_start",
        {"command": "python wiki.py", "cwd": "."},
        "bg-start",
    )
    status = await gateway.call(
        "background_job_status",
        {"job_id": "job-http"},
        "bg-status",
    )
    tail = await gateway.call(
        "background_job_tail",
        {"job_id": "job-http", "lines": 25},
        "bg-tail",
    )
    cancelled = await gateway.call(
        "background_job_cancel",
        {"job_id": "job-http"},
        "bg-cancel",
    )
    listed = await gateway.call(
        "background_job_list",
        {"agent": "momo", "limit": 5},
        "bg-list",
    )

    assert all(
        result["isError"] is False
        for result in (started, status, tail, cancelled, listed)
    )
    assert '"job_id": "job-http"' in started["content"][0]["text"]
    assert "wiki completed" in tail["content"][0]["text"]
    assert '"state": "cancelled"' in cancelled["content"][0]["text"]
    assert calls[0][0:2] == (
        "POST",
        "http://10.255.255.254:18800/api/background-jobs",
    )
    assert calls[-1][0:2] == (
        "GET",
        "http://10.255.255.254:18800/api/background-jobs?limit=5&agent=momo",
    )
    assert calls[0][2]["agent"] == "momo"
    assert calls[0][2]["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_gateway_background_jobs_report_missing_workbench_context(tmp_path):
    registry = ToolRegistry(
        allowed_tools=["background_job_list"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={"agent_name": "momo"},
    )
    gateway = ToolGateway(load_gateway_context(_write_context(registry, tmp_path)))

    result = await gateway.call("background_job_list", {}, "bg-list")

    assert result["isError"] is True
    assert "Workbench API is unavailable in this gateway context" in result["content"][0]["text"]
    assert "BackgroundJobManager is not running" not in result["content"][0]["text"]


def _write_context(registry: ToolRegistry, tmp_path: Path) -> Path:
    path = tmp_path / "context.json"
    write_gateway_context(registry, path)
    return path


def _png_base64() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 20), "purple").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


@pytest.mark.asyncio
async def test_gateway_bridges_legacy_browser_screenshot_string_to_image(tmp_path, monkeypatch):
    registry = ToolRegistry(
        allowed_tools=["browser_screenshot"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
    )
    gateway = ToolGateway(load_gateway_context(_write_context(registry, tmp_path)))
    original_payload = _png_base64()

    async def screenshot_execute(name, arguments, tool_call_id=""):
        assert name == "browser_screenshot"
        return ToolResult(
            tool_call_id=tool_call_id,
            output=f"screenshot:{original_payload}",
        )

    monkeypatch.setattr(gateway.registry, "execute", screenshot_execute)
    result = await gateway.call(
        "browser_screenshot",
        {"url": "https://example.com"},
        "call-screenshot",
    )

    assert result["isError"] is False
    assert len(result["content"]) == 1
    assert result["content"][0]["type"] == "image"
    assert result["content"][0]["mimeType"] == "image/jpeg"
    assert base64.b64decode(result["content"][0]["data"]).startswith(b"\xff\xd8\xff")
    assert original_payload not in json.dumps(result)


def test_gateway_bridges_legacy_desktop_and_session_screenshots_in_order():
    payload = _png_base64()
    desktop_content, desktop_error = _bridge_legacy_screenshot_output(
        "desktop_screenshot",
        f"Screenshot OK — 1KB\ndata:image/png;base64,{payload}",
    )
    assert desktop_error is False
    assert desktop_content and [block["type"] for block in desktop_content] == ["text", "image"]
    assert payload not in json.dumps(desktop_content)

    session_content, session_error = _bridge_legacy_screenshot_output(
        "browser_session",
        f"[goto] https://example.com\n[screenshot] base64:{payload}\n[click] button",
    )
    assert session_error is False
    assert session_content and [block["type"] for block in session_content] == [
        "text",
        "image",
        "text",
    ]
    assert "[goto]" in session_content[0]["text"]
    assert "[click]" in session_content[2]["text"]


def test_gateway_rejects_malformed_legacy_screenshot_payload():
    content, is_error = _bridge_legacy_screenshot_output(
        "windows_screenshot",
        "Windows screenshot OK\ndata:image/png;base64,not-valid!",
    )

    assert is_error is True
    assert content and any(
        "Screenshot unavailable" in block.get("text", "") for block in content
    )
    assert "not-valid" not in json.dumps(content)


def test_gateway_bounds_legacy_browser_session_screenshot_count():
    payload = _png_base64()
    output = "\n".join(
        f"[screenshot] base64:{payload}" for _index in range(7)
    )

    content, is_error = _bridge_legacy_screenshot_output("browser_session", output)

    assert is_error is False
    assert content and sum(block.get("type") == "image" for block in content) == 6
    assert any("6-image safety limit" in block.get("text", "") for block in content)


@pytest.mark.asyncio
async def test_gateway_stops_identical_retry_loop(tmp_path, monkeypatch):
    context_path = _write_context(_registry(tmp_path), tmp_path)
    gateway = ToolGateway(load_gateway_context(context_path))

    async def successful_execute(name, arguments, tool_call_id=""):
        from tools.registry import ToolResult

        return ToolResult(tool_call_id=tool_call_id, output="ok", is_error=False)

    monkeypatch.setattr(gateway.registry, "execute", successful_execute)
    arguments = {"path": "README.md"}
    for number in range(gateway.context.max_identical_calls):
        result = await gateway.call("hashi_file_read", arguments, str(number))
        assert result["isError"] is False

    stopped = await gateway.call("hashi_file_read", arguments, "last")
    assert stopped["isError"] is True
    assert "repeated identical call" in stopped["content"][0]["text"]


@pytest.mark.asyncio
async def test_gateway_allows_repeated_browser_side_effect_when_state_changes(tmp_path, monkeypatch):
    context_path = _write_context(_registry(tmp_path), tmp_path)
    gateway = ToolGateway(load_gateway_context(context_path))

    async def changing_execute(name, arguments, tool_call_id=""):
        from tools.registry import ToolResult

        return ToolResult(
            tool_call_id=tool_call_id,
            output=json.dumps({"ok": True, "state_changed": True}),
            is_error=False,
        )

    monkeypatch.setattr(gateway.registry, "execute", changing_execute)
    arguments = {"url": "https://example.com", "selector": "button.next"}
    for number in range(gateway.context.max_identical_calls + 2):
        result = await gateway.call("browser_click", arguments, str(number))
        assert result["isError"] is False
