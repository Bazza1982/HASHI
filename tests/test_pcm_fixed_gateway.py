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

        # Result paging and catalog are read-only auxiliaries attached to an
        # already granted tool set; they do not grant a new resource root.
        assert set(context.allowed_tools) == {
            "file_read", "memory_search", "tool_catalog", "tool_output_read"
        }
        assert Path(context.workspace_dir) == registry.workspace_dir.resolve()
        assert Path(context.access_root) == registry.access_root.resolve()
        assert context.enforce_legacy_limits is False
        # Result paging is visible only when this invocation has a Run binding.
        assert set(descriptor["exposed_tools"]) == {
            "hashi_file_read", "memory_search", "tool_catalog"
        }
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
            assert "tool_output_read" in invocation["exposed_tools"]
            assert set(invocation["exposed_tools"]) == {
                item["name"] for item in ToolGateway(context).tool_definitions()
            }
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


@pytest.mark.asyncio
async def test_fixed_manager_queues_in_order_and_cancelled_waiter_never_dispatches(
    tmp_path, monkeypatch
):
    global_config = _global(tmp_path)
    registry = _registry(tmp_path, global_config)
    monkeypatch.setattr(hashi_mcp, "MAX_ACTIVE_INVOCATIONS_PER_ADAPTER", 1)
    hold = asyncio.Event()
    entered = asyncio.Event()
    dispatched = []

    class Backend:
        _hashi_mcp_enabled = True

        def __init__(self):
            self.tool_registry = registry
            self.config = SimpleNamespace(workspace_dir=tmp_path)
            self.global_config = global_config

        async def generate_response(self, _prompt, request_id, **_kwargs):
            invocation = current_hashi_mcp_invocation(self)
            assert invocation is not None
            context = load_gateway_context(Path(invocation["context_path"]))
            assert context.audit["request_id"] == request_id
            dispatched.append(request_id)
            if request_id == "req-a":
                entered.set()
                await hold.wait()
            return request_id

    backend = Backend()
    manager = FlexibleBackendManager.__new__(FlexibleBackendManager)
    manager.config = SimpleNamespace(active_backend="codex-cli")
    manager.current_backend = backend
    manager.runtime = SimpleNamespace(
        name="rika", session_store=None,
        _request_meta_by_id={
            name: {"request_id": name, "request_metadata": {}}
            for name in ("req-a", "req-b", "req-c", "req-d")
        },
        current_request_meta=None,
    )
    tasks = []
    try:
        first = asyncio.create_task(manager.generate_response("A", "req-a"))
        tasks.append(first)
        await asyncio.wait_for(entered.wait(), timeout=2)
        for request_id in ("req-b", "req-c", "req-d"):
            tasks.append(asyncio.create_task(manager.generate_response(request_id, request_id)))
            await asyncio.sleep(0)
        assert not any(task.done() for task in tasks[1:])
        tasks[2].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[2]
        hold.set()
        assert await asyncio.gather(tasks[0], tasks[1], tasks[3]) == [
            "req-a", "req-b", "req-d"
        ]
        assert dispatched == ["req-a", "req-b", "req-d"]
        assert backend._active_hashi_mcp_invocations == {}
        assert not list((tmp_path / "backend_state").glob("*-hashi-mcp-*.json"))
    finally:
        hold.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_fixed_manager_reports_bounded_queue_pressure_and_resumes(tmp_path, monkeypatch):
    global_config = _global(tmp_path)
    registry = _registry(tmp_path, global_config)
    monkeypatch.setenv("HASHI_MCP_MAX_ACTIVE_INVOCATIONS", "2")
    monkeypatch.setenv("HASHI_MCP_MAX_QUEUED_INVOCATIONS", "1")
    release = asyncio.Event()
    both_entered = asyncio.Event()
    dispatched = []

    class Backend:
        _hashi_mcp_enabled = True

        def __init__(self):
            self.tool_registry = registry
            self.config = SimpleNamespace(workspace_dir=tmp_path)
            self.global_config = global_config

        async def generate_response(self, _prompt, request_id, **_kwargs):
            dispatched.append(request_id)
            if len(dispatched) == 2:
                both_entered.set()
            if request_id in {"req-a", "req-b"}:
                await release.wait()
            return request_id

    backend = Backend()
    manager = FlexibleBackendManager.__new__(FlexibleBackendManager)
    manager.config = SimpleNamespace(active_backend="codex-cli")
    manager.current_backend = backend
    manager.runtime = SimpleNamespace(
        name="rika", session_store=None,
        _request_meta_by_id={
            name: {"request_id": name, "request_metadata": {}}
            for name in ("req-a", "req-b", "req-c", "req-d")
        },
        current_request_meta=None,
    )
    tasks = []
    try:
        for name in ("req-a", "req-b"):
            tasks.append(asyncio.create_task(manager.generate_response(name, name)))
        await asyncio.wait_for(both_entered.wait(), 2)
        queued = asyncio.create_task(manager.generate_response("C", "req-c"))
        tasks.append(queued)
        await asyncio.sleep(0)
        pressure = await manager.generate_response("D", "req-d")
        assert pressure.is_success is False
        assert pressure.error_code == "MCP_CAPACITY_BUSY"
        assert pressure.error_retryable is True
        assert pressure.stream_metadata["mcp_capacity"] == {
            "active": 2, "reserved": 0, "waiting": 1, "active_limit": 2, "queue_limit": 1
        }
        assert dispatched == ["req-a", "req-b"]
        release.set()
        assert await asyncio.gather(*tasks) == ["req-a", "req-b", "req-c"]
        assert dispatched == ["req-a", "req-b", "req-c"]
        assert backend._active_hashi_mcp_invocations == {}
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_fixed_manager_does_not_dispatch_waiter_after_backend_switch(tmp_path, monkeypatch):
    global_config = _global(tmp_path)
    registry = _registry(tmp_path, global_config)
    monkeypatch.setattr(hashi_mcp, "MAX_ACTIVE_INVOCATIONS_PER_ADAPTER", 1)
    release = asyncio.Event()
    entered = asyncio.Event()
    dispatched = []

    class Backend:
        _hashi_mcp_enabled = True

        def __init__(self):
            self.tool_registry = registry
            self.config = SimpleNamespace(workspace_dir=tmp_path)
            self.global_config = global_config

        async def generate_response(self, _prompt, request_id, **_kwargs):
            dispatched.append(request_id)
            entered.set()
            await release.wait()
            return request_id

    backend = Backend()
    manager = FlexibleBackendManager.__new__(FlexibleBackendManager)
    manager.config = SimpleNamespace(active_backend="codex-cli")
    manager.current_backend = backend
    manager.runtime = SimpleNamespace(
        name="rika", session_store=None,
        _request_meta_by_id={
            name: {"request_id": name, "request_metadata": {}}
            for name in ("req-a", "req-b")
        },
        current_request_meta=None,
    )
    first = asyncio.create_task(manager.generate_response("A", "req-a"))
    queued = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        queued = asyncio.create_task(manager.generate_response("B", "req-b"))
        await asyncio.sleep(0)
        manager.current_backend = Backend()
        release.set()
        assert await first == "req-a"
        result = await queued
        assert result.is_success is False
        assert result.error_code == "MCP_BACKEND_CHANGED"
        assert result.error_retryable is True
        assert dispatched == ["req-a"]
        assert backend._active_hashi_mcp_invocations == {}
    finally:
        release.set()
        await asyncio.gather(*(task for task in (first, queued) if task), return_exceptions=True)


@pytest.mark.asyncio
async def test_seventeenth_fixed_cli_waits_and_then_dispatches_once(tmp_path, monkeypatch):
    monkeypatch.delenv("HASHI_MCP_MAX_ACTIVE_INVOCATIONS", raising=False)
    monkeypatch.delenv("HASHI_MCP_MAX_QUEUED_INVOCATIONS", raising=False)
    global_config = _global(tmp_path)
    registry = _registry(tmp_path, global_config)
    release = asyncio.Event()
    saturated = asyncio.Event()
    seen = []

    class Backend:
        _hashi_mcp_enabled = True

        def __init__(self):
            self.tool_registry = registry
            self.config = SimpleNamespace(workspace_dir=tmp_path)
            self.global_config = global_config

        async def generate_response(self, _prompt, request_id, **_kwargs):
            invocation = current_hashi_mcp_invocation(self)
            assert invocation is not None
            assert load_gateway_context(Path(invocation["context_path"])).audit["request_id"] == request_id
            seen.append(request_id)
            if len(seen) == 16:
                saturated.set()
            await release.wait()
            return request_id

    backend = Backend()
    manager = FlexibleBackendManager.__new__(FlexibleBackendManager)
    manager.config = SimpleNamespace(active_backend="codex-cli")
    manager.current_backend = backend
    manager.runtime = SimpleNamespace(
        name="rika", session_store=None,
        _request_meta_by_id={
            f"req-{index}": {"request_id": f"req-{index}", "request_metadata": {}}
            for index in range(17)
        },
        current_request_meta=None,
    )
    tasks = []
    try:
        tasks = [
            asyncio.create_task(manager.generate_response(str(index), f"req-{index}"))
            for index in range(16)
        ]
        await asyncio.wait_for(saturated.wait(), 5)
        seventeenth = asyncio.create_task(manager.generate_response("16", "req-16"))
        tasks.append(seventeenth)
        await asyncio.sleep(0)
        assert not seventeenth.done()
        assert len(seen) == 16
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert results == [f"req-{index}" for index in range(17)]
        assert seen.count("req-16") == 1
        assert backend._active_hashi_mcp_invocations == {}
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_after_queue_slot_grant_returns_capacity(monkeypatch):
    monkeypatch.setattr(hashi_mcp, "MAX_ACTIVE_INVOCATIONS_PER_ADAPTER", 1)
    owner = SimpleNamespace()
    first = await hashi_mcp.reserve_hashi_mcp_capacity(owner)
    waiter = asyncio.create_task(hashi_mcp.reserve_hashi_mcp_capacity(owner))
    await asyncio.sleep(0)
    assert first.snapshot()["waiting"] == 1
    first.release_reservation()
    # Cancel after the future received its slot but before its coroutine runs.
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert first.snapshot()["reserved"] == 0
    assert first.snapshot()["waiting"] == 0
    next_slot = await asyncio.wait_for(hashi_mcp.reserve_hashi_mcp_capacity(owner), 1)
    next_slot.release_reservation()
