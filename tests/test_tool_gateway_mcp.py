from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from orchestrator.background_job_policy import USER_BACKGROUND_JOB_REQUEST_SOURCE
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

    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600
    assert context.agent == "momo"
    # The retired HER v1 gateway owns its isolated historical circuit breaker;
    # ToolRegistry.max_loops is no longer a source of active execution limits.
    assert context.max_calls == 100
    loaded = load_gateway_context(path)
    assert loaded.build_registry().is_allowed("browser_click")
    assert "global_config" not in loaded.audit


def test_gateway_context_uses_running_workbench_bind_host(tmp_path):
    global_config = SimpleNamespace(
        instance_id="HASHI3",
        api_host="127.0.0.1",
        workbench_port=18800,
    )
    runtime = SimpleNamespace(
        orchestrator=SimpleNamespace(
            resolve_service_endpoint=lambda service, expected_instance=None: {
                "service": service,
                "instance_id": expected_instance,
                "host": "172.29.144.7",
                "port": 18847,
                "base_url": "http://172.29.144.7:18847",
            }
        ),
    )
    registry = ToolRegistry(
        allowed_tools=["background_job_list"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={"agent_name": "momo", "_runtime": runtime},
    )

    base_url = live_workbench_api_base_url(registry, global_config)
    assert base_url == "http://172.29.144.7:18847"

    context_path = tmp_path / "live-workbench-context.json"
    write_gateway_context(
        registry,
        context_path,
        workbench_api_base_url=base_url,
    )
    loaded = load_gateway_context(context_path)
    rebuilt = loaded.build_registry()
    assert loaded.schema_version == 7
    assert loaded.workbench_api_base_url == base_url
    assert rebuilt.audit_context["workbench_api_base_url"] == base_url
    assert "_runtime" not in loaded.audit


def test_gateway_context_rejects_cross_instance_workbench_endpoint(tmp_path):
    global_config = SimpleNamespace(instance_id="HASHI3")

    def resolve(service, *, expected_instance=None):
        assert expected_instance == "HASHI3"
        return {
            "service": service,
            "instance_id": "HASHI1",
            "base_url": "http://127.0.0.1:18800",
        }

    registry = ToolRegistry(
        allowed_tools=["background_job_list"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={
            "agent_name": "momo",
            "_runtime": SimpleNamespace(
                orchestrator=SimpleNamespace(resolve_service_endpoint=resolve)
            ),
        },
    )

    with pytest.raises(ValueError, match="cross-instance"):
        live_workbench_api_base_url(registry, global_config)


def test_gateway_namespaces_hashi_filesystem_and_hides_unqualified_authorities(tmp_path):
    gateway = ToolGateway(load_gateway_context(_write_context(_registry(tmp_path), tmp_path)))

    definitions = {item["name"]: item for item in gateway.tool_definitions()}

    assert "hashi_file_read" in definitions
    assert "file_read" not in definitions
    assert "CronList" not in definitions
    assert "access_root scoped" in definitions["hashi_file_read"]["description"]


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows stat modes do not represent the installer-owned NTFS DACL",
)
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
async def test_mcp_preserves_safe_authoritative_error_details(tmp_path, monkeypatch):
    gateway = ToolGateway(load_gateway_context(_write_context(_registry(tmp_path), tmp_path)))

    async def denied(*args, **kwargs):
        return ToolResult(
            tool_call_id="runtime-denial",
            output="Error: installed runtime protection could not be verified; use controlled recovery.",
            is_error=True,
            details={
                "control_disposition": "denied",
                "tool_executed": False,
                "error_code": "runtime_integrity_failed",
                "retryable": False,
                "runtime_protection": {
                    "execution_role": "functions",
                    "qualification_scope": "core_and_functions",
                    "instance_id": "HASHI6",
                    "agent_id": "lin_yueru",
                    "request_id": "request-1",
                    "session_id": "session-1",
                    "run_id": "run-1",
                    "recovery": "controlled_installation_maintenance",
                },
                "secret": "must-not-cross-mcp",
            },
        )

    monkeypatch.setattr(gateway.registry, "execute", denied)
    result = await gateway.call(
        "hashi_file_read", {"path": str(tmp_path / "note.txt")}, "runtime-denial"
    )

    projected = result["structuredContent"]["hashi"]["details"]
    assert result["_meta"]["hashi/toolResultDetails"] == projected
    assert projected["error_code"] == "runtime_integrity_failed"
    assert projected["tool_executed"] is False
    assert projected["retryable"] is False
    assert projected["runtime_protection"]["instance_id"] == "HASHI6"
    assert projected["runtime_protection"]["run_id"] == "run-1"
    assert "secret" not in projected
    assert any("runtime_integrity_failed" in block.get("text", "") for block in result["content"])


def test_mcp_size_details_expose_only_nonnegative_integer_counts():
    from tools.gateway.mcp_stdio import _safe_mcp_result_details

    assert _safe_mcp_result_details({
        "actual_bytes": 123, "limit_bytes": 100, "secret": "private",
    }) == {"actual_bytes": 123, "limit_bytes": 100}
    assert _safe_mcp_result_details({
        "actual_bytes": True, "limit_bytes": "not-a-count", "secret": "private",
    }) == {}
    assert _safe_mcp_result_details({"actual_bytes": -1, "limit_bytes": -1}) == {}



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
            "hashi_scheduler_recovery_resolve",
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
        "hashi_scheduler_recovery_resolve",
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
        },
    )

    resolved = await gateway.call(
        "hashi_scheduler_recovery_resolve",
        {
            "batch_id": "recovery-1",
            "action": "rerun_selected",
            "counts": {"daily": 2},
        },
        "resolve-1",
    )
    assert resolved["isError"] is False
    assert calls[-1] == (
        "POST",
        "http://10.255.255.254:18800/api/agents/momo/scheduler/recovery/recovery-1/resolve",
        {
            "action": "rerun_selected",
            "counts": {"daily": 2},
            "requested_by": "hashi_tool_gateway",
        },
    )


@pytest.mark.asyncio
async def test_gateway_exposes_agent_owned_scheduler_and_superloop_mutations(
    tmp_path, monkeypatch
):
    registry = _registry(tmp_path)
    context_path = tmp_path / "agent-task-context.json"
    write_gateway_context(
        registry,
        context_path,
        additional_allowed_tools={
            "hashi_scheduler_create",
            "hashi_scheduler_update",
            "hashi_scheduler_delete",
            "hashi_superloop_list",
            "hashi_superloop_get",
            "hashi_superloop_create",
            "hashi_superloop_update",
            "hashi_superloop_delete",
        },
        workbench_api_base_url="http://10.255.255.254:18800",
    )
    gateway = ToolGateway(load_gateway_context(context_path))
    calls = []

    async def fake_request(method, url, *, payload=None):
        calls.append((method, url, payload))
        if method == "GET" and "/scheduler/status?" in url:
            return 200, {"ok": True, "status": {"deletion_revision": "a" * 64}}
        return 200, {"ok": True, "state_changed": True}

    monkeypatch.setattr("tools.hashi_scheduler._request_json", fake_request)
    monkeypatch.setattr("tools.hashi_superloop._request_json", fake_request)

    names = {item["name"] for item in gateway.tool_definitions()}
    assert {
        "hashi_scheduler_create",
        "hashi_scheduler_update",
        "hashi_scheduler_delete",
        "hashi_superloop_list",
        "hashi_superloop_get",
        "hashi_superloop_create",
        "hashi_superloop_update",
        "hashi_superloop_delete",
    } <= names

    created = await gateway.call(
        "hashi_scheduler_create",
        {
            "kind": "heartbeat",
            "task_id": "momo-loop-1",
            "interval_seconds": 60,
            "prompt": "check the work",
        },
        "create-1",
    )
    assert created["isError"] is False
    assert calls[-1] == (
        "POST",
        "http://10.255.255.254:18800/api/agents/momo/scheduler/jobs",
        {
            "kind": "heartbeat",
            "task_id": "momo-loop-1",
            "interval_seconds": 60,
            "prompt": "check the work",
            "requested_by": "hashi_tool_gateway",
        },
    )

    updated = await gateway.call(
        "hashi_scheduler_update",
        {
            "kind": "heartbeat",
            "job_id": "momo-loop-1",
            "enabled": False,
        },
        "update-1",
    )
    assert updated["isError"] is False
    assert calls[-1] == (
        "PATCH",
        "http://10.255.255.254:18800/api/agents/momo/scheduler/jobs/momo-loop-1",
        {
            "kind": "heartbeat",
            "enabled": False,
            "requested_by": "hashi_tool_gateway",
        },
    )

    denied = await gateway.call(
        "hashi_scheduler_delete",
        {"kind": "heartbeat"},
        "delete-denied",
    )
    assert denied["isError"] is True
    assert calls[-1][0] == "PATCH"

    deleted = await gateway.call(
        "hashi_scheduler_delete",
        {
            "kind": "heartbeat",
            "job_id": "momo-loop-1",
        },
        "delete-1",
    )
    assert deleted["isError"] is False
    assert calls[-2] == (
        "GET",
        "http://10.255.255.254:18800/api/agents/momo/scheduler/status?kind=heartbeat&job_id=momo-loop-1",
        None,
    )
    assert calls[-1] == (
        "DELETE",
        "http://10.255.255.254:18800/api/agents/momo/scheduler/jobs/momo-loop-1",
        {
            "kind": "heartbeat",
            "requested_by": "hashi_tool_gateway",
            "expected_revision": "a" * 64,
        },
    )

    created_loop = await gateway.call(
        "hashi_superloop_create",
        {"goal": "keep the migration moving"},
        "superloop-create-1",
    )
    assert created_loop["isError"] is False
    assert calls[-1] == (
        "POST",
        "http://10.255.255.254:18800/api/agents/momo/superloops",
        {
            "goal": "keep the migration moving",
            "task_title": None,
            "requested_by": "hashi_tool_gateway",
        },
    )

    await gateway.call(
        "hashi_superloop_update",
        {"loop_id": "sl-1", "action": "pause", "mode": "drain"},
        "superloop-update-1",
    )
    assert calls[-1] == (
        "PATCH",
        "http://10.255.255.254:18800/api/agents/momo/superloops/sl-1",
        {
            "action": "pause",
            "mode": "drain",
            "requested_by": "hashi_tool_gateway",
        },
    )

    denied_loop = await gateway.call(
        "hashi_superloop_delete",
        {},
        "superloop-delete-denied",
    )
    assert denied_loop["isError"] is True
    assert calls[-1][0] == "PATCH"

    deleted_loop = await gateway.call(
        "hashi_superloop_delete",
        {"loop_id": "sl-1"},
        "superloop-delete-1",
    )
    assert deleted_loop["isError"] is False
    assert calls[-1] == (
        "DELETE",
        "http://10.255.255.254:18800/api/agents/momo/superloops/sl-1",
        {
            "requested_by": "hashi_tool_gateway",
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
        audit_context={
            "agent_name": "momo",
            "request_source": USER_BACKGROUND_JOB_REQUEST_SOURCE,
        },
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
    assert (
        "Backend API is unavailable in this tool context"
        in result["content"][0]["text"]
    )
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
    assert any("remaining frames are unavailable" in block.get("text", "") for block in content)


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
# HASHI-Fingerprint: hfp_cf66b29779af41f99a82f150ae5ef628



@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["codex-cli", "claude-cli", "her-v3"])
async def test_current_gateway_does_not_apply_legacy_repeat_call_or_error_limits(tmp_path, backend):
    from dataclasses import replace
    from tools.gateway.context import GatewayContext

    path = tmp_path / "readable.txt"
    path.write_text("fixture", encoding="utf-8")
    registry = ToolRegistry(["file_read"], tmp_path, tmp_path, {})
    original = GatewayContext.from_registry(registry, backend=backend)
    assert original.enforce_legacy_limits is False
    context = replace(original, max_calls=2, max_identical_calls=2, max_consecutive_errors=2)
    gateway = ToolGateway(context)
    for i in range(6):
        result = await gateway.call("hashi_file_read", {"path": str(tmp_path / "missing")}, f"missing-{i}")
        assert result["isError"] is True
        assert "legacy HER v1" not in str(result)
    for i in range(6):
        result = await gateway.call("hashi_file_read", {"path": str(path)}, f"valid-{i}")
        assert result["isError"] is False
        assert "fixture" in str(result)
    assert gateway.call_count == 12


@pytest.mark.asyncio
async def test_retired_legacy_gateway_repeat_limit_is_explicitly_scoped(tmp_path):
    from dataclasses import replace
    from tools.gateway.context import GatewayContext

    path = tmp_path / "readable.txt"
    path.write_text("fixture", encoding="utf-8")
    registry = ToolRegistry(["file_read"], tmp_path, tmp_path, {})
    original = GatewayContext.from_registry(registry, backend="her-v1")
    assert original.enforce_legacy_limits is True
    gateway = ToolGateway(replace(original, max_calls=100, max_identical_calls=2))
    for i in range(2):
        result = await gateway.call("hashi_file_read", {"path": str(path)}, f"legacy-{i}")
        assert result["isError"] is False
    result = await gateway.call("hashi_file_read", {"path": str(path)}, "legacy-2")
    assert result["isError"] is True and "legacy HER v1" in str(result)


@pytest.mark.asyncio
async def test_legacy_screenshot_paging_preserves_every_image_and_run_scope(tmp_path, monkeypatch):
    from tools.legacy_screenshot import normalize_legacy_screenshot as _normalize_legacy_screenshot

    registry = ToolRegistry(
        allowed_tools=["browser_session"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={"instance_id": "TEST", "agent_name": "momo", "owner_id": "user",
                       "hashi_session_id": "session", "hashi_run_id": "run-a", "request_id": "request-a"},
    )
    gateway = ToolGateway(load_gateway_context(_write_context(registry, tmp_path)))
    payloads = []
    for index in range(7):
        buf = io.BytesIO()
        Image.new("RGB", (32, 20), (index * 31, 45, 210 - index * 25)).save(buf, format="PNG")
        payloads.append(base64.b64encode(buf.getvalue()).decode("ascii"))
    legacy = "\n".join(f"[screenshot] base64:{payload}" for payload in payloads)
    original_dispatch = gateway.registry._dispatch

    async def fake_dispatch(name, arguments, *, tool_call_id=""):
        if name == "browser_session":
            return legacy
        return await original_dispatch(name, arguments, tool_call_id=tool_call_id)

    monkeypatch.setattr(gateway.registry, "_dispatch", fake_dispatch)
    first = await gateway.call("browser_session", {"steps": [{"action": "screenshot"}]}, "first")
    assert first["isError"] is False
    first_images = [part for part in first["content"] if part["type"] == "image"]
    assert len(first_images) == 6
    details = first["structuredContent"]["hashi"]["details"]
    reference, offset = details["image_continuation"], details["image_next_offset"]
    assert offset == 6 and details["complete_content"] is False

    second = await gateway.call("tool_output_read", {"reference": reference, "offset": offset}, "second")
    assert second["isError"] is False
    second_images = [part for part in second["content"] if part["type"] == "image"]
    assert len(second_images) == 1
    assert first_images + second_images == [_normalize_legacy_screenshot(payload)[0] for payload in payloads]
    assert second["structuredContent"]["hashi"]["details"]["complete_content"] is True

    foreign = await gateway.registry.execute_with_audit_context(
        "tool_output_read", {"reference": reference, "offset": offset}, tool_call_id="foreign",
        audit_context={"instance_id": "TEST", "agent_name": "momo", "owner_id": "user",
                       "hashi_session_id": "session", "hashi_run_id": "run-b", "request_id": "request-b"},
    )
    assert foreign.is_error
    assert not foreign.content


@pytest.mark.asyncio
async def test_legacy_screenshot_rejects_declared_mime_mismatch(tmp_path, monkeypatch):
    registry = ToolRegistry(allowed_tools=["browser_screenshot"], access_root=tmp_path,
                            workspace_dir=tmp_path, secrets={})
    gateway = ToolGateway(load_gateway_context(_write_context(registry, tmp_path)))

    async def fake_execute(name, arguments, tool_call_id=""):
        return ToolResult(tool_call_id=tool_call_id,
                          output=f"data:image/gif;base64,{_png_base64()}")

    monkeypatch.setattr(gateway.registry, "execute", fake_execute)
    result = await gateway.call("browser_screenshot", {"url": "https://example.com"}, "bad-mime")
    assert result["isError"] is True
    assert not any(part["type"] == "image" for part in result["content"])
    assert "mime" in json.dumps(result).lower()


@pytest.mark.asyncio
async def test_gateway_leaves_existing_structured_images_uncapped(tmp_path, monkeypatch):
    registry = ToolRegistry(allowed_tools=["browser_screenshot"], access_root=tmp_path,
                            workspace_dir=tmp_path, secrets={})
    gateway = ToolGateway(load_gateway_context(_write_context(registry, tmp_path)))
    images = [{"type": "image", "mimeType": "image/png", "data": _png_base64()} for _ in range(7)]

    async def fake_execute(name, arguments, tool_call_id=""):
        return ToolResult(tool_call_id=tool_call_id, output="images", content=images)

    monkeypatch.setattr(gateway.registry, "execute", fake_execute)
    result = await gateway.call("browser_screenshot", {"url": "https://example.com"}, "structured")
    assert result["isError"] is False
    assert result["content"] == images


@pytest.mark.asyncio
async def test_legacy_screenshot_pages_preserve_bytes_beyond_default_budget(tmp_path, monkeypatch):
    import random
    from tools.legacy_screenshot import normalize_legacy_screenshot as _normalize_legacy_screenshot

    registry = ToolRegistry(
        allowed_tools=["browser_session"], access_root=tmp_path, workspace_dir=tmp_path, secrets={},
        audit_context={"agent_name": "momo", "request_id": "budget-request", "hashi_run_id": "budget-run"},
    )
    gateway = ToolGateway(load_gateway_context(_write_context(registry, tmp_path)))
    pixels = random.Random(12).randbytes(1600 * 1600 * 3)
    buf = io.BytesIO()
    Image.frombytes("RGB", (1600, 1600), pixels).save(buf, format="JPEG", quality=90)
    payload = base64.b64encode(buf.getvalue()).decode("ascii")
    expected, size = _normalize_legacy_screenshot(payload)
    assert size * 3 > 4_000_000
    output = "\n".join(f"data:image/jpeg;base64,{payload}" for _ in range(3))
    original_dispatch = gateway.registry._dispatch

    async def fake_dispatch(name, arguments, *, tool_call_id=""):
        if name == "browser_session":
            return output
        return await original_dispatch(name, arguments, tool_call_id=tool_call_id)

    monkeypatch.setattr(gateway.registry, "_dispatch", fake_dispatch)
    page = await gateway.call("browser_session", {"steps": [{"action": "screenshot"}]}, "first")
    images = [part for part in page["content"] if part["type"] == "image"]
    detail = page["structuredContent"]["hashi"]["details"]
    reference, offset = detail["image_continuation"], detail["image_next_offset"]
    assert reference and offset < 3
    while offset is not None:
        page = await gateway.call("tool_output_read", {"reference": reference, "offset": offset, "limit": 1}, f"page-{offset}")
        assert page["isError"] is False
        images.extend(part for part in page["content"] if part["type"] == "image")
        detail = page["structuredContent"]["hashi"]["details"]
        offset = detail["image_next_offset"]
    assert images == [expected] * 3
    assert detail["complete_content"] is True


@pytest.mark.asyncio
async def test_screenshot_paging_keeps_prior_decode_failure_visible(tmp_path, monkeypatch):
    monkeypatch.setenv("HASHI_MCP_IMAGE_PAGE_COUNT", "1")
    registry = ToolRegistry(
        allowed_tools=["browser_session"], access_root=tmp_path, workspace_dir=tmp_path, secrets={},
        audit_context={"agent_name": "momo", "request_id": "failure-request", "hashi_run_id": "failure-run"},
    )
    gateway = ToolGateway(load_gateway_context(_write_context(registry, tmp_path)))
    output = f"data:image/gif;base64,{_png_base64()}\n[screenshot] base64:{_png_base64()}"
    original_dispatch = gateway.registry._dispatch

    async def fake_dispatch(name, arguments, *, tool_call_id=""):
        if name == "browser_session":
            return output
        return await original_dispatch(name, arguments, tool_call_id=tool_call_id)

    monkeypatch.setattr(gateway.registry, "_dispatch", fake_dispatch)
    first = await gateway.call("browser_session", {"steps": [{"action": "screenshot"}]}, "first")
    assert first["isError"] is True
    detail = first["structuredContent"]["hashi"]["details"]
    assert detail["image_next_offset"] == 1
    last = await gateway.call("tool_output_read", {
        "reference": detail["image_continuation"], "offset": 1,
    }, "last")
    assert any(part["type"] == "image" for part in last["content"])
    assert last["structuredContent"]["hashi"]["details"]["complete_content"] is False


@pytest.mark.asyncio
async def test_nonvision_gateway_does_not_fall_back_to_raw_screenshot_payload(tmp_path, monkeypatch):
    from dataclasses import replace
    registry = ToolRegistry(allowed_tools=["browser_screenshot"], access_root=tmp_path,
                            workspace_dir=tmp_path, secrets={})
    context = load_gateway_context(_write_context(registry, tmp_path))
    gateway = ToolGateway(replace(context, vision_enabled=False))
    payload = _png_base64()

    async def fake_execute(name, arguments, tool_call_id=""):
        return ToolResult(tool_call_id=tool_call_id, output=f"screenshot:{payload}")

    monkeypatch.setattr(gateway.registry, "execute", fake_execute)
    result = await gateway.call("browser_screenshot", {"url": "https://example.com"}, "nonvision")
    assert payload not in json.dumps(result)
    assert "non-vision" in json.dumps(result)


@pytest.mark.asyncio
async def test_screenshot_projection_uses_owner_budget_for_matching_offset(tmp_path, monkeypatch):
    monkeypatch.setenv("HASHI_MCP_IMAGE_PAGE_COUNT", "1")
    registry = ToolRegistry(allowed_tools=["browser_session"], access_root=tmp_path,
                            workspace_dir=tmp_path, secrets={})
    gateway = ToolGateway(load_gateway_context(_write_context(registry, tmp_path)))
    output = "\n".join(f"[screenshot] base64:{_png_base64()}" for _ in range(7))

    async def fake_execute(name, arguments, tool_call_id=""):
        return ToolResult(
            tool_call_id=tool_call_id, output=output,
            details={"complete_content": False, "image_continuation": "owner-reference",
                     "image_next_offset": 6, "image_total": 7,
                     "image_page_count_budget": 6, "image_page_byte_budget": 4_000_000},
        )

    monkeypatch.setattr(gateway.registry, "execute", fake_execute)
    first = await gateway.call("browser_session", {"steps": [{"action": "screenshot"}]}, "first")
    assert sum(part["type"] == "image" for part in first["content"]) == 6
    assert first["structuredContent"]["hashi"]["details"]["image_next_offset"] == 6
