"""Request-scoped bridge from an isolated Tool Gateway to its Function Worker.

The gateway never connects to a device directly. The owning Function Worker
rechecks tool policy and uses its existing authenticated Core capability RPC.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
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
                if self.path != "/browser-tool":
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
                    tool_name = str(payload.get("tool_name") or "")
                    if not tool_name.startswith("browser_") or not proxy.registry.is_allowed(tool_name):
                        raise ValueError("browser tool is not allowed")
                    arguments = payload.get("arguments")
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be an object")
                    call_id = str(payload.get("tool_call_id") or "")
                    phase, dispatched = "dispatch", True
                    future = asyncio.run_coroutine_threadsafe(
                        proxy.registry.execute_with_audit_context(
                            tool_name,
                            arguments,
                            call_id,
                            audit_context=audit_context,
                        ),
                        proxy.loop,
                    )
                    try:
                        result = future.result(timeout=75)
                    except FutureTimeout:
                        future.cancel()
                        raise TimeoutError("browser action timed out") from None
                    phase = "presentation"
                    response = json.dumps(
                        {"output": result.output, "is_error": result.is_error},
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

    def issue(self, audit_context: dict[str, Any]) -> dict[str, str]:
        with self._lock:
            if len(self._scopes) >= MAX_ACTIVE_SCOPES:
                raise RuntimeError("browser gateway has too many active request scopes")
            token = secrets.token_urlsafe(48)
            self._scopes[token] = dict(audit_context)
            return {
                "url": f"http://127.0.0.1:{self.server.server_port}/browser-tool",
                "token": token,
            }

    def revoke(self, token: str) -> bool:
        with self._lock:
            return self._scopes.pop(str(token or ""), None) is not None

    def close(self) -> None:
        with self._lock:
            self._scopes.clear()
        self.server.shutdown()
        self.server.server_close()
