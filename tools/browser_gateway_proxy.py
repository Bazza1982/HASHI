"""Request-scoped bridge from an isolated Tool Gateway to its Function Worker.

The gateway never connects to a device directly. The owning Function Worker
rechecks tool policy and uses its existing authenticated Core capability RPC.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import copy
import json
import inspect
import logging
import secrets
import threading
from typing import Any
from types import SimpleNamespace

from orchestrator import ui_language
from orchestrator.api_failures import operation_failure_payload
from orchestrator.operation_failures import OperationRejected, operation_failure

logger = logging.getLogger("HASHI.BrowserGateway")


class _BrowserResultLimit(ValueError):
    pass


MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 48 * 1024 * 1024
MAX_ACTIVE_SCOPES = 32


class BrowserGatewayProxy:
    def __init__(self, registry: Any, loop: asyncio.AbstractEventLoop) -> None:
        self.registry = registry
        self.loop = loop
        self._lock = threading.Lock()
        self._scopes: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, set] = {}
        self._calls: dict[tuple[str, str], Any] = {}
        self._progress: dict[tuple[str, str], dict] = {}
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def failure(self, error, *, phase, dispatched, audit_context, status=502, code="browser_action_failed", explanation=""):
                if dispatched and isinstance(error, (ValueError, TypeError)):
                    error = OperationRejected("response_invalid")
                failure = operation_failure(error, operation="browser_gateway", phase=phase, unconfirmed=dispatched)
                try:
                    logger.warning("Browser proxy failure: %s", failure.diagnostic())
                except Exception:
                    pass
                payload = operation_failure_payload(failure, error_code=code,
                    effect_state="unconfirmed" if dispatched else "not_started",
                    locale=ui_language.configured_default_locale(SimpleNamespace(global_config=audit_context.get("global_config"))))
                if explanation:
                    payload["message"] = explanation + "\n" + payload["message"]
                    payload["error"] = payload["message"]
                response = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def do_POST(self) -> None:
                if self.path not in {"/browser-tool", "/tool", "/tool-cancel", "/tool-status"}:
                    self.send_error(404)
                    return
                length = self.headers.get("Content-Length", "")
                try:
                    size = int(length)
                except ValueError:
                    size = 0
                if not 0 < size <= MAX_REQUEST_BYTES:
                    self.send_error(413)
                    return
                authorization = self.headers.get("Authorization", "")
                prefix = "Bearer "
                presented = authorization[len(prefix):] if authorization.startswith(prefix) else ""
                with proxy._lock:
                    matched = next(
                        (
                            token
                            for token in proxy._scopes
                            if presented and hmac.compare_digest(presented, token)
                        ),
                        None,
                    )
                    audit_context = (
                        dict(proxy._scopes[matched]) if matched is not None else None
                    )
                if audit_context is None:
                    self.send_error(403)
                    return
                phase, dispatched = "validate", False
                try:
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict):
                        raise ValueError("request must be an object")
                    dispatch_id = str(payload.get("dispatch_id") or "")
                    if self.path in {"/tool-cancel", "/tool-status"}:
                        if not audit_context.get("_gateway_all_tools") or not dispatch_id:
                            self.send_error(403)
                            return
                        with proxy._lock:
                            scope = proxy._scopes.get(matched)
                            if scope is None:
                                self.send_error(403)
                                return
                            pending = proxy._calls.get((matched, dispatch_id))
                            if self.path == "/tool-cancel":
                                scope.setdefault("_gateway_cancelled", set()).add(dispatch_id)
                                status = {"cancel_requested":True, "cleanup_confirmed":False}
                            else:
                                status = proxy._progress.get((matched, dispatch_id)) or {}
                        if self.path == "/tool-cancel" and pending is not None:
                            pending.cancel()
                        response = json.dumps(status).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(response)))
                        self.end_headers()
                        self.wfile.write(response)
                        return
                    tool_name = str(payload.get("tool_name") or "")
                    all_tools = self.path == "/tool"
                    if (not proxy.registry.is_allowed(tool_name)
                            or (all_tools and not audit_context.get("_gateway_all_tools"))
                            or (not all_tools and not tool_name.startswith("browser_"))):
                        raise ValueError("tool is not allowed for this invocation")
                    arguments = payload.get("arguments")
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be an object")
                    call_id = str(payload.get("tool_call_id") or "")
                    phase, dispatched = "dispatch", True
                    future = asyncio.run_coroutine_threadsafe(
                        proxy._execute_scoped(
                            matched, tool_name, arguments, call_id, audit_context, dispatch_id,
                        ),
                        proxy.loop,
                    )
                    with proxy._lock:
                        if (matched not in proxy._scopes or dispatch_id in
                                proxy._scopes[matched].get("_gateway_cancelled", set())):
                            future.cancel()
                        else:
                            proxy._pending.setdefault(matched, set()).add(future)
                            if dispatch_id:
                                proxy._calls[(matched, dispatch_id)] = future
                    try:
                        result = future.result(timeout=None if all_tools else 75)
                    except FutureTimeout:
                        future.cancel()
                        raise TimeoutError("browser action timed out") from None
                    finally:
                        with proxy._lock:
                            proxy._pending.get(matched, set()).discard(future)
                            proxy._calls.pop((matched, dispatch_id), None)
                    phase = "presentation"
                    response = json.dumps(
                        {"output": result.output, "is_error": result.is_error,
                         "content": result.content, "details": result.details},
                        ensure_ascii=False,
                    ).encode("utf-8")
                    if len(response) > MAX_RESPONSE_BYTES:
                        raise _BrowserResultLimit("browser result exceeds gateway limit")
                except _BrowserResultLimit as exc:
                    self.failure(exc, phase=phase, dispatched=dispatched, audit_context=audit_context,
                        status=400, code="browser_result_too_large", explanation="browser result exceeds gateway limit")
                    return
                except TimeoutError as exc:
                    self.failure(exc, phase=phase, dispatched=dispatched, audit_context=audit_context,
                        status=400, code="browser_action_timed_out", explanation="browser action timed out")
                    return
                except ValueError as exc:
                    if not dispatched:
                        self.send_error(400, str(exc))
                    else:
                        self.failure(exc, phase=phase, dispatched=True, audit_context=audit_context)
                    return
                except Exception as exc:
                    self.failure(exc, phase=phase, dispatched=dispatched, audit_context=audit_context)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="hashi-browser-gateway-proxy",
            daemon=True,
        )
        self.thread.start()

    def issue(self, audit_context: dict[str, Any], *, all_tools: bool = False) -> dict[str, str]:
        scope = dict(audit_context)
        scope["_read_access_roots"] = tuple(self.registry.read_access_roots)
        scope["_search_scope_snapshot"] = copy.deepcopy(self.registry.search_scope)
        scope["_execution_cwd"] = str(self.registry.workspace_dir)
        if "request_tool_allowlist" in scope:
            scope["request_tool_allowlist"] = copy.deepcopy(scope["request_tool_allowlist"])
        scope["_gateway_scope_identity"] = self._scope_identity()
        if all_tools:
            from orchestrator.session_store import SessionStore
            from tools.registry import ToolRegistry
            runtime = audit_context.get("_runtime")
            store = getattr(runtime, "session_store", None)
            if not isinstance(store, SessionStore) or type(self.registry) is not ToolRegistry:
                raise ValueError("owner ToolRegistry and canonical PAO Run are required")
            owner_id = str(audit_context.get("owner_id") or "")
            run_id = str(audit_context.get("hashi_run_id") or "")
            row = store.get_run(run_id, owner_id=owner_id)
            binding = {
                "run_id": run_id, "owner_id": owner_id,
                "agent_id": str(audit_context.get("agent_name") or ""),
                "session_id": str(audit_context.get("hashi_session_id") or ""),
                "request_id": str(audit_context.get("request_id") or ""),
                "context_generation": row["context_generation"],
                "fencing_token": audit_context.get("hashi_fencing_token"),
                "worker_id": row["worker_id"],
            }
            instance = audit_context.get("instance_id") or getattr(
                audit_context.get("global_config"), "instance_id", "")
            if str(instance).upper() != store.instance_id:
                raise ValueError("owner instance does not match the PAO Run")
            with store.hold_active_run(**binding):
                pass
            scope.update(_gateway_all_tools=True, _gateway_store=store,
                         _gateway_binding=binding)
        with self._lock:
            if len(self._scopes) >= MAX_ACTIVE_SCOPES:
                raise RuntimeError("browser gateway has too many active request scopes")
            token = secrets.token_urlsafe(48)
            self._scopes[token] = scope
            return {
                "url": f"http://127.0.0.1:{self.server.server_port}/" + ("tool" if all_tools else "browser-tool"),
                "token": token,
            }

    def _scope_identity(self):
        return (tuple(self.registry.access_roots), tuple(self.registry.read_access_roots),
                str(self.registry.workspace_dir), copy.deepcopy(self.registry.search_scope))

    @contextmanager
    def _dispatch_guard(self, token, scope, dispatch_id=""):
        from orchestrator.session_store import SessionConflict
        with self._lock:
            if (self._scopes.get(token) is not scope
                    or dispatch_id in scope.get("_gateway_cancelled", set())):
                raise SessionConflict("MCP invocation is no longer current")
        if self._scope_identity() != scope["_gateway_scope_identity"]:
            raise SessionConflict("MCP workspace scope changed; refresh the invocation")
        store = scope.get("_gateway_store")
        if store is not None:
            with store.hold_active_run(**scope["_gateway_binding"]):
                yield
        else:
            yield

    async def _execute_scoped(self, token, tool_name, arguments, call_id, audit_context, dispatch_id=""):
        with self._lock:
            scope = self._scopes.get(token)
        if scope is None:
            raise ValueError("MCP invocation was revoked before dispatch")
        context = dict(audit_context)
        if scope.get("_gateway_all_tools"):
            original = context.get("tool_activity_observer")
            async def observe(event):
                from orchestrator.request_activity import _tool_activity_fields
                facts = _tool_activity_fields(getattr(event, "metadata", None))
                if facts is not None:
                    with self._lock:
                        if token in self._scopes:
                            self._progress[(token, dispatch_id)] = facts
                if callable(original):
                    value = original(event)
                    if inspect.isawaitable(value):
                        await value
            context["tool_activity_observer"] = observe
        context["_tool_dispatch_guard"] = lambda: self._dispatch_guard(token, scope, dispatch_id)
        with self._dispatch_guard(token, scope, dispatch_id):
            pass
        return await self.registry.execute_with_audit_context(
            tool_name, arguments, call_id, audit_context=context)

    def revoke(self, token: str) -> bool:
        with self._lock:
            existed = self._scopes.pop(str(token or ""), None) is not None
            pending = self._pending.pop(str(token or ""), set())
            for key in tuple(self._progress):
                if key[0] == token:
                    self._progress.pop(key, None)
        for future in pending:
            future.cancel()
        return existed

    def close(self) -> None:
        with self._lock:
            tokens = tuple(self._scopes)
        for token in tokens:
            self.revoke(token)
        self.server.shutdown()
        self.server.server_close()
