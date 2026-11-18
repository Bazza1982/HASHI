"""Per-invocation HASHI Tool Gateway configuration for supported Fixed CLIs."""

from __future__ import annotations

import json
import hashlib
import asyncio
from collections import deque
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
MAX_QUEUED_INVOCATIONS_PER_ADAPTER = 128


def _capacity_limit(name: str, default: int, *, allow_zero: bool = False) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 or (allow_zero and value == 0) else default


def _mcp_limits() -> tuple[int, int]:
    return (
        _capacity_limit("HASHI_MCP_MAX_ACTIVE_INVOCATIONS", MAX_ACTIVE_INVOCATIONS_PER_ADAPTER),
        _capacity_limit("HASHI_MCP_MAX_QUEUED_INVOCATIONS", MAX_QUEUED_INVOCATIONS_PER_ADAPTER, allow_zero=True),
    )


class HashiMcpCapacityError(RuntimeError):
    """Bounded queue is full before an invocation has started."""

    def __init__(self, snapshot: dict[str, int]):
        super().__init__("HASHI MCP invocation queue is full; retry when capacity is available")
        self.snapshot = snapshot


class _HashiMcpAdmission:
    """Per-adapter FIFO reservation; actual Run authority is checked at dispatch."""

    def __init__(self, owner: Any):
        self.owner = owner
        self.loop = asyncio.get_running_loop()
        self.active_limit, self.queue_limit = _mcp_limits()
        self.reserved = 0
        self.waiters: deque[asyncio.Future[None]] = deque()

    def _active(self) -> int:
        active = getattr(self.owner, "_active_hashi_mcp_invocations", None)
        return len(active) if isinstance(active, dict) else 0

    def snapshot(self) -> dict[str, int]:
        return {"active": self._active(), "reserved": self.reserved,
                "waiting": len(self.waiters), "active_limit": self.active_limit,
                "queue_limit": self.queue_limit}

    def _drain(self) -> None:
        while self.waiters and self._active() + self.reserved < self.active_limit:
            waiter = self.waiters.popleft()
            if waiter.done():
                continue
            self.reserved += 1
            waiter.set_result(None)

    async def acquire(self) -> None:
        if not self.waiters and self._active() + self.reserved < self.active_limit:
            self.reserved += 1
            return
        if len(self.waiters) >= self.queue_limit:
            raise HashiMcpCapacityError(self.snapshot())
        waiter = self.loop.create_future()
        self.waiters.append(waiter)
        self._drain()
        try:
            await waiter
        except BaseException:
            if waiter.done() and not waiter.cancelled():
                self.reserved -= 1
            else:
                waiter.cancel()
                try:
                    self.waiters.remove(waiter)
                except ValueError:
                    pass
            self._drain()
            raise

    def release_reservation(self) -> None:
        if self.reserved <= 0:
            raise RuntimeError("HASHI MCP capacity reservation is missing")
        self.reserved -= 1
        self._drain()

    def notify_closed(self) -> None:
        try:
            if asyncio.get_running_loop() is self.loop:
                self._drain()
                return
        except RuntimeError:
            pass
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._drain)


async def reserve_hashi_mcp_capacity(adapter: Any) -> _HashiMcpAdmission:
    loop = asyncio.get_running_loop()
    admission = getattr(adapter, "_hashi_mcp_admission", None)
    if admission is not None and admission.loop is not loop:
        if admission._active() or admission.reserved or admission.waiters:
            raise RuntimeError("HASHI MCP adapter is already active on another event loop")
        admission = None
    if admission is None:
        admission = _HashiMcpAdmission(adapter)
        adapter._hashi_mcp_admission = admission
    await admission.acquire()
    return admission


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
        admission = getattr(self._owner, "_hashi_mcp_admission", None)
        if admission is not None:
            admission.notify_closed()
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


def _qualified_mcp_parent(adapter, registry, audit_context):
    from orchestrator.runtime_contract import (
        FUNCTIONS_ROLE, RuntimeContractError, current_process_runtime,
    )
    admitted = current_process_runtime()
    if admitted is None:
        audit = audit_context if audit_context is not None else registry.audit_context
        facade = getattr((audit or {}).get("_runtime"), "orchestrator", None)
        if type(facade).__module__ == "orchestrator.function_worker_host":
            raise RuntimeContractError("runtime_protection_context_unavailable: private Functions receipt missing")
        return None  # Existing standalone developer/legacy embedding, not an installed Worker.
    if admitted.fingerprint.role != FUNCTIONS_ROLE:
        raise RuntimeContractError(
            "runtime_integrity_failed: current process is not the admitted Functions role"
        )
    # The Worker already bound this exact private parent receipt during process
    # admission. Re-reading and recursively hashing the same installation for
    # every tool invocation proves no new process fact; the MCP child performs
    # its own fast admission before importing product tooling.
    return admitted


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
    qualified_parent = _qualified_mcp_parent(adapter, registry, audit_context)
    active = getattr(adapter, "_active_hashi_mcp_invocations", None)
    if not isinstance(active, dict):
        active = {}
        adapter._active_hashi_mcp_invocations = active
    admission = getattr(adapter, "_hashi_mcp_admission", None)
    active_limit = admission.active_limit if admission is not None else _mcp_limits()[0]
    if len(active) >= active_limit:
        raise RuntimeError("Fixed CLI has too many active HASHI MCP invocations")
    state_dir = Path(adapter.config.workspace_dir) / "backend_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    context_path = state_dir / f"{backend}-hashi-mcp-{uuid4().hex}.json"
    request_audit = dict(
        audit_context if audit_context is not None else registry.audit_context or {}
    )
    request_audit.pop("browser_gateway_proxy", None)
    request_audit.pop("tool_gateway_proxy", None)
    runtime = request_audit.get("_runtime")
    facade = getattr(runtime, "orchestrator", None)
    from orchestrator.runtime_contract import current_process_runtime
    owner_required = bool(getattr(facade, "is_function_worker_facade", False)
                          or current_process_runtime() is not None)
    proxy = None
    proxy_descriptor = None
    if owner_required:
        request_audit["tool_gateway_required"] = True
        # Adapter initialization prepares a catalogue without a PAO Run. Only
        # the refreshed actual invocation receives an executable capability.
        if request_audit.get("hashi_run_id"):
            from tools.browser_gateway_proxy import BrowserGatewayProxy, MAX_ACTIVE_SCOPES
            proxy = getattr(adapter, "_browser_gateway_proxy", None)
            if proxy is not None and proxy.registry is not registry:
                proxy.close()
                proxy = None
            if proxy is None:
                proxy = BrowserGatewayProxy(
                    registry, asyncio.get_running_loop(),
                    max_active_scopes=max(MAX_ACTIVE_SCOPES, active_limit),
                )
                adapter._browser_gateway_proxy = proxy
            proxy_descriptor = proxy.issue(request_audit, all_tools=True)
            request_audit["tool_gateway_proxy"] = proxy_descriptor
    descriptor: HashiMcpInvocation | None = None
    try:
        workbench_url = live_workbench_api_base_url(
            registry,
            adapter.global_config,
            audit_context=request_audit,
        )
        gateway_context = write_gateway_context(
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
        exposed_definitions = (
            gateway_context.owner_tool_definitions
            if owner_required else registry.get_tool_definitions_with_audit_context(request_audit)
        )
        descriptor = HashiMcpInvocation(
            {
                "name": SERVER_NAME,
                "command": sys.executable,
                "args": [
                    "-B",
                    "-m",
                    "tools.gateway.mcp_stdio",
                    "--context",
                    str(context_path),
                    *(
                        ["--owner-proxy-required", "--context-digest",
                         hashlib.sha256(context_path.read_bytes()).hexdigest()]
                        if owner_required else []
                    ),
                    *(
                        ["--runtime-manifest", str(qualified_parent.manifest_path),
                         "--runtime-manifest-digest", qualified_parent.manifest_digest]
                        if qualified_parent is not None else []
                    ),
                ],
                "cwd": str(project_root),
                "context_path": str(context_path),
                "exposed_tools": [
                    exposed_tool_name(definition["function"]["name"])
                    for definition in exposed_definitions
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
    "HashiMcpCapacityError",
    "reserve_hashi_mcp_capacity",
    "current_hashi_mcp_invocation",
    "prepare_hashi_mcp",
    "write_claude_mcp_config",
]
# HASHI-Fingerprint: hfp_0313e7726fc74118817eb4bf1ca8c245
