from __future__ import annotations

import argparse
import json
import logging
import os
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

HOST_NAME = "com.hashi.browser_bridge"
HOST_VERSION = "0.1.0"
DEFAULT_SOCKET_PATH = Path(
    os.environ.get("HASHI_BROWSER_BRIDGE_SOCKET", "/tmp/hashi-browser-bridge.sock")
)
DEFAULT_LOG_PATH = Path(
    os.environ.get(
        "HASHI_BROWSER_BRIDGE_LOG",
        str(Path.home() / ".hashi" / "logs" / "browser_native_host.log"),
    )
)
DEFAULT_AUDIT_PATH = default_audit_path()
DEFAULT_REQUEST_TIMEOUT_S = float(
    os.environ.get("HASHI_BROWSER_BRIDGE_TIMEOUT", "20")
)
MUTATING_ACTIONS = {
    "click",
    "fill",
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
    socket_path: Path = DEFAULT_SOCKET_PATH
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
        action = str(request.get("action", "")).strip()
        args = request.get("args") or {}

        if action == "ping":
            response = {
                "ok": True,
                "host_name": HOST_NAME,
                "host_version": HOST_VERSION,
                "extension_connected": state.extension_connected.is_set(),
                "extension_meta": state.extension_meta,
                "socket_path": str(state.socket_path),
            }
        elif action == "session_list":
            with state.session_lock:
                response = {"ok": True, "sessions": list(state.sessions.values())}
        elif action == "session_create":
            session_id = str(args.get("session_id") or f"default::{args.get('owner') or 'unknown'}")
            with state.session_lock:
                existing = state.sessions.get(session_id)
            if existing:
                response = {"ok": True, "session": existing}
            else:
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
                if ext_response.get("ok"):
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
                    response = {"ok": True, "session": session}
                else:
                    response = ext_response
        elif action == "session_close":
            session_id = str(args.get("session_id", "")).strip()
            with state.session_lock:
                session = state.sessions.pop(session_id, None)
            if not session:
                response = {"ok": False, "error": f"unknown session_id: {session_id}"}
            else:
                response = state.dispatch_request("session_close", {"tabId": session.get("tab_id")})
                if not response.get("ok"):
                    response = {"ok": True, "output": "OK: session closed (mapping removed)"}
        elif action == "status":
            response = {
                "ok": True,
                "extension_connected": state.extension_connected.is_set(),
                "extension_meta": state.extension_meta,
                "pending_requests": len(state.pending),
                "socket_path": str(state.socket_path),
            }
        else:
            response = state.dispatch_request(action, args)

        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
        self.wfile.flush()


class ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


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
    if state.socket_path.exists():
        state.socket_path.unlink()
    server = ThreadedUnixServer(str(state.socket_path), UnixBridgeRequestHandler)
    server.bridge_state = state  # type: ignore[attr-defined]
    state.socket_inode = state.socket_path.stat().st_ino
    thread = threading.Thread(
        target=server.serve_forever,
        name="unix-bridge-server",
        daemon=True,
    )
    thread.start()
    state.logger.info("unix bridge socket listening at %s", state.socket_path)
    return server


def run_stdio_host(socket_path: Path, log_path: Path) -> int:
    logger = configure_logging(log_path)
    state = BridgeState(logger=logger, socket_path=socket_path)
    server = start_socket_server(state)
    reader = threading.Thread(target=native_reader_loop, args=(state,), daemon=True)
    reader.start()
    try:
        while not state.shutting_down.is_set():
            time.sleep(0.2)
    finally:
        server.shutdown()
        server.server_close()
        if socket_path.exists():
            try:
                current_inode = socket_path.stat().st_ino
            except FileNotFoundError:
                current_inode = None
            if current_inode is not None and current_inode == state.socket_inode:
                socket_path.unlink()
        logger.info("host shutdown complete")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HASHI native host for Chrome bridge")
    parser.add_argument("--stdio", action="store_true", help="Run as Chrome native messaging host")
    parser.add_argument("--socket", default=str(DEFAULT_SOCKET_PATH))
    parser.add_argument("--log-file", default=str(DEFAULT_LOG_PATH))
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.stdio:
        parser.error("--stdio is required for this host")
    return run_stdio_host(Path(args.socket), Path(args.log_file))


if __name__ == "__main__":
    raise SystemExit(main())
