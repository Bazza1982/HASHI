from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.stable_port_allocator import (
    SERVICE_HASHI_REMOTE,
    PortAllocationError,
    StablePortAllocator,
    candidate_ports,
    os_reserved_ports,
)


def test_allocator_persists_configured_port_when_free(tmp_path: Path) -> None:
    allocator = StablePortAllocator(
        bridge_home=tmp_path,
        service=SERVICE_HASHI_REMOTE,
        host="127.0.0.1",
        availability_probe=lambda host, port: True,
    )

    assignment = allocator.reserve_configured_port(8767)

    assert assignment.port == 8767
    assert assignment.source == "configured"
    assert assignment.persisted is True
    payload = json.loads((tmp_path / "runtime_port_assignments.json").read_text(encoding="utf-8"))
    assert payload["assignments"][SERVICE_HASHI_REMOTE]["port"] == 8767


def test_allocator_allocates_stable_fallback_when_configured_port_busy(tmp_path: Path) -> None:
    available_ports = {25001}
    seen = {"count": 0}

    def fake_rng(low: int, high: int) -> int:
        seen["count"] += 1
        return 25000 if seen["count"] == 1 else 25001

    allocator = StablePortAllocator(
        bridge_home=tmp_path,
        service=SERVICE_HASHI_REMOTE,
        host="127.0.0.1",
        availability_probe=lambda host, port: port in available_ports,
        platform_name="linux",
        linux_range_reader=lambda path: "25000 25000",
        rng=fake_rng,
    )

    assignment = allocator.reserve_configured_port(8767)

    assert assignment.port == 25001
    assert assignment.source == "allocated"
    assert 25000 not in assignment.attempted_ports


def test_allocator_fails_when_persisted_port_is_busy(tmp_path: Path) -> None:
    allocator = StablePortAllocator(
        bridge_home=tmp_path,
        service=SERVICE_HASHI_REMOTE,
        host="127.0.0.1",
        availability_probe=lambda host, port: True,
    )
    allocator.reserve_configured_port(8767)
    busy_allocator = StablePortAllocator(
        bridge_home=tmp_path,
        service=SERVICE_HASHI_REMOTE,
        host="127.0.0.1",
        availability_probe=lambda host, port: False,
    )

    with pytest.raises(PortAllocationError) as exc:
        busy_allocator.reserve_configured_port(8767)

    assert "Persisted" in str(exc.value)
    assert "reset flow" in str(exc.value)


def test_allocator_status_and_reset_surface(tmp_path: Path) -> None:
    allocator = StablePortAllocator(
        bridge_home=tmp_path,
        service=SERVICE_HASHI_REMOTE,
        host="127.0.0.1",
        availability_probe=lambda host, port: True,
    )

    assert allocator.status()["assigned"] is False
    allocator.reserve_configured_port(8767)
    assert allocator.status()["available"] is True
    assert allocator.reset() is True
    assert allocator.status()["assigned"] is False


def test_linux_reserved_ports_use_ephemeral_range_reader() -> None:
    reserved = os_reserved_ports(
        platform_name="linux",
        linux_range_reader=lambda path: "32768 32770",
    )

    assert reserved == {32768, 32769, 32770}


def test_windows_reserved_ports_include_excluded_and_dynamic_ranges() -> None:
    outputs = {
        ("netsh", "interface", "ipv4", "show", "excludedportrange", "protocol=tcp"): """
Protocol tcp Port Exclusion Ranges

Start Port    End Port
----------    --------
      5000        5002
""",
        ("netsh", "int", "ipv4", "show", "dynamicport", "tcp"): """
Protocol tcp Dynamic Port Range
---------------------------------
Start Port      : 49152
Number of Ports : 16
""",
    }

    def runner(args: list[str]) -> str:
        return outputs[tuple(args)]

    reserved = os_reserved_ports(platform_name="win32", windows_runner=runner)

    assert 5000 in reserved
    assert 5002 in reserved
    assert 49152 in reserved
    assert 49167 in reserved


def test_candidate_ports_skip_reserved_candidates() -> None:
    ports = candidate_ports(
        8767,
        8767,
        reserved_ports={25000},
        rng=lambda low, high: 25000 if low <= 25000 <= high else 25001,
    )

    assert 25000 not in ports
