from __future__ import annotations

import socket

from remote.port_selection import candidate_ports, select_available_port


def _unused_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_select_available_port_uses_configured_port_when_free():
    configured_port = _unused_port()

    port, attempted = select_available_port("127.0.0.1", configured_port, configured_port)

    assert port == configured_port
    assert attempted == [configured_port]


def test_select_available_port_falls_back_when_requested_port_is_busy():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    busy_port = sock.getsockname()[1]
    sock.listen()
    try:
        fallback_port = _unused_port()
        port, attempted = select_available_port("127.0.0.1", busy_port, fallback_port)
    finally:
        sock.close()

    assert attempted[0] == busy_port
    assert port != busy_port
    assert port == fallback_port


def test_candidate_ports_skip_reserved_ports():
    ports = candidate_ports(8767, 8767, reserved_ports={8766, 8767})

    assert 8766 not in ports
    assert 8767 not in ports
    assert ports[0] == 8768
