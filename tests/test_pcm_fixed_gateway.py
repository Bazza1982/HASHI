from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

import pytest

import adapters.hashi_mcp as hashi_mcp
from adapters.codex_cli import CodexCLIAdapter
from adapters.hashi_mcp import (
    current_hashi_mcp_invocation,
    prepare_hashi_mcp,
    write_claude_mcp_config,
)
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from tools.gateway.context import load_gateway_context
from tools.gateway.mcp_stdio import ToolGateway, _read_frame, _write_frame
from tools.registry import ToolRegistry


def _global(tmp_path):
    return SimpleNamespace(
        project_root=Path(__file__).resolve().parents[1],
        bridge_home=tmp_path,
        instance_id="HASHI2",
        api_host="127.0.0.1",
        workbench_port=18800,
        central_memory={},
        wiki_provider={},
        codex_cmd="codex",
    )


def _registry(tmp_path, global_config):
    access_root = tmp_path / "access"
    workspace_dir = access_root / "workzone"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    def resolve_service_endpoint(service, *, expected_instance=None):
        assert service == "workbench"
        assert expected_instance == global_config.instance_id
        return {
            "service": service,
            "instance_id": global_config.instance_id,
            "base_url": f"http://127.0.0.1:{global_config.workbench_port}",
        }

    return ToolRegistry(
        allowed_tools=["file_read", "memory_search"],
        access_root=access_root,
        workspace_dir=workspace_dir,
        secrets={},
        audit_context={
            "agent_name": "rika",
            "global_config": global_config,
            "_kernel": SimpleNamespace(resolve_service_endpoint=resolve_service_endpoint),
        },
    )


def test_fixed_cli_gateway_context_matches_registry_workzone_and_permissions(tmp_path):
    global_config = _global(tmp_path)
    registry = _registry(tmp_path, global_config)
    adapter = SimpleNamespace(
        config=SimpleNamespace(workspace_dir=tmp_path),
        global_config=global_config,
        tool_registry=registry,
    )

    descriptor = prepare_hashi_mcp(adapter, backend="codex-cli")
    try:
        context = load_gateway_context(Path(descriptor["context_path"]))

        assert context.allowed_tools == ["file_read", "memory_search"]
        assert Path(context.workspace_dir) == registry.workspace_dir.resolve()
        assert Path(context.access_root) == registry.access_root.resolve()
        assert context.enforce_legacy_limits is False
        assert set(descriptor["exposed_tools"]) == {"hashi_file_read", "memory_search"}
    finally:
        descriptor.close()


def test_codex_fixed_command_connects_only_the_per_invocation_hashi_gateway(tmp_path):
    config = SimpleNamespace(
        name="rika",
        model="gpt-5.4",
        workspace_dir=tmp_path,
        system_md=tmp_path / "agent.md",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = CodexCLIAdapter(config, _global(tmp_path))
    adapter.tool_registry = _registry(tmp_path, adapter.global_config)
    adapter._hashi_mcp_enabled = True
    adapter._external_mcp_server_names = ("github", "openaiDeveloperDocs")
    descriptor = prepare_hashi_mcp(adapter, backend="codex-cli")
    try:
        command = adapter._build_cmd("prompt", tmp_path / "last.txt")
    finally:
        descriptor.close()

    overrides = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "-c" and command[index + 1].startswith("mcp_servers.")
    ]
    assert len(overrides) == 3
    assert overrides[:2] == [
        "mcp_servers.github.enabled=false",
        "mcp_servers.openaiDeveloperDocs.enabled=false",
    ]
    assert overrides[2].startswith("mcp_servers.hashi_tools={command=")
    assert "args=" in overrides[2] and "cwd=" in overrides[2]
    assert "enabled=true" in overrides[2]
    assert "required=true" in overrides[2]
    assert command.count("--disable") == 7
    assert 'web_search="disabled"' in command


def test_codex_fixed_resume_requires_the_per_invocation_hashi_gateway(tmp_path):
    config = SimpleNamespace(
        name="rika",
        model="gpt-5.4",
        workspace_dir=tmp_path,
        system_md=tmp_path / "agent.md",
        extra={"session_mode": True},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = CodexCLIAdapter(config, _global(tmp_path))
    adapter.tool_registry = _registry(tmp_path, adapter.global_config)
    adapter._hashi_mcp_enabled = True
    adapter._external_mcp_server_names = ()
    adapter._session_id = "thread-existing"
    descriptor = prepare_hashi_mcp(adapter, backend="codex-cli")
    try:
        command = adapter._build_cmd("prompt", tmp_path / "last.txt")
    finally:
        descriptor.close()

    assert command[1:4] == ["exec", "resume", "thread-existing"]
    gateway_overrides = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "-c"
        and command[index + 1].startswith("mcp_servers.hashi_tools=")
    ]
    assert len(gateway_overrides) == 1
    assert "enabled=true" in gateway_overrides[0]
    assert "required=true" in gateway_overrides[0]


def test_codex_fixed_command_rejects_missing_request_gateway(tmp_path):
    config = SimpleNamespace(
        name="rika",
        model="gpt-5.4",
        workspace_dir=tmp_path,
        system_md=tmp_path / "agent.md",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = CodexCLIAdapter(config, _global(tmp_path))
    adapter._hashi_mcp_enabled = True

    with pytest.raises(RuntimeError, match="request-scoped invocation is missing"):
        adapter._build_cmd("prompt", tmp_path / "last.txt")


def test_claude_fixed_config_is_owner_only_and_strictly_scoped(tmp_path):
    descriptor = {
        "name": "hashi_tools",
        "command": "/usr/bin/python",
        "args": ["-m", "tools.gateway.mcp_stdio", "--context", "/tmp/context.json"],
        "cwd": "/repo",
    }
    adapter = SimpleNamespace(config=SimpleNamespace(workspace_dir=tmp_path))
    (tmp_path / "backend_state").mkdir()
    path = write_claude_mcp_config(adapter, descriptor)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert list(payload["mcpServers"]) == ["hashi_tools"]
    assert payload["mcpServers"]["hashi_tools"]["cwd"] == "/repo"
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_invocation_cleanup_revokes_unique_context_and_claude_config_files(tmp_path):
    global_config = _global(tmp_path)
    adapter = SimpleNamespace(
        config=SimpleNamespace(workspace_dir=tmp_path),
        global_config=global_config,
        tool_registry=_registry(tmp_path, global_config),
    )
    first = prepare_hashi_mcp(
        adapter,
        backend="claude-cli",
        audit_context={**adapter.tool_registry.audit_context, "request_id": "req-a"},
    )
    second = prepare_hashi_mcp(
        adapter,
        backend="claude-cli",
        audit_context={**adapter.tool_registry.audit_context, "request_id": "req-b"},
    )
    first_config = write_claude_mcp_config(adapter, first)
    second_config = write_claude_mcp_config(adapter, second)
    paths = {
        Path(first["context_path"]),
        Path(second["context_path"]),
        first_config,
        second_config,
    }

    assert len(paths) == 4
    assert all(path.exists() for path in paths)
    assert len(adapter._active_hashi_mcp_invocations) == 2
    second.close()
    first.close()
    assert all(not path.exists() for path in paths)
    assert adapter._active_hashi_mcp_invocations == {}


def test_invocation_limit_is_fail_closed_and_cleanup_reopens_capacity(
    tmp_path, monkeypatch
):
    global_config = _global(tmp_path)
    adapter = SimpleNamespace(
        config=SimpleNamespace(workspace_dir=tmp_path),
        global_config=global_config,
        tool_registry=_registry(tmp_path, global_config),
    )
    monkeypatch.setattr(hashi_mcp, "MAX_ACTIVE_INVOCATIONS_PER_ADAPTER", 1)
    first = prepare_hashi_mcp(adapter, backend="codex-cli")
    with pytest.raises(RuntimeError, match="too many active HASHI MCP invocations"):
        prepare_hashi_mcp(adapter, backend="codex-cli")
    first.close()
    second = prepare_hashi_mcp(adapter, backend="codex-cli")
    second.close()


def test_modern_mcp_jsonl_and_legacy_content_length_framing_are_both_supported():
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    jsonl = io.BytesIO(json.dumps(request).encode() + b"\n")
    parsed = _read_frame(jsonl)
    assert parsed.pop("_hashi_stdio_transport") == "jsonl"
    assert parsed == request

    output = io.BytesIO()
    _write_frame(output, {"jsonrpc": "2.0", "id": 1, "result": {}}, transport="jsonl")
    assert output.getvalue().endswith(b"\n")

    body = json.dumps(request).encode()
    legacy = io.BytesIO(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    assert _read_frame(legacy) == request


def test_gateway_catalogue_exactly_matches_authorised_exposed_tools(tmp_path):
    global_config = _global(tmp_path)
    registry = _registry(tmp_path, global_config)
    adapter = SimpleNamespace(
        config=SimpleNamespace(workspace_dir=tmp_path),
        global_config=global_config,
        tool_registry=registry,
    )
    descriptor = prepare_hashi_mcp(adapter, backend="claude-cli")
    gateway = ToolGateway(load_gateway_context(Path(descriptor["context_path"])))
    assert {item["name"] for item in gateway.tool_definitions()} == set(
        descriptor["exposed_tools"]
    )


@pytest.mark.asyncio
async def test_fixed_gateway_executes_authorised_tool_inside_shared_workzone(tmp_path):
    global_config = _global(tmp_path)
    registry = _registry(tmp_path, global_config)
    source = registry.workspace_dir / "sample.txt"
    source.write_text("gateway evidence", encoding="utf-8")
    adapter = SimpleNamespace(
        config=SimpleNamespace(workspace_dir=tmp_path),
        global_config=global_config,
        tool_registry=registry,
    )
    descriptor = prepare_hashi_mcp(adapter, backend="codex-cli")
    gateway = ToolGateway(load_gateway_context(Path(descriptor["context_path"])))

    result = await gateway.call(
        "hashi_file_read",
        {"path": "sample.txt"},
        "call-1",
    )

    assert result["isError"] is False
    assert "gateway evidence" in result["content"][0]["text"]


def test_grok_never_advertises_registry_tools_without_isolated_gateway(tmp_path):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.config = SimpleNamespace(active_backend="grok-cli")
    runtime.backend_manager = SimpleNamespace(
        current_backend=SimpleNamespace(
            tool_registry=_registry(tmp_path, _global(tmp_path))
        )
    )

    assert runtime._get_available_tool_catalogue() == []


def test_fixed_gateway_context_is_refreshed_with_request_bound_authority(
    tmp_path, monkeypatch
):
    global_config = _global(tmp_path)
    registry = _registry(tmp_path, global_config)
    backend = SimpleNamespace(
        tool_registry=registry,
        _hashi_mcp_enabled=True,
    )
    manager = FlexibleBackendManager.__new__(FlexibleBackendManager)
    manager.config = SimpleNamespace(active_backend="codex-cli")
    manager.current_backend = backend
    manager.runtime = SimpleNamespace(
        name="rika",
        session_store=SimpleNamespace(
            db_path=tmp_path / "state" / "sessions.sqlite3",
            instance_id="HASHI3",
            attachment_files_root=tmp_path / "media" / "session_attachments",
        ),
        _request_meta_by_id={
            "req-1": {
                "request_id": "req-1",
                "chat_id": 9,
                "source": "memory:raw-search",
                "summary": "raw search",
                "hashi_session_id": "session-1",
                "hashi_run_id": "run-1",
                "owner_id": "user:7",
                "session_surface": "generic-desktop",
                "session_channel_key": "client-1",
                "request_metadata": {
                    "tool_allowlist": ["memory_search"],
                    "memory_search_authorization": {
                        "authorization": "explicit_user_authorization",
                        "instance_id": "HASHI2",
                        "agent_id": "arale",
                        "purpose": "bound purpose",
                    }
                },
            }
        },
        current_request_meta=None,
    )
    refresh = Mock(return_value={})
    monkeypatch.setattr("adapters.hashi_mcp.prepare_hashi_mcp", refresh)

    manager._refresh_tool_runtime_context("req-1")

    assert registry.audit_context.get("request_id") is None
    request_context = refresh.call_args.kwargs["audit_context"]
    assert request_context["request_id"] == "req-1"
    assert request_context["hashi_session_id"] == "session-1"
    assert request_context["hashi_run_id"] == "run-1"
    assert request_context["owner_id"] == "user:7"
    assert request_context["session_surface"] == "generic-desktop"
    assert request_context["session_channel_key"] == "client-1"
    assert request_context["session_store_descriptor"] == {
        "db_path": str(tmp_path / "state" / "sessions.sqlite3"),
        "instance_id": "HASHI3",
        "attachment_root": str(tmp_path / "media" / "session_attachments"),
    }
    assert request_context["memory_search_authorization"]["agent_id"] == "arale"
    assert request_context["request_tool_allowlist"] == ["memory_search"]
    assert refresh.call_args.args == (backend,)
    assert refresh.call_args.kwargs["backend"] == "codex-cli"

    manager.runtime._request_meta_by_id["req-2"] = {
        "request_id": "req-2",
        "source": "text",
        "request_metadata": {},
    }
    manager._refresh_tool_runtime_context("req-2")
    second_context = refresh.call_args.kwargs["audit_context"]
    assert "memory_search_authorization" not in second_context


@pytest.mark.asyncio
async def test_fixed_manager_keeps_descriptor_request_scoped_across_await(tmp_path):
    global_config = _global(tmp_path)
    registry = _registry(tmp_path, global_config)
    observed = {}
    both_entered = asyncio.Event()
    entered = 0

    class Backend:
        _hashi_mcp_enabled = True

        def __init__(self):
            self.tool_registry = registry
            self.config = SimpleNamespace(workspace_dir=tmp_path)
            self.global_config = global_config

        async def generate_response(self, _prompt, request_id, **_kwargs):
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await both_entered.wait()
            invocation = current_hashi_mcp_invocation(self)
            context = load_gateway_context(Path(invocation["context_path"]))
            observed[request_id] = (context.audit["request_id"], Path(invocation["context_path"]))
            return request_id

    backend = Backend()
    manager = FlexibleBackendManager.__new__(FlexibleBackendManager)
    manager.config = SimpleNamespace(active_backend="codex-cli")
    manager.current_backend = backend
    manager.runtime = SimpleNamespace(
        name="rika",
        session_store=None,
        _request_meta_by_id={
            "req-a": {"request_id": "req-a", "request_metadata": {}},
            "req-b": {"request_id": "req-b", "request_metadata": {}},
        },
        current_request_meta=None,
    )

    results = await asyncio.gather(
        manager.generate_response("A", "req-a"),
        manager.generate_response("B", "req-b"),
    )

    assert results == ["req-a", "req-b"]
    assert observed["req-a"][0] == "req-a"
    assert observed["req-b"][0] == "req-b"
    assert observed["req-a"][1] != observed["req-b"][1]
    assert all(not path.exists() for _request, path in observed.values())
