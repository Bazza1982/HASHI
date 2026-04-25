from __future__ import annotations

import json
import socketserver
import threading
import time
from pathlib import Path

import pytest

from tools.browser_extension_bridge import (
    BrowserBridgeError,
    ensure_bridge_session,
    healthcheck,
    send_bridge_command,
)
from tools.browser_audit import append_audit_record
from tools.browser_native_host import decode_native_message, encode_native_message


class _UnixHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        payload = json.loads(self.rfile.readline().decode("utf-8"))
        if payload["action"] == "ping":
            response = {"ok": True, "output": "pong"}
        else:
            response = {"ok": True, "output": f"echo:{payload['action']}"}
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
        self.wfile.flush()


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
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


def test_send_bridge_command_roundtrip(bridge_socket: Path) -> None:
    response = send_bridge_command("get_text", {"url": "https://example.com"}, socket_path=bridge_socket)
    assert response["ok"] is True
    assert response["output"] == "echo:get_text"


def test_healthcheck_uses_ping(bridge_socket: Path) -> None:
    response = healthcheck(socket_path=bridge_socket)
    assert response["connected"] is True
    assert response["response"]["output"] == "pong"


def test_send_bridge_command_missing_socket(tmp_path: Path) -> None:
    with pytest.raises(BrowserBridgeError):
        send_bridge_command("ping", {}, socket_path=tmp_path / "missing.sock", timeout_s=0.1)


def test_native_message_codec_roundtrip() -> None:
    message = {"type": "hello", "value": 1}
    encoded = encode_native_message(message)
    decoded = decode_native_message(__import__("io").BytesIO(encoded))
    assert decoded == message


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


def test_ensure_bridge_session_raises_without_socket(tmp_path: Path) -> None:
    with pytest.raises(BrowserBridgeError):
        ensure_bridge_session(
            session_id="test",
            args={"agent_name": "zelda"},
            socket_path=tmp_path / "missing.sock",
        )
