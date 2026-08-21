from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from adapters.claw_cli import ClawCLIAdapter
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


def _her_e2e_runtime_provider() -> dict[str, str]:
    staged_binary = os.environ.get("HASHI_HER_STAGED_BINARY", "").strip()
    if staged_binary:
        return {
            "runtime_policy": "system-only",
            "binary_path": staged_binary,
        }
    return {"runtime_policy": "require-packaged"}


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
    assert loaded.schema_version == 4
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


def test_packaged_claw_calls_hashi_tool_gateway_and_returns_answer(tmp_path):
    (tmp_path / "probe.txt").write_text("browser gateway works", encoding="utf-8")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            requests.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if not body.get("tools"):
                chunks = [
                    {
                        "id": "chatcmpl-plan",
                        "choices": [
                            {
                                "delta": {
                                    "content": json.dumps(
                                        {
                                            "acknowledgement": "I will read only probe.txt through the HASHI file tool and report the returned content.",
                                            "active_goal": "Read probe.txt with the HASHI file tool and answer.",
                                            "success_criteria": ["report the file content"],
                                            "planned_actions": ["read probe.txt", "report the result"],
                                            "planned_tools": ["mcp__hashi-tools__hashi_file_read"],
                                            "do_not_do": ["do not modify the file"],
                                            "assurance": {
                                                "review_strategy": ["review tool evidence before answering"],
                                                "review_interval_tool_results": 6,
                                                "review_triggers": ["tool failure or scope change"],
                                                "validation_strategy": ["validate the answer against the file-read result"],
                                                "finalization_reserve": 6,
                                                "critical_review_findings": [],
                                                "validation_evidence": [],
                                                "unverified_items": [],
                                            },
                                            "completed": [],
                                            "remaining_work": ["read and report"],
                                            "failures": [],
                                            "next_action": "read probe.txt",
                                        }
                                    )
                                },
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ]
            elif len([request for request in requests if request.get("tools")]) == 1:
                chunks = [
                    {
                        "id": "chatcmpl-1",
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call-1",
                                            "type": "function",
                                            "function": {
                                                "name": "mcp__hashi-tools__hashi_file_read",
                                                "arguments": json.dumps({"path": "probe.txt"}),
                                            },
                                        }
                                    ]
                                }
                            }
                        ],
                    },
                    {"id": "chatcmpl-1", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                ]
            else:
                chunks = [
                    {"id": "chatcmpl-2", "choices": [{"delta": {"content": "gateway result received"}}]},
                    {"id": "chatcmpl-2", "choices": [{"delta": {}, "finish_reason": "stop"}]},
                ]
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        project_root = Path(__file__).resolve().parents[1]
        global_config = SimpleNamespace(
            project_root=project_root,
            claw_providers=_her_e2e_runtime_provider(),
        )
        cfg = SimpleNamespace(
            name="probe",
            workspace_dir=tmp_path,
            model="deepseek/deepseek-v4-pro",
            extra={},
            resolve_access_root=lambda: tmp_path,
        )
        adapter = ClawCLIAdapter(cfg, global_config, api_key="test")
        adapter.tool_registry = ToolRegistry(
            ["file_read", "browser_get_text", "browser_click"],
            tmp_path,
            tmp_path,
            {},
            max_loops=4,
            audit_context={"agent_name": "probe"},
        )
        assert asyncio.run(adapter.initialize()) is True
        binary = adapter._binary
        assert binary is not None
        env = adapter._task_env()
        env["OPENAI_BASE_URL"] = f"http://127.0.0.1:{server.server_port}/v1"
        env["OPENAI_API_KEY"] = "test"
        completed = subprocess.run(
            [
                str(binary),
                "--model",
                cfg.model,
                "--permission-mode",
                "danger-full-access",
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "prompt",
                "Read probe.txt with the HASHI file tool and answer.",
            ],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    requested_tool_names = [tool["function"]["name"] for tool in requests[1].get("tools", [])]
    assert "mcp__hashi-tools__hashi_file_read" in requested_tool_names, requested_tool_names
    assert payload["message"] == "gateway result received"
    assert payload["session_id"]
    assert any(
        tool.get("name") == "mcp__hashi-tools__hashi_file_read"
        for tool in payload["tool_uses"]
    )
    assert len(requests) == 3
    final_messages = requests[2]["messages"]
    assert any("browser gateway works" in str(message) for message in final_messages), final_messages


def test_packaged_her_bridges_media_read_image_into_provider_vision_input(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "workspace"
    media_root = tmp_path / "media" / "probe"
    workspace.mkdir()
    media_root.mkdir(parents=True)
    photo = media_root / "photo.png"
    Image.new("RGB", (24, 16), "teal").save(photo)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            requests.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            tool_requests = [request for request in requests if request.get("tools")]
            if not body.get("tools"):
                chunks = [
                    {
                        "id": "chatcmpl-plan",
                        "choices": [
                            {
                                "delta": {
                                    "content": json.dumps(
                                        {
                                            "acknowledgement": "I will inspect the supplied local image through the HASHI media tool.",
                                            "active_goal": "Inspect the supplied image and report what is visible.",
                                            "success_criteria": ["use model-visible image evidence"],
                                            "planned_actions": ["read the media", "report the result"],
                                            "planned_tools": ["mcp__hashi-tools__media_read"],
                                            "do_not_do": ["do not modify the image"],
                                            "assurance": {
                                                "review_strategy": ["review the visual tool result"],
                                                "review_interval_tool_results": 6,
                                                "review_triggers": ["tool failure or scope change"],
                                                "validation_strategy": ["validate the answer against visual input"],
                                                "finalization_reserve": 6,
                                                "critical_review_findings": [],
                                                "validation_evidence": [],
                                                "unverified_items": [],
                                            },
                                            "completed": [],
                                            "remaining_work": ["inspect and report"],
                                            "failures": [],
                                            "next_action": "read the image",
                                        }
                                    )
                                },
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ]
            elif len(tool_requests) == 1:
                chunks = [
                    {
                        "id": "chatcmpl-media-tool",
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call-media",
                                            "type": "function",
                                            "function": {
                                                "name": "mcp__hashi-tools__media_read",
                                                "arguments": json.dumps(
                                                    {"path": str(photo), "ocr_mode": "off"}
                                                ),
                                            },
                                        }
                                    ]
                                }
                            }
                        ],
                    },
                    {
                        "id": "chatcmpl-media-tool",
                        "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                    },
                ]
            else:
                chunks = [
                    {
                        "id": "chatcmpl-media-final",
                        "choices": [{"delta": {"content": "vision payload received"}}],
                    },
                    {
                        "id": "chatcmpl-media-final",
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                    },
                ]
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        global_config = SimpleNamespace(
            project_root=project_root,
            base_media_dir=tmp_path / "media",
            claw_providers=_her_e2e_runtime_provider(),
        )
        cfg = SimpleNamespace(
            name="probe",
            workspace_dir=workspace,
            model="openai/gpt-4o",
            extra={},
            resolve_access_root=lambda: workspace,
        )
        adapter = ClawCLIAdapter(cfg, global_config, api_key="test")
        adapter.tool_registry = ToolRegistry(
            ["file_read"],
            workspace,
            workspace,
            {},
            max_loops=4,
            audit_context={"agent_name": "probe"},
        )
        assert asyncio.run(adapter.initialize()) is True
        context = load_gateway_context(workspace / "backend_state" / "her_gateway_context.json")
        assert "media_read" in context.allowed_tools
        assert context.media_roots == [str(media_root.resolve())]
        binary = adapter._binary
        assert binary is not None
        env = adapter._task_env()
        env["OPENAI_BASE_URL"] = f"http://127.0.0.1:{server.server_port}/v1"
        env["OPENAI_API_KEY"] = "test"
        completed = subprocess.run(
            [
                str(binary),
                "--model",
                cfg.model,
                "--permission-mode",
                "danger-full-access",
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "prompt",
                f"Use media_read to inspect {photo} and answer.",
            ],
            cwd=workspace,
            env=env,
            text=True,
            capture_output=True,
            timeout=45,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["message"] == "vision payload received"
    assert len(requests) == 3
    requested_tool_names = [tool["function"]["name"] for tool in requests[1].get("tools", [])]
    assert "mcp__hashi-tools__media_read" in requested_tool_names

    final_messages = requests[2]["messages"]
    tool_message = next(
        message
        for message in final_messages
        if message.get("role") == "tool" and message.get("tool_call_id") == "call-media"
    )
    assert "image attached separately" in tool_message["content"]
    assert "base64," not in tool_message["content"]
    image_parts = [
        part
        for message in final_messages
        if message.get("role") == "user" and isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") == "image_url"
    ]
    assert len(image_parts) == 1
    data_url = image_parts[0]["image_url"]["url"]
    assert data_url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(data_url.partition(",")[2]).startswith(b"\xff\xd8\xff")
    audit_text = (workspace / "tool_action_audit.jsonl").read_text(encoding="utf-8")
    assert data_url.partition(",")[2] not in audit_text
