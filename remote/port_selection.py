"""Port selection helpers for Hashi Remote."""

from __future__ import annotations

import socket
import secrets
from collections.abc import Callable

DEFAULT_PORT = 8766
FALLBACK_PORT_MIN = 20000
FALLBACK_PORT_MAX = 60999
FALLBACK_PORT_ATTEMPTS = 96
COMMON_POPULAR_PORTS = {
    22,
    25,
    53,
    80,
    110,
    143,
    443,
    465,
    587,
    993,
    995,
    1433,
    1521,
    2049,
    2375,
    2376,
    3000,
    3306,
    5000,
    5173,
    5432,
    5601,
    5672,
    5900,
    6379,
    8000,
    8080,
    8443,
    8765,
    8766,
    8767,
    8768,
    8769,
    8770,
    8888,
    9200,
    9300,
    27017,
}


def is_port_available(host: str, port: int) -> bool:
    bind_host = "" if host in {"0.0.0.0", "::"} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((bind_host, int(port)))
        except OSError:
            return False
    return True


def candidate_ports(
    requested_port: int,
    configured_port: int,
    *,
    reserved_ports: set[int] | None = None,
    rng: Callable[[int, int], int] | None = None,
) -> list[int]:
    candidates: list[int] = []
    reserved = set(reserved_ports or set())
    randrange = rng or secrets.SystemRandom().randint

    def add(value: int | None, *, skip_common: bool = False) -> None:
        if not value:
            return
        try:
            port = int(value)
        except Exception:
            return
        if port in reserved:
            return
        if skip_common and port in COMMON_POPULAR_PORTS:
            return
        if port > 0 and port not in candidates:
            candidates.append(port)

    add(requested_port)
    add(configured_port)
    guard = 0
    while len(candidates) < FALLBACK_PORT_ATTEMPTS + 2 and guard < FALLBACK_PORT_ATTEMPTS * 32:
        guard += 1
        add(randrange(FALLBACK_PORT_MIN, FALLBACK_PORT_MAX), skip_common=True)
    return candidates


def select_available_port(
    host: str,
    requested_port: int,
    configured_port: int,
    *,
    reserved_ports: set[int] | None = None,
    rng: Callable[[int, int], int] | None = None,
) -> tuple[int, list[int]]:
    attempted: list[int] = []
    for port in candidate_ports(
        requested_port,
        configured_port,
        reserved_ports=reserved_ports,
        rng=rng,
    ):
        attempted.append(port)
        if is_port_available(host, port):
            return port, attempted
    raise OSError(f"No available Hashi Remote port found; attempted {attempted}")
