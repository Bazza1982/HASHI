from __future__ import annotations

import asyncio
import io
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.claw_cli import ClawCLIAdapter
from tools.gateway.context import load_gateway_context, write_gateway_context
from tools.gateway.mcp_stdio import ToolGateway, _dispatch, _read_frame, _write_frame
from tools.registry import ToolRegistry


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
    assert context.max_calls == 8
    loaded = load_gateway_context(path)
    assert loaded.build_registry().is_allowed("browser_click")
    assert "global_config" not in loaded.audit


def test_gateway_context_rejects_group_readable_secret_snapshot(tmp_path):
    path = tmp_path / "context.json"
    write_gateway_context(_registry(tmp_path), path)
    path.chmod(0o640)

    with pytest.raises(PermissionError, match="owner-only"):
        load_gateway_context(path)


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


def _write_context(registry: ToolRegistry, tmp_path: Path) -> Path:
    path = tmp_path / "context.json"
    write_gateway_context(registry, path)
    return path


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
        result = await gateway.call("file_read", arguments, str(number))
        assert result["isError"] is False

    stopped = await gateway.call("file_read", arguments, "last")
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
                                            "planned_tools": ["mcp__hashi-tools__file_read"],
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
                                                "name": "mcp__hashi-tools__file_read",
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
            claw_providers={"runtime_policy": "require-packaged"},
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
    assert "mcp__hashi-tools__file_read" in requested_tool_names, requested_tool_names
    assert payload["message"] == "gateway result received"
    assert payload["session_id"]
    assert any(tool.get("name") == "mcp__hashi-tools__file_read" for tool in payload["tool_uses"])
    assert len(requests) == 3
    final_messages = requests[2]["messages"]
    assert any("browser gateway works" in str(message) for message in final_messages), final_messages
