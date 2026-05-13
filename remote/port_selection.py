"""Port selection helpers for Hashi Remote."""

from __future__ import annotations

import socket

DEFAULT_PORT = 8766
FALLBACK_PORT_SCAN_LIMIT = 20


def is_port_available(host: str, port: int) -> bool:
    bind_host = "" if host in {"0.0.0.0", "::"} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((bind_host, int(port)))
        except OSError:
            return False
    return True


def candidate_ports(requested_port: int, configured_port: int) -> list[int]:
    candidates: list[int] = []

    def add(value: int | None) -> None:
        if not value:
            return
        try:
            port = int(value)
        except Exception:
            return
        if port > 0 and port not in candidates:
            candidates.append(port)

    add(requested_port)
    add(configured_port)
    add(DEFAULT_PORT)
    for port in range(DEFAULT_PORT + 1, DEFAULT_PORT + FALLBACK_PORT_SCAN_LIMIT + 1):
        add(port)
    return candidates


def select_available_port(host: str, requested_port: int, configured_port: int) -> tuple[int, list[int]]:
    attempted: list[int] = []
    for port in candidate_ports(requested_port, configured_port):
        attempted.append(port)
        if is_port_available(host, port):
            return port, attempted
    raise OSError(f"No available Hashi Remote port found; attempted {attempted}")
