from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from pathlib import Path

import pytest

from tools import browser_native_host
from tools.browser_bridge_transport import load_auth_key
from tools.browser_extension_bridge import send_bridge_command


pytestmark = [
    pytest.mark.platform,
    pytest.mark.skipif(os.name != "nt", reason="Windows named pipes require Windows"),
]


@pytest.mark.parametrize("disconnect_at", ["authentication", "response"])
def test_next_client_can_ping_after_previous_client_disconnects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disconnect_at: str
) -> None:
    from multiprocessing.connection import Client, PipeClient

    endpoint = rf"\\.\pipe\hashi-browser-recovery-{uuid.uuid4().hex}"
    auth_file = tmp_path / "bridge-auth.key"
    auth_key = load_auth_key(auth_file, create=True)
    state = browser_native_host.BridgeState(
        logger=logging.getLogger("hashi.browser_bridge.recovery"),
        socket_path=endpoint,
        audit_path=tmp_path / "audit.jsonl",
    )
    state.extension_connected.set()
    release_reply = threading.Event()
    dispatch = browser_native_host.dispatch_local_request

    if disconnect_at == "response":
        def dispatch_after_client_closes(bridge_state, request):
            assert release_reply.wait(2), "client did not finish disconnecting"
            return dispatch(bridge_state, request)

        monkeypatch.setattr(
            browser_native_host, "dispatch_local_request", dispatch_after_client_closes
        )

    server = browser_native_host.WindowsPipeServer(endpoint, state, auth_key)
    try:
        if disconnect_at == "authentication":
            with PipeClient(endpoint) as abandoned:
                assert abandoned.poll(2), "server did not start authentication"
                abandoned.recv_bytes()
        else:
            with Client(endpoint, family="AF_PIPE", authkey=auth_key) as abandoned:
                abandoned.send_bytes(json.dumps({"action": "ping", "args": {}}).encode())
            release_reply.set()

        response = send_bridge_command(
            "ping",
            {},
            socket_path=endpoint,
            auth_file=auth_file,
            timeout_s=2,
            connect_wait_s=2,
        )
        assert response["ok"] is True
        assert response["extension_connected"] is True
        assert server.thread.is_alive()
    finally:
        release_reply.set()
        server.shutdown()


def test_native_host_exits_for_extension_reconnect_if_pipe_thread_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    servers = []

    class StoppedThread:
        @staticmethod
        def is_alive() -> bool:
            return False

    class StoppedServer:
        def __init__(self, endpoint, state, auth_key):
            self.endpoint = endpoint
            self.state = state
            self.auth_key = auth_key
            self.thread = StoppedThread()
            self.shutdown_called = False
            servers.append(self)

        def shutdown(self) -> None:
            self.shutdown_called = True

    monkeypatch.setattr(browser_native_host, "is_windows_pipe", lambda _value: True)
    monkeypatch.setattr(browser_native_host, "WindowsPipeServer", StoppedServer)
    monkeypatch.setattr(browser_native_host, "native_reader_loop", lambda _state: None)

    result = browser_native_host.run_stdio_host(
        r"\\.\pipe\hashi-browser-dead-thread",
        tmp_path / "browser.log",
        auth_file=tmp_path / "auth.key",
    )

    assert result == 0
    assert servers[0].shutdown_called is True
