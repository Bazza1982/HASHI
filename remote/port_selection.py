"""Compatibility facade over the authoritative stable-port candidate policy."""

from __future__ import annotations

from collections.abc import Callable

from orchestrator.runtime_defaults import DEFAULT_HASHI_REMOTE_PORT
from orchestrator.stable_port_allocator import (
    DEFAULT_POOL_ATTEMPTS,
    DEFAULT_POOL_MAX,
    DEFAULT_POOL_MIN,
    POPULAR_PORTS,
    candidate_ports as _candidate_ports,
    is_port_available,
)

DEFAULT_PORT = DEFAULT_HASHI_REMOTE_PORT
FALLBACK_PORT_MIN = DEFAULT_POOL_MIN
FALLBACK_PORT_MAX = DEFAULT_POOL_MAX
FALLBACK_PORT_ATTEMPTS = DEFAULT_POOL_ATTEMPTS
COMMON_POPULAR_PORTS = POPULAR_PORTS


def candidate_ports(
    requested_port: int,
    configured_port: int,
    *,
    reserved_ports: set[int] | None = None,
    rng: Callable[[int, int], int] | None = None,
) -> list[int]:
    return _candidate_ports(
        requested_port,
        configured_port,
        reserved_ports=reserved_ports,
        rng=rng,
    )


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
