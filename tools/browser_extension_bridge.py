from __future__ import annotations

import json
import logging
import os
import socket
import uuid
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hashi.browser_extension_bridge")

DEFAULT_SOCKET_PATH = Path(
    os.environ.get("HASHI_BROWSER_BRIDGE_SOCKET", "/tmp/hashi-browser-bridge.sock")
)
DEFAULT_TIMEOUT_S = float(os.environ.get("HASHI_BROWSER_BRIDGE_TIMEOUT", "20"))
DEFAULT_CONNECT_WAIT_S = float(os.environ.get("HASHI_BROWSER_BRIDGE_CONNECT_WAIT", "6"))
DEFAULT_RETRY_DELAY_S = float(os.environ.get("HASHI_BROWSER_BRIDGE_RETRY_DELAY", "0.35"))


class BrowserBridgeError(RuntimeError):
    pass


def get_socket_path() -> Path:
    return DEFAULT_SOCKET_PATH


def bridge_available(socket_path: Optional[Path] = None) -> bool:
    path = Path(socket_path or get_socket_path())
    return path.exists()


def send_bridge_command(
    action: str,
    args: Optional[dict[str, Any]] = None,
    *,
    socket_path: Optional[Path] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    connect_wait_s: float = DEFAULT_CONNECT_WAIT_S,
) -> dict[str, Any]:
    path = Path(socket_path or get_socket_path())
    request = {
        "request_id": str(uuid.uuid4()),
        "action": action,
        "args": args or {},
    }

    deadline = time.monotonic() + connect_wait_s
    last_error: Optional[Exception] = None
    while True:
        if not path.exists():
            if time.monotonic() >= deadline:
                raise BrowserBridgeError(
                    f"extension bridge socket not found: {path}. "
                    "Install the Chrome extension and native host first."
                )
            time.sleep(DEFAULT_RETRY_DELAY_S)
            continue

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_s)
            try:
                client.connect(str(path))
                client.sendall((json.dumps(request) + "\n").encode("utf-8"))
                chunks: list[bytes] = []
                while True:
                    data = client.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
                    if b"\n" in data:
                        break
                break
            except (OSError, socket.timeout) as exc:
                last_error = exc
                if time.monotonic() >= deadline:
                    raise BrowserBridgeError(f"failed talking to extension bridge: {exc}") from exc
                time.sleep(DEFAULT_RETRY_DELAY_S)
                continue

    payload = b"".join(chunks).decode("utf-8").strip()
    if not payload:
        if last_error is not None:
            raise BrowserBridgeError(f"extension bridge returned empty response after retry: {last_error}")
        raise BrowserBridgeError("extension bridge returned empty response")
    try:
        response = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BrowserBridgeError(f"invalid bridge response: {payload[:300]}") from exc

    logger.debug("Bridge response for %s: %s", action, response)
    return response


def _derive_owner(args: dict[str, Any] | None = None) -> str:
    args = args or {}
    audit = args.get("_audit") if isinstance(args.get("_audit"), dict) else {}
    return str(
        args.get("agent_name")
        or audit.get("agent_name")
        or os.environ.get("HASHI_AGENT_NAME")
        or os.environ.get("CLAUDE_FLOW_WORKER")
        or Path.cwd().name
    )


def ensure_bridge_session(
    *,
    session_id: str | None = None,
    args: Optional[dict[str, Any]] = None,
    url: str | None = None,
    safety_mode: str | None = None,
    socket_path: Optional[Path] = None,
) -> dict[str, Any]:
    payload = dict(args or {})
    payload["owner"] = _derive_owner(payload)
    payload["session_id"] = session_id or payload.get("session_id") or f"default::{payload['owner']}"
    if url:
        payload["url"] = url
    if safety_mode:
        payload["safety_mode"] = safety_mode
    response = send_bridge_command("session_create", payload, socket_path=socket_path)
    if not response.get("ok"):
        raise BrowserBridgeError(str(response.get("error", "failed to create browser session")))
    return response


def healthcheck(
    *,
    socket_path: Optional[Path] = None,
    timeout_s: float = 2.0,
) -> dict[str, Any]:
    path = Path(socket_path or get_socket_path())
    result: dict[str, Any] = {
        "socket_path": str(path),
        "socket_exists": path.exists(),
        "connected": False,
    }
    if not path.exists():
        return result
    try:
        response = send_bridge_command("ping", {}, socket_path=path, timeout_s=timeout_s)
    except BrowserBridgeError as exc:
        result["error"] = str(exc)
        return result
    result["connected"] = bool(response.get("ok"))
    result["response"] = response
    return result
