from __future__ import annotations

import json
import os
import threading
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


def _hold_windows_read_lock(path):
    import ctypes
    from ctypes import wintypes
    win = ctypes.WinDLL("kernel32", use_last_error=True)
    win.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    win.CreateFileW.restype = wintypes.HANDLE
    win.CloseHandle.argtypes = [wintypes.HANDLE]
    win.CloseHandle.restype = wintypes.BOOL
    # A genuine reader shares reads/writes, but not rename/delete.
    handle = win.CreateFileW(str(path), 0x80000000, 3, None, 3, 0, None)
    assert handle != wintypes.HANDLE(-1).value
    closed = False
    lock = threading.Lock()
    def release():
        nonlocal closed
        with lock:
            if not closed:
                assert win.CloseHandle(handle)
                closed = True
    return release


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing contract")
def test_live_endpoint_publication_survives_real_temporary_windows_read_lock(tmp_path):
    registry = _registry(tmp_path)
    old = registry.publish("workbench", instance_id="HASHI3", host="127.0.0.1", port=32117)
    release = _hold_windows_read_lock(registry.state_path)
    timer = threading.Timer(.12, release)
    timer.start()
    try:
        new = registry.publish("workbench", instance_id="HASHI3", host="127.0.0.1", port=43129)
    finally:
        timer.cancel()
        timer.join(timeout=1)
        release()
    assert new.revision == old.revision + 1
    assert registry.resolve("workbench") == new
    assert load_service_endpoint(registry.state_path, "workbench", expected_instance="HASHI3") == new


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing contract")
def test_persistent_windows_read_lock_refuses_without_publishing_a_phantom_route(tmp_path):
    registry = _registry(tmp_path)
    old = registry.publish("workbench", instance_id="HASHI3", host="127.0.0.1", port=32117)
    before = registry.snapshot()
    raw = registry.state_path.read_bytes()
    release = _hold_windows_read_lock(registry.state_path)
    try:
        with pytest.raises(PermissionError):
            registry.publish("workbench", instance_id="HASHI3", host="127.0.0.1", port=43129)
    finally:
        release()
    assert registry.snapshot() == before
    assert registry.resolve("workbench") == old
    assert registry.state_path.read_bytes() == raw


@pytest.mark.parametrize("operation", ["publish", "unpublish"])
def test_failed_endpoint_publication_preserves_durable_and_live_snapshot(tmp_path, monkeypatch, operation):
    registry = _registry(tmp_path)
    old = registry.publish("workbench", instance_id="HASHI3", host="127.0.0.1", port=32117)
    before = registry.snapshot()
    raw = registry.state_path.read_bytes()
    replacements = []
    def reject(source, destination):
        replacements.append((source, destination))
        raise OSError("permanent write failure")
    monkeypatch.setattr("orchestrator.service_endpoints.os.replace", reject)
    with pytest.raises(OSError, match="permanent write failure"):
        if operation == "publish":
            registry.publish("workbench", instance_id="HASHI3", host="127.0.0.1", port=43129)
        else:
            registry.unpublish("workbench")
    assert len(replacements) == 1
    assert registry.snapshot() == before
    assert registry.resolve("workbench") == old
    assert registry.state_path.read_bytes() == raw
    assert not list(registry.state_path.parent.glob(".service_endpoints.json.*"))


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
