from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator.service_endpoints import (
    ServiceEndpointError,
    ServiceEndpointRegistry,
    discover_worker_callback_hosts,
    endpoint_from_snapshot,
    load_service_endpoint,
    select_service_bind_host,
)


def _registry(tmp_path, instance_id="HASHI3"):
    kernel = SimpleNamespace(
        global_cfg=SimpleNamespace(instance_id=instance_id),
        paths=SimpleNamespace(bridge_home=tmp_path, instance_id=instance_id),
    )
    return ServiceEndpointRegistry(kernel)


def test_wsl_bind_host_is_discovered_without_a_machine_address_constant():
    checked = []

    def can_bind(host):
        checked.append(host)
        return host == "172.29.144.1"

    selected = select_service_bind_host(
        "127.0.0.1",
        candidates=("172.29.144.1", "192.168.50.9"),
        can_bind=can_bind,
        wsl=True,
    )

    assert selected == "172.29.144.1"
    assert checked == ["172.29.144.1"]


def test_wsl_worker_callback_uses_live_gateway_then_loopback():
    assert discover_worker_callback_hosts(
        wsl=True,
        route_output=(
            "default via 172.29.144.1 dev eth0 proto kernel\n"
            "default via 172.30.0.1 dev eth1 metric 50\n"
        ),
    ) == ("172.29.144.1", "172.30.0.1", "127.0.0.1")


def test_native_worker_callback_is_loopback_only():
    assert discover_worker_callback_hosts(
        wsl=False,
        route_output="default via 192.0.2.9 dev eth0",
    ) == ("127.0.0.1",)


def test_registry_publishes_actual_non_default_endpoint_and_tracks_change(tmp_path):
    registry = _registry(tmp_path)

    first = registry.publish(
        "workbench",
        instance_id="hashi3",
        host="172.29.144.1",
        port=32117,
    )
    second = registry.publish(
        "workbench",
        instance_id="HASHI3",
        host="172.29.144.2",
        port=43129,
    )

    assert first.base_url == "http://172.29.144.1:32117"
    assert second.base_url == "http://172.29.144.2:43129"
    assert second.revision > first.revision
    assert registry.resolve("workbench", expected_instance="HASHI3") == second
    persisted = load_service_endpoint(
        tmp_path / "state" / "service_endpoints.json",
        "workbench",
        expected_instance="HASHI3",
    )
    assert persisted == second
    payload = json.loads(
        (tmp_path / "state" / "service_endpoints.json").read_text(encoding="utf-8")
    )
    assert payload["services"]["workbench"]["port"] == 43129


def test_registry_and_serialized_snapshot_reject_cross_instance_routes(tmp_path):
    registry = _registry(tmp_path)
    registry.publish(
        "workbench",
        instance_id="HASHI3",
        host="127.0.0.1",
        port=32117,
    )

    with pytest.raises(ServiceEndpointError, match="cross-instance"):
        registry.resolve("workbench", expected_instance="HASHI1")

    snapshot = registry.snapshot()
    snapshot["services"]["workbench"]["instance_id"] = "HASHI1"
    with pytest.raises(ServiceEndpointError, match="cross-instance"):
        endpoint_from_snapshot(
            snapshot,
            "workbench",
            expected_instance="HASHI3",
        )


def test_registry_rejects_wrong_owner_and_unpublish_removes_live_route(tmp_path):
    registry = _registry(tmp_path)

    with pytest.raises(ServiceEndpointError, match="cross-instance"):
        registry.publish(
            "workbench",
            instance_id="HASHI2",
            host="127.0.0.1",
            port=32117,
        )

    registry.publish(
        "workbench",
        instance_id="HASHI3",
        host="127.0.0.1",
        port=32117,
    )
    registry.unpublish("workbench")
    with pytest.raises(ServiceEndpointError, match="unavailable"):
        registry.resolve("workbench")
