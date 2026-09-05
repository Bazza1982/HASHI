"""Persistent authenticated Browser/Computer capability Worker.

Run one process per capability kind.  The process binds an actual endpoint,
registers it through the Core-issued bootstrap receipt, then executes routine
actions without launching a shell or helper process per request.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import platform
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from orchestrator.file_permissions import tighten_fd_permissions
from tools.device_paths import resolve_device_path_arguments


CAPABILITY_PROTOCOL_VERSION = 1
CAPABILITY_REGISTRATION_SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_BROKER_RESPONSE_BYTES = 1024 * 1024
REGISTRATION_TTL_SECONDS = 90.0
HEARTBEAT_INTERVAL_SECONDS = 25.0
REPLAY_TTL_SECONDS = 300.0
CAPABILITY_KINDS = frozenset({"browser_control", "computer_control"})
COMPUTER_ACTIONS = frozenset(
    {
        "click",
        "drag",
        "helper_warmup",
        "info",
        "key",
        "mouse_move",
        "reset_input_state",
        "screenshot",
        "scroll",
        "type",
        "window_close",
        "window_focus",
        "window_list",
    }
)
BROWSER_ACTIONS = frozenset(
    {
        "active_tab",
        "click",
        "drag",
        "evaluate",
        "fill",
        "get_attribute",
        "get_html",
        "get_text",
        "hover",
        "key",
        "media_play",
        "media_state",
        "open_play_verify",
        "react",
        "screenshot",
        "scroll",
        "select",
        "session",
        "type_text",
        "upload",
        "wait_for",
    }
)
MUTATING_ACTIONS = frozenset(
    {
        "click",
        "drag",
        "fill",
        "hover",
        "key",
        "media_play",
        "mouse_move",
        "open_play_verify",
        "react",
        "reset_input_state",
        "scroll",
        "select",
        "session",
        "type",
        "type_text",
        "upload",
        "window_close",
        "window_focus",
    }
)


class DeviceWorkerError(RuntimeError):
    """The worker request, registration, or local authority is invalid."""


class DeviceWorkerBootstrapPending(DeviceWorkerError):
    """The current Core bootstrap or a usable callback route is not ready yet."""


def _private_json_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        tighten_fd_permissions(descriptor)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def _logger(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"hashi.device_control.{path.stem}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = RotatingFileHandler(
        path,
        maxBytes=2 * 1024 * 1024,
        backupCount=4,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def _event(logger: logging.Logger, event: str, **fields: Any) -> None:
    logger.info(
        json.dumps(
            {"ts": time.time(), "event": event, **fields},
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _default_device_id() -> str:
    seed = f"{platform.node()}|{platform.system()}|{platform.machine()}"
    return "device-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _default_session_id() -> str:
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
    domain = os.environ.get("USERDOMAIN") or platform.node() or "local"
    session = os.environ.get("SESSIONNAME") or os.environ.get("XDG_SESSION_ID") or "interactive"
    return f"{domain}\\{user}:{session}"


def _load_instance_id(bridge_home: Path, explicit: str | None) -> str:
    if explicit:
        value = explicit
    else:
        try:
            payload = json.loads((bridge_home / "agents.json").read_text(encoding="utf-8-sig"))
            value = (payload.get("global") or {}).get("instance_id")
        except (OSError, json.JSONDecodeError, AttributeError):
            value = None
    normalized = str(value or "").strip().upper()
    if not normalized:
        raise DeviceWorkerError("instance identity is unavailable")
    return normalized


def _read_bootstrap(bridge_home: Path, instance_id: str) -> dict[str, Any]:
    path = bridge_home / "state" / "device_control" / "bootstrap.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DeviceWorkerBootstrapPending(
            f"capability bootstrap is unavailable: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise DeviceWorkerError(f"capability bootstrap is unavailable: {path}") from exc
    if not isinstance(value, dict):
        raise DeviceWorkerError("capability bootstrap must be an object")
    if int(value.get("schema_version") or 0) != CAPABILITY_REGISTRATION_SCHEMA_VERSION:
        raise DeviceWorkerError("capability bootstrap schema is unsupported")
    if int(value.get("protocol_version") or 0) != CAPABILITY_PROTOCOL_VERSION:
        raise DeviceWorkerError("capability bootstrap protocol is unsupported")
    if str(value.get("instance_id") or "").strip().upper() != instance_id:
        raise DeviceWorkerError("capability bootstrap belongs to another instance")
    for key in ("registration_url", "heartbeat_url", "bootstrap_token"):
        if not str(value.get(key) or "").strip():
            raise DeviceWorkerError(f"capability bootstrap is missing {key}")
    return value


def _host_can_bind(host: str) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((host, 0))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def select_worker_bind_host(
    bridge_home: Path,
    instance_id: str,
    requested_host: str,
    *,
    can_bind: Callable[[str], bool] | None = None,
) -> str:
    """Select a locally bindable host from the current Core bootstrap receipt."""

    requested = str(requested_host or "").strip().strip("[]")
    if requested.casefold() != "auto":
        if not requested:
            raise DeviceWorkerError("worker bind host is required")
        return requested
    bootstrap = _read_bootstrap(bridge_home, instance_id)
    return _select_callback_host(
        bootstrap,
        can_bind=can_bind,
    )


def _select_callback_host(
    bootstrap: Mapping[str, Any],
    *,
    can_bind: Callable[[str], bool] | None = None,
) -> str:
    candidates = bootstrap.get("worker_callback_hosts")
    if not isinstance(candidates, list) or not candidates:
        raise DeviceWorkerBootstrapPending(
            "capability bootstrap has no worker callback host candidates"
        )
    probe = can_bind or _host_can_bind
    for value in candidates:
        host = str(value or "").strip().strip("[]")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            continue
        if address.is_unspecified or address.is_multicast:
            continue
        if probe(host):
            return host
    raise DeviceWorkerBootstrapPending(
        "none of the Core-published worker callback hosts can bind locally"
    )


def _capability_id(
    instance_id: str,
    device_id: str,
    user_session_id: str,
    capability_kind: str,
) -> str:
    seed = "|".join((instance_id, device_id, user_session_id, capability_kind))
    return "cap-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _generation_id(capability_kind: str) -> str:
    digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:24]
    return f"device-worker-v1:{capability_kind}:{digest}"


def _render_origin(host: str, port: int) -> str:
    rendered = f"[{host}]" if ":" in host else host
    return f"http://{rendered}:{port}"


class _DeviceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0)
        if self.stream.read(1) == b"":
            self.stream.seek(0)
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise DeviceWorkerError(
                "device/session write lock is held by another worker"
            ) from exc

    def release(self) -> None:
        if self.stream is None:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None

    def __enter__(self) -> "_DeviceLock":
        self.acquire()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()


@dataclass
class DeviceWorkerState:
    bridge_home: Path
    capability_kind: str
    instance_id: str
    device_id: str
    user_session_id: str
    worker_token: str
    bind_host: str
    advertise_host: str
    wsl_distro: str | None
    logger: logging.Logger
    auto_bind: bool = False
    executor: Callable[[str, dict[str, Any]], Any] | None = None
    bound_port: int = 0
    stopping: threading.Event = field(default_factory=threading.Event)
    rebind_requested: threading.Event = field(default_factory=threading.Event)
    shutdown_callback: Callable[[], None] | None = None
    replay_lock: threading.Lock = field(default_factory=threading.Lock)
    replayed_requests: dict[str, float] = field(default_factory=dict)

    @property
    def supported_actions(self) -> frozenset[str]:
        return BROWSER_ACTIONS if self.capability_kind == "browser_control" else COMPUTER_ACTIONS

    @property
    def capability_id(self) -> str:
        return _capability_id(
            self.instance_id,
            self.device_id,
            self.user_session_id,
            self.capability_kind,
        )

    @property
    def identity(self) -> dict[str, str]:
        return {
            "instance_id": self.instance_id,
            "capability_id": self.capability_id,
            "device_id": self.device_id,
            "user_session_id": self.user_session_id,
        }

    @property
    def endpoint(self) -> str:
        if self.bound_port <= 0:
            raise DeviceWorkerError("worker endpoint is not bound")
        return _render_origin(self.advertise_host, self.bound_port)

    @property
    def lock_path(self) -> Path:
        seed = f"{self.device_id}|{self.user_session_id}"
        name = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32] + ".lock"
        local_root = Path(
            os.environ.get("LOCALAPPDATA")
            or (self.bridge_home / "state" / "device_control")
        )
        return local_root / "HASHI" / "device_control" / "locks" / name

    @property
    def status_path(self) -> Path:
        return (
            self.bridge_home
            / "state"
            / "device_control"
            / f"{self.capability_kind}.json"
        )

    def mark_request(self, request_id: str) -> None:
        now = time.time()
        with self.replay_lock:
            self.replayed_requests = {
                key: seen
                for key, seen in self.replayed_requests.items()
                if now - seen <= REPLAY_TTL_SECONDS
            }
            if request_id in self.replayed_requests:
                raise DeviceWorkerError("replayed capability request rejected")
            self.replayed_requests[request_id] = now

    def validate_identity(self, value: Mapping[str, Any] | None) -> None:
        received = {
            key: str((value or {}).get(key) or "").strip()
            for key in self.identity
        }
        received["instance_id"] = received["instance_id"].upper()
        if received != self.identity:
            raise DeviceWorkerError(
                f"capability request identity mismatch: expected={self.identity} received={received}"
            )

    def validate_lease(
        self,
        action: str,
        payload: Mapping[str, Any],
    ) -> None:
        if (
            self.capability_kind != "browser_control"
            and action not in MUTATING_ACTIONS
        ):
            return
        lease = payload.get("lease")
        if not isinstance(lease, Mapping):
            raise DeviceWorkerError("mutating device action requires a Core lease")
        expected = {
            "capability_id": self.capability_id,
            "capability_kind": self.capability_kind,
            "instance_id": self.instance_id,
            "device_id": self.device_id,
            "user_session_id": self.user_session_id,
            "agent_id": str(payload.get("agent_id") or ""),
            "task_id": str(payload.get("task_id") or ""),
        }
        actual = {key: str(lease.get(key) or "") for key in expected}
        actual["instance_id"] = actual["instance_id"].upper()
        if actual != expected or not expected["agent_id"] or not expected["task_id"]:
            raise DeviceWorkerError("device action lease identity is invalid")
        expires_at = float(lease.get("expires_at") or 0)
        if expires_at <= time.time():
            raise DeviceWorkerError("device action lease has expired")
        if not str(lease.get("lease_id") or "").strip():
            raise DeviceWorkerError("device action lease id is missing")

    def health(self) -> dict[str, Any]:
        healthy = True
        detail: dict[str, Any] = {}
        if self.capability_kind == "computer_control":
            desktop_state: dict[str, Any]
            if os.name == "nt":
                try:
                    from tools.windows_helper import win32

                    desktop_state = win32.get_desktop_state()
                except Exception as exc:
                    desktop_state = {
                        "available": False,
                        "interactive": False,
                        "locked": None,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
            else:
                desktop_state = {
                    "available": False,
                    "interactive": False,
                    "locked": None,
                    "error": "Computer Control Worker requires Windows",
                }
            healthy = os.name == "nt" and bool(desktop_state.get("interactive"))
            detail = {
                "interactive_session": _default_session_id(),
                "desktop_platform": platform.system(),
                "desktop_state": desktop_state,
            }
        else:
            try:
                from tools.browser_extension_bridge import healthcheck

                bridge = healthcheck(timeout_s=1.0)
                healthy = bool(bridge.get("connected"))
                detail = {
                    "bridge_connected": healthy,
                    "bridge_endpoint": bridge.get("endpoint"),
                    "bridge_error": bridge.get("error"),
                }
            except Exception as exc:
                healthy = False
                detail = {
                    "bridge_connected": False,
                    "bridge_error": f"{type(exc).__name__}: {exc}",
                }
        return {
            "ok": healthy,
            "protocol_version": CAPABILITY_PROTOCOL_VERSION,
            "identity": self.identity,
            "capability_kind": self.capability_kind,
            "supported_actions": sorted(self.supported_actions),
            "worker_pid_and_generation": {
                "pid": os.getpid(),
                "generation": _generation_id(self.capability_kind),
            },
            "platform": platform.system().lower(),
            "health": detail,
        }

    async def execute(self, action: str, arguments: dict[str, Any]) -> Any:
        action = str(action or "").strip().casefold()
        if action not in self.supported_actions:
            raise DeviceWorkerError(f"unsupported {self.capability_kind} action: {action}")
        args = resolve_device_path_arguments(
            arguments,
            target_platform=("windows" if os.name == "nt" else "linux"),
            distro=self.wsl_distro,
        )
        if self.executor is not None:
            result = self.executor(action, args)
            return await result if asyncio.iscoroutine(result) else result
        if self.capability_kind == "computer_control":
            if os.name != "nt":
                raise DeviceWorkerError(
                    "Computer Control Worker must run in the Windows interactive session"
                )
            if action == "helper_warmup":
                return "Windows Computer Control Worker is already warm"
            from tools.windows_helper import win32

            desktop_state = win32.get_desktop_state()
            if not desktop_state.get("interactive"):
                raise DeviceWorkerError(
                    "Windows input desktop is locked or unavailable"
                )
            requested_provider = str(args.get("provider") or "auto").strip().casefold()
            if requested_provider not in {"", "auto", "usecomputer"}:
                raise DeviceWorkerError(
                    "persistent Computer Control Worker only permits its native provider"
                )
            # Production Computer Control is a long-lived worker.  Mark calls as
            # native-only so a Win32 failure cannot silently spawn usecomputer or
            # windows-mcp as a per-request child process.
            args["provider"] = "usecomputer"
            args["_persistent_worker"] = True
            from tools.windows_helper.backends import execute_action

            return await execute_action(action, args)
        return await _execute_browser_action(action, args)

    def cleanup(self, *, reason: str) -> dict[str, Any]:
        if self.capability_kind != "computer_control" or os.name != "nt":
            return {"ok": True, "reason": str(reason), "input_reset": False}
        from tools.windows_helper import win32

        result = win32.reset_input_state()
        return {
            "ok": True,
            "reason": str(reason),
            "input_reset": True,
            "released_keys": result.get("released_keys", []),
            "released_mouse": result.get("released_mouse", []),
        }


async def _execute_browser_action(action: str, args: dict[str, Any]) -> str:
    from tools.browser import (
        execute_browser_active_tab,
        execute_browser_click,
        execute_browser_drag,
        execute_browser_evaluate,
        execute_browser_fill,
        execute_browser_get_attribute,
        execute_browser_get_html,
        execute_browser_get_media_state,
        execute_browser_get_text,
        execute_browser_hover,
        execute_browser_key,
        execute_browser_open_play_verify,
        execute_browser_play,
        execute_browser_react,
        execute_browser_screenshot,
        execute_browser_scroll,
        execute_browser_select,
        execute_browser_session,
        execute_browser_type_text,
        execute_browser_upload,
        execute_browser_wait_for,
    )

    dispatch = {
        "active_tab": execute_browser_active_tab,
        "click": execute_browser_click,
        "drag": execute_browser_drag,
        "evaluate": execute_browser_evaluate,
        "fill": execute_browser_fill,
        "get_attribute": execute_browser_get_attribute,
        "get_html": execute_browser_get_html,
        "get_text": execute_browser_get_text,
        "hover": execute_browser_hover,
        "key": execute_browser_key,
        "media_play": execute_browser_play,
        "media_state": execute_browser_get_media_state,
        "open_play_verify": execute_browser_open_play_verify,
        "react": execute_browser_react,
        "screenshot": execute_browser_screenshot,
        "scroll": execute_browser_scroll,
        "select": execute_browser_select,
        "session": execute_browser_session,
        "type_text": execute_browser_type_text,
        "upload": execute_browser_upload,
        "wait_for": execute_browser_wait_for,
    }
    extension_args = dict(args)
    extension_args["bridge_backend"] = "extension"
    return await dispatch[action](extension_args)


@contextmanager
def _optional_device_lock(
    state: DeviceWorkerState,
    action: str,
) -> Iterator[None]:
    if (
        state.capability_kind != "browser_control"
        and action not in MUTATING_ACTIONS
    ):
        yield
        return
    with _DeviceLock(state.lock_path):
        yield


class DeviceWorkerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], state: DeviceWorkerState) -> None:
        self.state = state
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        super().__init__(address, DeviceWorkerRequestHandler)


class DeviceWorkerRequestHandler(BaseHTTPRequestHandler):
    server: DeviceWorkerServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _write(self, status: int, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _authorized(self) -> bool:
        authorization = str(self.headers.get("Authorization") or "")
        supplied = (
            authorization.removeprefix("Bearer ").strip()
            if authorization.startswith("Bearer ")
            else ""
        )
        return bool(supplied) and hmac.compare_digest(
            supplied,
            self.server.state.worker_token,
        )

    def _json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise DeviceWorkerError("invalid Content-Length") from exc
        if not 0 < length <= MAX_REQUEST_BYTES:
            raise DeviceWorkerError("capability request body size is invalid")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeviceWorkerError("capability request is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise DeviceWorkerError("capability request must be an object")
        return payload

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        if self.path != "/health":
            self._write(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._write(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return
        health = self.server.state.health()
        self._write(HTTPStatus.OK if health["ok"] else HTTPStatus.SERVICE_UNAVAILABLE, health)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        if self.path not in {"/action", "/cleanup"}:
            self._write(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._write(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return
        state = self.server.state
        if self.path == "/cleanup":
            try:
                payload = self._json_body()
                if int(payload.get("protocol_version") or 0) != CAPABILITY_PROTOCOL_VERSION:
                    raise DeviceWorkerError("capability protocol version mismatch")
                state.validate_identity(payload.get("identity"))
                result = state.cleanup(reason=str(payload.get("reason") or "cleanup"))
                _event(
                    state.logger,
                    "cleanup",
                    request_id=str(payload.get("request_id") or "") or None,
                    reason=str(payload.get("reason") or "cleanup"),
                    ok=True,
                )
                self._write(
                    HTTPStatus.OK,
                    {"ok": True, "identity": state.identity, "result": result},
                )
            except Exception as exc:
                self._write(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "ok": False,
                        "identity": state.identity,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
            return
        started = time.perf_counter()
        action = ""
        request_id = ""
        try:
            payload = self._json_body()
            if int(payload.get("protocol_version") or 0) != CAPABILITY_PROTOCOL_VERSION:
                raise DeviceWorkerError("capability protocol version mismatch")
            request_id = str(payload.get("request_id") or "").strip()
            if not request_id:
                raise DeviceWorkerError("capability request id is required")
            state.mark_request(request_id)
            state.validate_identity(payload.get("identity"))
            action = str(payload.get("action") or "").strip().casefold()
            state.validate_lease(action, payload)
            args = payload.get("args")
            if not isinstance(args, Mapping):
                raise DeviceWorkerError("capability action args must be an object")
            with _optional_device_lock(state, action):
                result = asyncio.run(state.execute(action, dict(args)))
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            _event(
                state.logger,
                "action",
                request_id=request_id,
                action=action,
                argument_names=sorted(str(key) for key in args if key != "_authorized_roots"),
                elapsed_ms=elapsed_ms,
                ok=True,
            )
            self._write(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "identity": state.identity,
                    "request_id": request_id,
                    "result": result,
                    "elapsed_ms": elapsed_ms,
                },
            )
        except Exception as exc:
            if state.capability_kind == "computer_control":
                try:
                    state.cleanup(reason="action-failed")
                except Exception:
                    pass
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            _event(
                state.logger,
                "action",
                request_id=request_id or None,
                action=action or None,
                elapsed_ms=elapsed_ms,
                ok=False,
                error_type=type(exc).__name__,
            )
            self._write(
                HTTPStatus.BAD_REQUEST,
                {
                    "ok": False,
                    "identity": state.identity,
                    "request_id": request_id or None,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "elapsed_ms": elapsed_ms,
                },
            )


def _http_json(
    url: str,
    *,
    method: str,
    payload: Mapping[str, Any] | None,
    headers: Mapping[str, str],
    timeout: float,
) -> dict[str, Any]:
    encoded = (
        json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        if payload is not None
        else None
    )
    request = urllib_request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/json", **dict(headers)},
        method=method,
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_BROKER_RESPONSE_BYTES + 1)
            if len(raw) > MAX_BROKER_RESPONSE_BYTES:
                raise DeviceWorkerError(
                    "capability broker response exceeds the size limit"
                )
            value = json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = {}
        raise DeviceWorkerError(
            str(detail.get("error") or f"HTTP {exc.code} from capability broker")
        ) from exc
    except (URLError, OSError, json.JSONDecodeError) as exc:
        raise DeviceWorkerError(f"capability broker request failed: {exc}") from exc
    if not isinstance(value, dict):
        raise DeviceWorkerError("capability broker response must be an object")
    return value


def _load_bootstrap(state: DeviceWorkerState) -> dict[str, Any]:
    return _read_bootstrap(state.bridge_home, state.instance_id)


def _registration_payload(state: DeviceWorkerState) -> dict[str, Any]:
    try:
        host_gateway = not ipaddress.ip_address(state.advertise_host).is_loopback
    except ValueError:
        host_gateway = True
    return {
        "schema_version": CAPABILITY_REGISTRATION_SCHEMA_VERSION,
        "capability_id": state.capability_id,
        "capability_kind": state.capability_kind,
        "instance_id": state.instance_id,
        "device_id": state.device_id,
        "user_session_id": state.user_session_id,
        "platform": platform.system().lower(),
        "transport_kind": (
            "authenticated_http_host_gateway"
            if host_gateway
            else "authenticated_http_loopback"
        ),
        "negotiated_endpoint": state.endpoint,
        "protocol_version": CAPABILITY_PROTOCOL_VERSION,
        "supported_actions": sorted(state.supported_actions),
        "worker_pid_and_generation": {
            "pid": os.getpid(),
            "generation": _generation_id(state.capability_kind),
        },
        "authorization_key_id": hashlib.sha256(
            state.worker_token.encode("utf-8")
        ).hexdigest()[:24],
        "health_and_expiry": {"ttl_seconds": REGISTRATION_TTL_SECONDS},
        "worker_token": state.worker_token,
    }


def _register(state: DeviceWorkerState, bootstrap: Mapping[str, Any]) -> None:
    response = _http_json(
        str(bootstrap["registration_url"]),
        method="POST",
        payload=_registration_payload(state),
        headers={
            "X-HASHI-Capability-Bootstrap": str(bootstrap["bootstrap_token"]),
        },
        timeout=5.0,
    )
    registration = response.get("registration")
    if response.get("ok") is not True or not isinstance(registration, Mapping):
        raise DeviceWorkerError(
            str(response.get("error") or "capability registration was rejected")
        )
    if str(registration.get("instance_id") or "").upper() != state.instance_id:
        raise DeviceWorkerError("capability registration response identity mismatch")
    _private_json_write(
        state.status_path,
        {
            "schema_version": 1,
            "instance_id": state.instance_id,
            "capability_id": state.capability_id,
            "capability_kind": state.capability_kind,
            "device_id": state.device_id,
            "user_session_id": state.user_session_id,
            "endpoint": state.endpoint,
            "pid": os.getpid(),
            "generation": _generation_id(state.capability_kind),
            "registered_at": time.time(),
        },
    )


def _heartbeat(state: DeviceWorkerState, bootstrap: Mapping[str, Any]) -> None:
    response = _http_json(
        str(bootstrap["heartbeat_url"]),
        method="POST",
        payload={
            "capability_id": state.capability_id,
            "identity": state.identity,
            "ttl_seconds": REGISTRATION_TTL_SECONDS,
        },
        headers={"Authorization": f"Bearer {state.worker_token}"},
        timeout=5.0,
    )
    if response.get("ok") is not True:
        raise DeviceWorkerError(
            str(response.get("error") or "capability heartbeat was rejected")
        )


def registration_loop(state: DeviceWorkerState) -> None:
    registered_bootstrap = ""
    next_heartbeat = 0.0
    backoff = 1.0
    while not state.stopping.wait(0.2):
        try:
            bootstrap = _load_bootstrap(state)
            if state.auto_bind:
                selected_host = _select_callback_host(bootstrap)
                if selected_host != state.bind_host:
                    _event(
                        state.logger,
                        "callback_route_changed",
                        previous_host=state.bind_host,
                        selected_host=selected_host,
                    )
                    state.rebind_requested.set()
                    state.stopping.set()
                    if state.shutdown_callback is not None:
                        state.shutdown_callback()
                    return
            bootstrap_id = hashlib.sha256(
                str(bootstrap["bootstrap_token"]).encode("utf-8")
            ).hexdigest()
            now = time.monotonic()
            if bootstrap_id != registered_bootstrap:
                _register(state, bootstrap)
                registered_bootstrap = bootstrap_id
                next_heartbeat = now + HEARTBEAT_INTERVAL_SECONDS
                backoff = 1.0
                _event(
                    state.logger,
                    "registered",
                    instance_id=state.instance_id,
                    capability_id=state.capability_id,
                    capability_kind=state.capability_kind,
                    endpoint=state.endpoint,
                )
            elif now >= next_heartbeat:
                _heartbeat(state, bootstrap)
                next_heartbeat = now + HEARTBEAT_INTERVAL_SECONDS
                backoff = 1.0
            continue
        except Exception as exc:
            registered_bootstrap = ""
            _event(
                state.logger,
                "registration_retry",
                error_type=type(exc).__name__,
                error=str(exc),
                retry_seconds=backoff,
            )
            state.stopping.wait(backoff)
            backoff = min(backoff * 2.0, 30.0)


def _configure_browser_transport(args: argparse.Namespace) -> None:
    if args.browser_endpoint:
        os.environ["HASHI_BROWSER_BRIDGE_ENDPOINT"] = args.browser_endpoint
        os.environ["HASHI_BROWSER_BRIDGE_SOCKET"] = args.browser_endpoint
    if args.browser_auth_file:
        os.environ["HASHI_BROWSER_BRIDGE_AUTH_FILE"] = args.browser_auth_file


def build_state(args: argparse.Namespace) -> DeviceWorkerState:
    bridge_home = Path(args.bridge_home).expanduser().resolve()
    capability_kind = str(args.kind).strip().casefold()
    if capability_kind not in CAPABILITY_KINDS:
        raise DeviceWorkerError(f"unsupported capability kind: {capability_kind}")
    instance_id = _load_instance_id(bridge_home, args.instance_id)
    if capability_kind == "browser_control":
        _configure_browser_transport(args)
    log_dir = Path(args.log_dir).expanduser().resolve()
    bind_host = select_worker_bind_host(
        bridge_home,
        instance_id,
        str(args.host),
    )
    requested_advertise = str(args.advertise_host or args.host).strip().strip("[]")
    advertise_host = (
        bind_host
        if requested_advertise.casefold() == "auto"
        else requested_advertise
    )
    if not bind_host or not advertise_host:
        raise DeviceWorkerError("worker bind and advertise hosts are required")
    if advertise_host in {"0.0.0.0", "::"}:
        raise DeviceWorkerError("worker advertise host must be connectable")
    if not 0 <= int(args.port) <= 65535:
        raise DeviceWorkerError("worker port must be between 0 and 65535")
    device_id = str(args.device_id or _default_device_id()).strip()
    user_session_id = str(args.user_session_id or _default_session_id()).strip()
    if not device_id or not user_session_id:
        raise DeviceWorkerError("worker device and user-session identities are required")
    return DeviceWorkerState(
        bridge_home=bridge_home,
        capability_kind=capability_kind,
        instance_id=instance_id,
        device_id=device_id,
        user_session_id=user_session_id,
        worker_token=os.urandom(48).hex(),
        bind_host=bind_host,
        advertise_host=advertise_host,
        wsl_distro=str(args.wsl_distro or "").strip() or None,
        logger=_logger(log_dir / f"{capability_kind}.jsonl"),
        auto_bind=str(args.host).strip().casefold() == "auto",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, choices=sorted(CAPABILITY_KINDS))
    parser.add_argument("--bridge-home", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--instance-id")
    parser.add_argument("--device-id")
    parser.add_argument("--user-session-id")
    parser.add_argument("--host", default="auto")
    parser.add_argument("--advertise-host")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--wsl-distro")
    parser.add_argument("--browser-endpoint")
    parser.add_argument("--browser-auth-file")
    parser.add_argument(
        "--log-dir",
        default=str(
            Path(os.environ.get("LOCALAPPDATA") or Path.home())
            / "HASHI"
            / "device_control"
            / "logs"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    startup_logger = _logger(
        Path(args.log_dir).expanduser().resolve()
        / f"{str(args.kind).strip().casefold()}.jsonl"
    )
    wait_seconds = 1.0
    while True:
        try:
            state = build_state(args)
            break
        except DeviceWorkerBootstrapPending as exc:
            _event(
                startup_logger,
                "bootstrap_wait",
                error=str(exc),
                retry_seconds=wait_seconds,
            )
            time.sleep(wait_seconds)
            wait_seconds = min(wait_seconds * 2.0, 30.0)

    while True:
        server = DeviceWorkerServer((state.bind_host, int(args.port)), state)
        state.bound_port = int(server.server_address[1])
        state.shutdown_callback = server.shutdown
        _private_json_write(
            state.status_path,
            {
                "schema_version": 1,
                "instance_id": state.instance_id,
                "capability_id": state.capability_id,
                "capability_kind": state.capability_kind,
                "device_id": state.device_id,
                "user_session_id": state.user_session_id,
                "endpoint": state.endpoint,
                "pid": os.getpid(),
                "generation": _generation_id(state.capability_kind),
                "started_at": time.time(),
            },
        )
        _event(
            state.logger,
            "started",
            instance_id=state.instance_id,
            capability_id=state.capability_id,
            capability_kind=state.capability_kind,
            endpoint=state.endpoint,
            pid=os.getpid(),
        )
        registration = threading.Thread(
            target=registration_loop,
            args=(state,),
            name=f"hashi-{state.capability_kind}-registration",
            daemon=True,
        )
        registration.start()
        if state.capability_kind == "computer_control":
            try:
                state.cleanup(reason="worker-started")
            except Exception as exc:
                _event(
                    state.logger,
                    "startup_cleanup_failed",
                    error_type=type(exc).__name__,
                )
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            state.stopping.set()
        finally:
            state.stopping.set()
            registration.join(timeout=3.0)
            if state.capability_kind == "computer_control":
                try:
                    state.cleanup(reason="worker-stopped")
                except Exception:
                    pass
            server.server_close()
            state.status_path.unlink(missing_ok=True)
            _event(state.logger, "stopped", capability_id=state.capability_id)
        if not state.rebind_requested.is_set():
            return 0
        state = build_state(args)


if __name__ == "__main__":
    raise SystemExit(main())
