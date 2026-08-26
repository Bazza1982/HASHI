from __future__ import annotations

import argparse
import json
import logging
import os
from multiprocessing import AuthenticationError
from multiprocessing.connection import Listener
import queue
import socketserver
import struct
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from tools.browser_audit import append_audit_record, default_audit_path
from tools.browser_bridge_transport import (
    DEFAULT_WINDOWS_AUTH_FILE,
    DEFAULT_WINDOWS_PIPE,
    is_windows_pipe,
    load_auth_key,
)

HOST_NAME = "com.hashi.browser_bridge"
HOST_VERSION = "0.1.0"
EXPECTED_EXTENSION_ORIGIN = "chrome-extension://jdeaedmoejdapldleofeggedgenogpka/"
DEFAULT_SOCKET_PATH = Path(
    os.environ.get("HASHI_BROWSER_BRIDGE_SOCKET", "/tmp/hashi-browser-bridge.sock")
)
DEFAULT_ENDPOINT: str | Path = (
    os.environ.get("HASHI_BROWSER_BRIDGE_ENDPOINT")
    or os.environ.get("HASHI_BROWSER_BRIDGE_SOCKET")
    or (DEFAULT_WINDOWS_PIPE if os.name == "nt" else str(DEFAULT_SOCKET_PATH))
)
_DEFAULT_LOG_PATH = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    / "HASHI"
    / "browser_bridge"
    / "logs"
    / "native-host.log"
    if os.name == "nt"
    else Path.home() / ".hashi" / "logs" / "browser_native_host.log"
)
DEFAULT_LOG_PATH = Path(
    os.environ.get(
        "HASHI_BROWSER_BRIDGE_LOG",
        str(_DEFAULT_LOG_PATH),
    )
)
DEFAULT_AUDIT_PATH = default_audit_path()
DEFAULT_REQUEST_TIMEOUT_S = float(
    os.environ.get("HASHI_BROWSER_BRIDGE_TIMEOUT", "20")
)
MUTATING_ACTIONS = {
    "click",
    "fill",
    "hover",
    "type_text",
    "key",
    "select",
    "drag",
    "upload",
    "session_close",
}


def encode_native_message(message: dict[str, Any]) -> bytes:
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return struct.pack("<I", len(payload)) + payload


def decode_native_message(stream: Any) -> Optional[dict[str, Any]]:
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise RuntimeError("incomplete native messaging header")
    length = struct.unpack("<I", header)[0]
    payload = stream.read(length)
    if len(payload) != length:
        raise RuntimeError("incomplete native messaging payload")
    return json.loads(payload.decode("utf-8"))


def configure_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("hashi.browser_native_host")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(threadName)s | %(message)s")
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger


@dataclass
class BridgeState:
    logger: logging.Logger
    pending: dict[str, queue.Queue[dict[str, Any]]] = field(default_factory=dict)
    pending_lock: threading.Lock = field(default_factory=threading.Lock)
    native_write_lock: threading.Lock = field(default_factory=threading.Lock)
    extension_connected: threading.Event = field(default_factory=threading.Event)
    extension_meta: dict[str, Any] = field(default_factory=dict)
    shutting_down: threading.Event = field(default_factory=threading.Event)
    socket_path: str | Path = DEFAULT_ENDPOINT
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
    socket_inode: Optional[int] = None
    audit_path: Path = DEFAULT_AUDIT_PATH
    session_lock: threading.Lock = field(default_factory=threading.Lock)
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)

    def write_audit(self, record: dict[str, Any]) -> None:
        append_audit_record(record, path=self.audit_path)

    def send_to_extension(self, message: dict[str, Any]) -> None:
        with self.native_write_lock:
            sys.stdout.buffer.write(encode_native_message(message))
            sys.stdout.buffer.flush()
        self.logger.info("host->extension %s", message.get("type"))

    def dispatch_request(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if not self.extension_connected.is_set():
            return {
                "ok": False,
                "error": (
                    "Chrome extension is not connected to the native host. "
                    "Open Chrome with the HASHI Browser Bridge extension enabled."
                ),
            }
        if str(args.get("safety_mode", "read_write")).lower() == "read_only" and action in MUTATING_ACTIONS:
            return {"ok": False, "error": f"action '{action}' is blocked in read_only mode"}
        session_id = str(args.get("session_id", "")).strip() or None
        request_id = str(uuid.uuid4())
        wait_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self.pending_lock:
            self.pending[request_id] = wait_queue
        try:
            request_args = dict(args)
            if session_id:
                with self.session_lock:
                    session = self.sessions.get(session_id)
                if session:
                    request_args.setdefault("tabId", session.get("tab_id"))
                    request_args.setdefault("session_id", session_id)
                    request_args.setdefault("safety_mode", session.get("safety_mode", "read_write"))
            started = time.time()
            self.send_to_extension(
                {
                    "type": "request",
                    "request_id": request_id,
                    "action": action,
                    "args": request_args,
                }
            )
            response = wait_queue.get(timeout=self.request_timeout_s)
            self.write_audit(
                {
                    "kind": "browser_action",
                    "action": action,
                    "request_id": request_id,
                    "session_id": session_id or request_args.get("session_id", ""),
                    "args": request_args,
                    "response": response,
                    "elapsed_ms": int((time.time() - started) * 1000),
                }
            )
            if session_id and response.get("ok"):
                with self.session_lock:
                    session = self.sessions.get(session_id)
                    if session:
                        session["updated_at"] = time.time()
                        meta = response.get("meta") or {}
                        if meta.get("tabId"):
                            session["tab_id"] = meta["tabId"]
                        if meta.get("url"):
                            session["url"] = meta["url"]
                        if meta.get("title"):
                            session["title"] = meta["title"]
            return response
        except queue.Empty:
            self.logger.error("timeout waiting for extension response: %s", request_id)
            return {"ok": False, "error": f"timeout waiting for extension action '{action}'"}
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def complete_request(self, request_id: str, response: dict[str, Any]) -> None:
        with self.pending_lock:
            wait_queue = self.pending.get(request_id)
        if wait_queue is None:
            self.logger.warning("response for unknown request_id: %s", request_id)
            return
        try:
            wait_queue.put_nowait(response)
        except queue.Full:
            self.logger.warning("queue already full for request_id: %s", request_id)

    def handle_native_message(self, message: dict[str, Any]) -> None:
        message_type = str(message.get("type", ""))
        self.logger.info("extension->host %s", message_type)
        if message_type == "hello":
            self.extension_meta = message
            self.extension_connected.set()
            self.send_to_extension(
                {
                    "type": "hello_ack",
                    "host_name": HOST_NAME,
                    "host_version": HOST_VERSION,
                    "socket_path": str(self.socket_path),
                }
            )
            return
        if message_type == "response":
            request_id = str(message.get("request_id", ""))
            self.complete_request(request_id, message)
            return
        if message_type == "log":
            self.logger.info(
                "extension-log %s %s",
                message.get("level", "info"),
                message.get("message", ""),
            )
            return
        if message_type == "heartbeat":
            return
        if message_type == "pong":
            return
        self.logger.warning("unknown native message: %s", message)


class UnixBridgeRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw_line = self.rfile.readline().decode("utf-8").strip()
        if not raw_line:
            return
        try:
            request = json.loads(raw_line)
        except json.JSONDecodeError:
            self.wfile.write(
                json.dumps({"ok": False, "error": "invalid json request"}).encode("utf-8")
                + b"\n"
            )
            self.wfile.flush()
            return

        state: BridgeState = self.server.bridge_state  # type: ignore[attr-defined]
        response = dispatch_local_request(state, request)

        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
        self.wfile.flush()


if hasattr(socketserver, "UnixStreamServer"):
    _UnixStreamServer = socketserver.UnixStreamServer
else:  # Windows native Python can import codecs/utilities but cannot host AF_UNIX server.
    _UnixStreamServer = None


class ThreadedUnixServer(socketserver.ThreadingMixIn, _UnixStreamServer if _UnixStreamServer else socketserver.TCPServer):
    daemon_threads = True


def dispatch_local_request(state: BridgeState, request: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one trusted local client request independent of its transport."""

    action = str(request.get("action", "")).strip()
    args = request.get("args") or {}
    if not isinstance(args, dict):
        return {"ok": False, "error": "request args must be a JSON object"}

    if action == "ping":
        return {
            "ok": True,
            "host_name": HOST_NAME,
            "host_version": HOST_VERSION,
            "extension_connected": state.extension_connected.is_set(),
            "extension_meta": state.extension_meta,
            "socket_path": str(state.socket_path),
        }
    if action == "session_list":
        with state.session_lock:
            return {"ok": True, "sessions": list(state.sessions.values())}
    if action == "session_create":
        session_id = str(args.get("session_id") or f"default::{args.get('owner') or 'unknown'}")
        with state.session_lock:
            existing = state.sessions.get(session_id)
        if existing:
            return {
                "ok": True,
                "session": existing,
                "extension_meta": state.extension_meta,
            }
        ext_response = state.dispatch_request("session_create", args)
        if not ext_response.get("ok"):
            state.logger.info("session_create unsupported by extension; falling back to active_tab")
            ext_response = state.dispatch_request(
                "active_tab",
                {
                    "url": args.get("url", ""),
                    "wait_ms": args.get("wait_ms", 0),
                    "safety_mode": args.get("safety_mode", "read_write"),
                },
            )
        if not ext_response.get("ok"):
            return ext_response
        raw = ext_response.get("output", "{}")
        try:
            info = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception:
            info = {}
        session = {
            "session_id": session_id,
            "owner": str(args.get("owner", "")),
            "safety_mode": str(args.get("safety_mode", "read_write")),
            "tab_id": info.get("tabId"),
            "url": info.get("url"),
            "title": info.get("title"),
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        with state.session_lock:
            state.sessions[session_id] = session
        return {
            "ok": True,
            "session": session,
            "extension_meta": state.extension_meta,
        }
    if action == "session_close":
        session_id = str(args.get("session_id", "")).strip()
        with state.session_lock:
            session = state.sessions.pop(session_id, None)
        if not session:
            return {"ok": False, "error": f"unknown session_id: {session_id}"}
        response = state.dispatch_request("session_close", {"tabId": session.get("tab_id")})
        if not response.get("ok"):
            return {"ok": True, "output": "OK: session closed (mapping removed)"}
        return response
    if action == "status":
        return {
            "ok": True,
            "extension_connected": state.extension_connected.is_set(),
            "extension_meta": state.extension_meta,
            "pending_requests": len(state.pending),
            "socket_path": str(state.socket_path),
        }
    return state.dispatch_request(action, args)


class WindowsPipeServer:
    """Authenticated JSON server for native Windows clients."""

    def __init__(self, endpoint: str, state: BridgeState, auth_key: bytes):
        self.endpoint = endpoint
        self.state = state
        self.listener = Listener(endpoint, family="AF_PIPE", authkey=auth_key)
        self.stopping = threading.Event()
        self.thread = threading.Thread(
            target=self._serve,
            name="pipe-bridge-server",
            daemon=True,
        )
        self.thread.start()

    def _serve(self) -> None:
        while not self.stopping.is_set():
            try:
                connection = self.listener.accept()
            except AuthenticationError:
                if not self.stopping.is_set():
                    self.state.logger.warning("Rejected unauthenticated Windows bridge client")
                continue
            except (OSError, EOFError):
                if not self.stopping.is_set():
                    self.state.logger.exception("Windows bridge pipe accept failed")
                break
            try:
                request = json.loads(connection.recv_bytes().decode("utf-8"))
                if not isinstance(request, dict):
                    raise ValueError("request must be a JSON object")
                response = dispatch_local_request(self.state, request)
            except Exception as exc:
                self.state.logger.exception("Windows bridge pipe request failed: %s", exc)
                response = {"ok": False, "error": "invalid local bridge request"}
            try:
                connection.send_bytes(json.dumps(response).encode("utf-8"))
            finally:
                connection.close()

    def shutdown(self) -> None:
        self.stopping.set()
        self.listener.close()


def native_reader_loop(state: BridgeState) -> None:
    try:
        while not state.shutting_down.is_set():
            message = decode_native_message(sys.stdin.buffer)
            if message is None:
                state.logger.info("native stdin closed; shutting down host")
                break
            state.handle_native_message(message)
    except Exception as exc:  # pragma: no cover - safety net
        state.logger.exception("native reader crashed: %s", exc)
    finally:
        state.extension_connected.clear()
        state.shutting_down.set()


def start_socket_server(state: BridgeState) -> ThreadedUnixServer:
    if _UnixStreamServer is None:
        raise RuntimeError("Unix domain sockets are not available on this platform")
    socket_path = Path(state.socket_path)
    if socket_path.exists():
        socket_path.unlink()
    server = ThreadedUnixServer(str(socket_path), UnixBridgeRequestHandler)
    server.bridge_state = state  # type: ignore[attr-defined]
    state.socket_inode = socket_path.stat().st_ino
    thread = threading.Thread(
        target=server.serve_forever,
        name="unix-bridge-server",
        daemon=True,
    )
    thread.start()
    state.logger.info("unix bridge socket listening at %s", state.socket_path)
    return server


def run_stdio_host(
    endpoint: str | Path,
    log_path: Path,
    *,
    auth_file: Path = DEFAULT_WINDOWS_AUTH_FILE,
) -> int:
    logger = configure_logging(log_path)
    state = BridgeState(logger=logger, socket_path=endpoint)
    if is_windows_pipe(endpoint):
        server: ThreadedUnixServer | WindowsPipeServer = WindowsPipeServer(
            str(endpoint),
            state,
            load_auth_key(auth_file, create=True),
        )
        logger.info("Windows bridge named pipe listening at %s", endpoint)
    else:
        state.socket_path = Path(endpoint)
        server = start_socket_server(state)
    reader = threading.Thread(target=native_reader_loop, args=(state,), daemon=True)
    reader.start()
    try:
        while not state.shutting_down.is_set():
            time.sleep(0.2)
    finally:
        server.shutdown()
        if isinstance(server, ThreadedUnixServer):
            server.server_close()
        socket_path = Path(endpoint) if not is_windows_pipe(endpoint) else None
        if socket_path is not None and socket_path.exists():
            try:
                current_inode = socket_path.stat().st_ino
            except FileNotFoundError:
                current_inode = None
            if current_inode is not None and current_inode == state.socket_inode:
                socket_path.unlink()
        logger.info("host shutdown complete")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HASHI native host for Chromium browser bridge")
    parser.add_argument(
        "--stdio",
        action="store_true",
        default=os.name == "nt",
        help="Run as a Chromium native messaging host",
    )
    parser.add_argument("--socket", "--endpoint", dest="endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--auth-file", default=str(DEFAULT_WINDOWS_AUTH_FILE))
    parser.add_argument("--log-file", default=str(DEFAULT_LOG_PATH))
    parser.add_argument("origin", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("--parent-window", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.stdio:
        parser.error("--stdio is required for this host")
    if args.origin and args.origin != EXPECTED_EXTENSION_ORIGIN:
        parser.error("native messaging origin is not authorised")
    if os.name == "nt":
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    return run_stdio_host(
        args.endpoint,
        Path(args.log_file),
        auth_file=Path(args.auth_file),
    )


if __name__ == "__main__":
    raise SystemExit(main())
