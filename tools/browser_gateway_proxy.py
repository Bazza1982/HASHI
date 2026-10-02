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
import secrets
import threading
from typing import Any


MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 48 * 1024 * 1024


class BrowserGatewayProxy:
    def __init__(self, registry: Any, loop: asyncio.AbstractEventLoop) -> None:
        self.registry = registry
        self.loop = loop
        self._lock = threading.Lock()
        self._token = ""
        self._audit_context: dict[str, Any] = {}
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: Any) -> None:
                return

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
                with proxy._lock:
                    token = proxy._token
                    audit_context = dict(proxy._audit_context)
                if not token or not hmac.compare_digest(authorization, f"Bearer {token}"):
                    self.send_error(403)
                    return
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
                    response = json.dumps(
                        {"output": result.output, "is_error": result.is_error},
                        ensure_ascii=False,
                    ).encode("utf-8")
                    if len(response) > MAX_RESPONSE_BYTES:
                        raise ValueError("browser result exceeds gateway limit")
                except (ValueError, TimeoutError, json.JSONDecodeError) as exc:
                    self.send_error(400, str(exc))
                    return
                except Exception:
                    self.send_error(502, "browser action failed")
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
            self._token = secrets.token_urlsafe(48)
            self._audit_context = dict(audit_context)
            return {
                "url": f"http://127.0.0.1:{self.server.server_port}/browser-tool",
                "token": self._token,
            }

    def close(self) -> None:
        with self._lock:
            self._token = ""
            self._audit_context = {}
        self.server.shutdown()
        self.server.server_close()
