from __future__ import annotations

import json
import logging
import os
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from tools.browser_extension_bridge import (
    BrowserBridgeError,
    healthcheck,
    send_bridge_command,
)
from tools.browser_audit import append_audit_record, sanitize_value
from tools.browser_bridge_transport import is_windows_pipe, load_auth_key
from tools.browser_native_host import (
    BridgeState,
    EXPECTED_EXTENSION_ORIGIN,
    WindowsPipeServer,
    build_parser,
    decode_native_message,
    encode_native_message,
    dispatch_local_request,
)

HAS_UNIX_STREAM_SERVER = hasattr(socketserver, "UnixStreamServer")
requires_unix_stream_server = pytest.mark.skipif(
    not HAS_UNIX_STREAM_SERVER,
    reason="Unix domain socket server is unavailable on this platform",
)
_UnixStreamServer = socketserver.UnixStreamServer if HAS_UNIX_STREAM_SERVER else socketserver.TCPServer


class _UnixHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        payload = json.loads(self.rfile.readline().decode("utf-8"))
        if payload["action"] == "ping":
            response = {"ok": True, "output": "pong"}
        else:
            response = {"ok": True, "output": f"echo:{payload['action']}"}
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
        self.wfile.flush()


class _UnixServer(socketserver.ThreadingMixIn, _UnixStreamServer):
    daemon_threads = True


@pytest.fixture()
def bridge_socket(tmp_path: Path):
    socket_path = tmp_path / "bridge.sock"
    server = _UnixServer(str(socket_path), _UnixHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield socket_path
    finally:
        server.shutdown()
        server.server_close()


@requires_unix_stream_server
def test_send_bridge_command_roundtrip(bridge_socket: Path) -> None:
    response = send_bridge_command("get_text", {"url": "https://example.com"}, socket_path=bridge_socket)
    assert response["ok"] is True
    assert response["output"] == "echo:get_text"


@requires_unix_stream_server
def test_healthcheck_uses_ping(bridge_socket: Path) -> None:
    response = healthcheck(socket_path=bridge_socket)
    assert response["connected"] is True
    assert response["response"]["output"] == "pong"


@requires_unix_stream_server
def test_send_bridge_command_missing_socket(tmp_path: Path) -> None:
    with pytest.raises(BrowserBridgeError):
        send_bridge_command(
            "ping",
            {},
            socket_path=tmp_path / "missing.sock",
            timeout_s=0.1,
            connect_wait_s=0.01,
        )


def test_native_message_codec_roundtrip() -> None:
    message = {"type": "hello", "value": 1}
    encoded = encode_native_message(message)
    decoded = decode_native_message(__import__("io").BytesIO(encoded))
    assert decoded == message


def test_windows_pipe_endpoint_detection() -> None:
    assert is_windows_pipe(r"\\.\pipe\hashi-browser-bridge") is True
    assert is_windows_pipe("/tmp/hashi-browser-bridge.sock") is False


def test_browser_bridge_auth_key_is_created_once(tmp_path: Path) -> None:
    auth_file = tmp_path / "bridge-auth.key"

    first = load_auth_key(auth_file, create=True)
    second = load_auth_key(auth_file, create=True)

    assert len(first) == 32
    assert second == first


@pytest.mark.skipif(os.name != "nt", reason="Windows named-pipe transport requires Windows")
def test_windows_named_pipe_roundtrip_and_healthcheck(tmp_path: Path) -> None:
    endpoint = rf"\\.\pipe\hashi-browser-test-{uuid.uuid4().hex}"
    auth_file = tmp_path / "bridge-auth.key"
    auth_key = load_auth_key(auth_file, create=True)
    state = BridgeState(
        logger=logging.getLogger("hashi.browser_bridge.test"),
        socket_path=endpoint,
        audit_path=tmp_path / "audit.jsonl",
    )
    server = WindowsPipeServer(endpoint, state, auth_key)
    try:
        wrong_auth_file = tmp_path / "wrong-auth.key"
        wrong_auth_file.write_bytes(b"x" * 32)
        with pytest.raises(BrowserBridgeError, match="authentication failed"):
            send_bridge_command(
                "ping",
                {},
                socket_path=endpoint,
                auth_file=wrong_auth_file,
                timeout_s=2.0,
                connect_wait_s=2.0,
            )
        disconnected_status = healthcheck(
            socket_path=endpoint,
            auth_file=auth_file,
            timeout_s=2.0,
        )
        state.extension_connected.set()
        state.extension_meta = {"browser": "Chrome", "test": True}
        response = send_bridge_command(
            "ping",
            {},
            socket_path=endpoint,
            auth_file=auth_file,
            timeout_s=2.0,
            connect_wait_s=2.0,
        )
        status = healthcheck(
            socket_path=endpoint,
            auth_file=auth_file,
            timeout_s=2.0,
        )
    finally:
        server.shutdown()

    assert response["ok"] is True
    assert disconnected_status["connected"] is False
    assert response["extension_connected"] is True
    assert response["extension_meta"]["browser"] == "Chrome"
    assert status["connected"] is True


def test_existing_session_returns_current_extension_capabilities() -> None:
    state = BridgeState(logger=logging.getLogger("test-browser-bridge"))
    state.extension_meta = {
        "extension_version": "0.2.0",
        "actions": ["active_tab", "media_state", "media_play"],
    }
    state.sessions["default::primary"] = {
        "session_id": "default::primary",
        "owner": "primary",
        "tab_id": 7,
    }

    response = dispatch_local_request(
        state,
        {
            "action": "session_create",
            "args": {"session_id": "default::primary", "owner": "primary"},
        },
    )

    assert response["ok"] is True
    assert response["extension_meta"]["extension_version"] == "0.2.0"
    assert response["extension_meta"]["actions"][-2:] == ["media_state", "media_play"]


@pytest.mark.skipif(os.name != "nt", reason="Chromium Windows invocation requires Windows")
def test_native_host_parser_accepts_chromium_invocation_arguments() -> None:
    args = build_parser().parse_args(
        [EXPECTED_EXTENSION_ORIGIN, "--parent-window=123"]
    )

    assert args.stdio is True
    assert args.origin == EXPECTED_EXTENSION_ORIGIN
    assert args.parent_window == "123"


@pytest.mark.skipif(os.name != "nt", reason="Native Windows host process requires Windows")
def test_native_windows_host_process_serves_authenticated_pipe(tmp_path: Path) -> None:
    endpoint = rf"\\.\pipe\hashi-browser-host-test-{uuid.uuid4().hex}"
    auth_file = tmp_path / "host-auth.key"
    log_file = tmp_path / "native-host.log"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tools.browser_native_host",
            "--endpoint",
            endpoint,
            "--auth-file",
            str(auth_file),
            "--log-file",
            str(log_file),
            EXPECTED_EXTENSION_ORIGIN,
            "--parent-window=0",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdin is not None
        process.stdin.write(
            encode_native_message(
                {"type": "hello", "browser": "Chrome", "test": True}
            )
        )
        process.stdin.flush()

        deadline = time.monotonic() + 5.0
        status = {"connected": False}
        while time.monotonic() < deadline:
            if auth_file.exists():
                status = healthcheck(
                    socket_path=endpoint,
                    auth_file=auth_file,
                    timeout_s=0.5,
                )
                if status["connected"]:
                    break
            time.sleep(0.05)

        assert status["connected"] is True
        assert status["response"]["extension_meta"]["browser"] == "Chrome"
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
    assert process.returncode == 0, stderr


@requires_unix_stream_server
def test_send_bridge_command_waits_for_socket(tmp_path: Path) -> None:
    socket_path = tmp_path / "late.sock"

    def start_server_later() -> None:
        time.sleep(0.5)
        server = _UnixServer(str(socket_path), _UnixHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(1.2)
        server.shutdown()
        server.server_close()

    thread = threading.Thread(target=start_server_later, daemon=True)
    thread.start()

    response = send_bridge_command(
        "ping",
        {},
        socket_path=socket_path,
        timeout_s=2,
        connect_wait_s=3,
    )
    assert response["ok"] is True


@requires_unix_stream_server
def test_send_bridge_command_does_not_unlink_existing_socket_path_on_connect_failure(tmp_path: Path) -> None:
    socket_path = tmp_path / "stale.sock"
    stale_server = _UnixServer(str(socket_path), _UnixHandler)
    stale_server.server_close()

    assert socket_path.exists()
    with pytest.raises(BrowserBridgeError):
        send_bridge_command(
            "ping",
            {},
            socket_path=socket_path,
            timeout_s=0.1,
            connect_wait_s=0.1,
        )
    assert socket_path.exists()


def test_append_audit_record_writes_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    append_audit_record({"kind": "browser_action", "action": "get_text"}, path=path)
    content = path.read_text(encoding="utf-8").strip()
    record = json.loads(content)
    assert record["kind"] == "browser_action"
    assert record["action"] == "get_text"


def test_browser_audit_redacts_screenshot_image_payloads() -> None:
    payload = "c2Vuc2l0aXZlLWJyb3dzZXItaW1hZ2U="

    sanitized = sanitize_value(
        {
            "direct": f"screenshot:{payload}",
            "session": f"[screenshot] base64:{payload}",
            "desktop": f"data:image/png;base64,{payload}",
        }
    )

    assert payload not in json.dumps(sanitized)
    assert all("[image-redacted]" in value for value in sanitized.values())
