"""Per-invocation HASHI Tool Gateway configuration for supported Fixed CLIs."""

from __future__ import annotations

import json
import asyncio
from contextvars import ContextVar, Token
import os
import sys
import tempfile
from uuid import uuid4
from pathlib import Path
from typing import Any, Mapping

from tools.gateway.context import live_workbench_api_base_url, write_gateway_context
from tools.gateway.mcp_stdio import exposed_tool_name
from orchestrator.file_permissions import tighten_fd_permissions


SERVER_NAME = "hashi_tools"
MAX_ACTIVE_INVOCATIONS_PER_ADAPTER = 16


class HashiMcpInvocation(dict[str, Any]):
    """One Fixed CLI invocation's owner-only gateway files and token."""

    def __init__(self, *args, owner=None, proxy=None, proxy_token: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._proxy = proxy
        self._owner = owner
        self._proxy_token = str(proxy_token or "")
        self._cleanup_paths: set[Path] = {Path(self["context_path"])}
        self._context_var: ContextVar | None = None
        self._context_token: Token | None = None
        self._closed = False

    def bind(self, context_var: ContextVar) -> None:
        self._context_var = context_var
        self._context_token = context_var.set(self)

    def add_cleanup_path(self, path: Path) -> None:
        self._cleanup_paths.add(Path(path))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        cleanup_error: Exception | None = None
        try:
            if self._context_var is not None and self._context_token is not None:
                self._context_var.reset(self._context_token)
        except Exception as exc:  # Preserve cleanup even if the caller crosses contexts.
            cleanup_error = exc
        try:
            if self._proxy is not None and self._proxy_token:
                self._proxy.revoke(self._proxy_token)
        except Exception as exc:
            cleanup_error = cleanup_error or exc
        for path in self._cleanup_paths:
            try:
                path.unlink(missing_ok=True)
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        active = getattr(self._owner, "_active_hashi_mcp_invocations", None)
        if isinstance(active, dict):
            active.pop(id(self), None)
        if cleanup_error is not None:
            raise cleanup_error


def _descriptor_context(adapter: Any) -> ContextVar:
    context = getattr(adapter, "_hashi_mcp_invocation_context", None)
    if context is None:
        context = ContextVar(f"hashi_mcp_invocation_{id(adapter)}", default=None)
        adapter._hashi_mcp_invocation_context = context
    return context


def current_hashi_mcp_invocation(adapter: Any) -> HashiMcpInvocation | None:
    return _descriptor_context(adapter).get()


def prepare_hashi_mcp(
    adapter: Any,
    *,
    backend: str,
    audit_context: Mapping[str, Any] | None = None,
) -> HashiMcpInvocation | None:
    registry = getattr(adapter, "tool_registry", None)
    if registry is None or not registry.allowed_tool_names():
        adapter._hashi_mcp_enabled = False
        return None
    active = getattr(adapter, "_active_hashi_mcp_invocations", None)
    if not isinstance(active, dict):
        active = {}
        adapter._active_hashi_mcp_invocations = active
    if len(active) >= MAX_ACTIVE_INVOCATIONS_PER_ADAPTER:
        raise RuntimeError("Fixed CLI has too many active HASHI MCP invocations")
    state_dir = Path(adapter.config.workspace_dir) / "backend_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    context_path = state_dir / f"{backend}-hashi-mcp-{uuid4().hex}.json"
    request_audit = dict(
        audit_context if audit_context is not None else registry.audit_context or {}
    )
    request_audit.pop("browser_gateway_proxy", None)
    runtime = request_audit.get("_runtime")
    facade = getattr(runtime, "orchestrator", None)
    proxy = None
    proxy_descriptor = None
    if (
        any(name.startswith("browser_") for name in registry.allowed_tool_names())
        and bool(getattr(facade, "is_function_worker_facade", False))
        and callable(getattr(facade, "invoke_capability", None))
    ):
        from tools.browser_gateway_proxy import BrowserGatewayProxy

        proxy = getattr(adapter, "_browser_gateway_proxy", None)
        if proxy is None:
            proxy = BrowserGatewayProxy(registry, asyncio.get_running_loop())
            adapter._browser_gateway_proxy = proxy
        proxy_descriptor = proxy.issue(request_audit)
        request_audit["browser_gateway_proxy"] = proxy_descriptor
    else:
        request_audit.pop("browser_gateway_proxy", None)
    descriptor: HashiMcpInvocation | None = None
    try:
        workbench_url = live_workbench_api_base_url(
            registry,
            adapter.global_config,
            audit_context=request_audit,
        )
        write_gateway_context(
            registry,
            context_path,
            workbench_api_base_url=workbench_url,
            backend=backend,
            audit_context=request_audit,
        )
        project_root = Path(adapter.global_config.project_root).resolve()
        server_script = project_root / "tools" / "gateway" / "mcp_stdio.py"
        if not server_script.is_file():
            raise FileNotFoundError(f"HASHI MCP gateway is missing: {server_script}")
        descriptor = HashiMcpInvocation(
            {
                "name": SERVER_NAME,
                "command": sys.executable,
                "args": [
                    "-m",
                    "tools.gateway.mcp_stdio",
                    "--context",
                    str(context_path),
                ],
                "cwd": str(project_root),
                "context_path": str(context_path),
                "exposed_tools": [
                    exposed_tool_name(name) for name in registry.allowed_tool_names()
                ],
            },
            owner=adapter,
            proxy=proxy,
            proxy_token=(proxy_descriptor or {}).get("token", ""),
        )
        active[id(descriptor)] = descriptor
        descriptor.bind(_descriptor_context(adapter))
        adapter._hashi_mcp_enabled = True
        return descriptor
    except Exception as exc:
        if descriptor is not None:
            try:
                descriptor.close()
            except Exception as cleanup_exc:
                exc.add_note(
                    "HASHI MCP invocation cleanup also failed: "
                    f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                )
        else:
            context_path.unlink(missing_ok=True)
            if proxy is not None and proxy_descriptor is not None:
                proxy.revoke(proxy_descriptor.get("token", ""))
        raise


def write_claude_mcp_config(adapter: Any, descriptor: dict[str, Any]) -> Path:
    target = (
        Path(adapter.config.workspace_dir)
        / "backend_state"
        / f"claude-hashi-mcp-{uuid4().hex}.json"
    )
    payload = {
        "mcpServers": {
            descriptor["name"]: {
                "command": descriptor["command"],
                "args": descriptor["args"],
                "cwd": descriptor["cwd"],
            }
        }
    }
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        tighten_fd_permissions(fd)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        if isinstance(descriptor, HashiMcpInvocation):
            descriptor.add_cleanup_path(target)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return target


__all__ = [
    "HashiMcpInvocation",
    "current_hashi_mcp_invocation",
    "prepare_hashi_mcp",
    "write_claude_mcp_config",
]
