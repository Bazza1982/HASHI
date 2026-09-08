from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator.service_endpoints import ServiceEndpointError
from scripts.check_function_worker_runtime import _install_service_endpoints


class _Registry:
    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, service: str, **kwargs):
        self.published.append({"service": service, **kwargs})


def _snapshot(instance_id: str = "HASHI1") -> dict:
    return {
        "schema_version": 1,
        "instance_id": instance_id,
        "revision": 1,
        "services": {
            "workbench": {
                "service": "workbench",
                "instance_id": instance_id,
                "scheme": "http",
                "host": "127.0.0.1",
                "port": 18800,
                "revision": 1,
                "published_at": 1.0,
                "metadata": {"pid": 123},
            }
        },
    }


def test_preflight_installs_validated_workbench_endpoint(tmp_path):
    path = tmp_path / "service_endpoints.json"
    path.write_text(json.dumps(_snapshot()), encoding="utf-8")
    registry = _Registry()
    kernel = SimpleNamespace(
        global_cfg=SimpleNamespace(instance_id="HASHI1"),
        endpoint_registry=registry,
    )

    _install_service_endpoints(kernel, path)

    assert registry.published == [
        {
            "service": "workbench",
            "instance_id": "HASHI1",
            "scheme": "http",
            "host": "127.0.0.1",
            "port": 18800,
            "metadata": {"pid": 123},
        }
    ]


def test_preflight_rejects_cross_instance_service_snapshot(tmp_path):
    path = tmp_path / "service_endpoints.json"
    path.write_text(json.dumps(_snapshot("HASHI2")), encoding="utf-8")
    kernel = SimpleNamespace(
        global_cfg=SimpleNamespace(instance_id="HASHI1"),
        endpoint_registry=_Registry(),
    )

    with pytest.raises(ServiceEndpointError, match="cross-instance"):
        _install_service_endpoints(kernel, path)
