import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.enterprise import (
    EnterpriseLeaseStore,
    IdentityService,
    KubernetesApiLeaseClient,
)
from orchestrator.service_manager import ServiceManager


def _manager(tmp_path):
    kernel = SimpleNamespace(
        paths=SimpleNamespace(
            bridge_home=tmp_path,
            tasks_path=tmp_path / "tasks.json",
            state_path=tmp_path / "scheduler_state.json",
        )
    )
    return ServiceManager(kernel)


def test_api_gateway_state_uses_canonical_config_after_legacy_migration(tmp_path):
    manager = _manager(tmp_path)
    legacy_path = tmp_path / "api_gateway_state.json"
    legacy_path.write_text(
        json.dumps({"enabled": True, "default_model": "grok-4.5"}),
        encoding="utf-8",
    )
    state = manager._load_api_gateway_state()
    assert state["enabled"] is True
    assert state["default_model"] == "grok-4.5"
    assert manager._api_gateway_state_path() == tmp_path / "state" / "api_gateway_config.json"

    manager._save_api_gateway_state(enabled=False)
    canonical = json.loads(manager._api_gateway_state_path().read_text(encoding="utf-8"))
    legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    assert canonical["enabled"] is False
    assert legacy["enabled"] is True


def test_core_service_manager_exposes_no_function_generation_refresh_api(tmp_path):
    manager = _manager(tmp_path)
    obsolete = {
        "refresh_hot_services",
        "restart_scheduler",
        "restart_workbench_api",
        "restart_api_gateway",
        "restart_whatsapp_transport",
        "restart_delivery_health_watcher",
        "restart_background_jobs",
        "assert_hot_services_healthy",
        "repair_workbench_api_if_needed",
    }
    assert not (obsolete & set(dir(manager)))


@pytest.mark.asyncio
async def test_start_workbench_api_constructs_the_core_service(tmp_path, monkeypatch):
    created = []

    class _WorkbenchApiServer:
        def __init__(
            self,
            config_path,
            global_cfg,
            runtimes,
            *,
            secrets=None,
            orchestrator=None,
            reconcile_session_runs=True,
        ):
            created.append(
                (
                    config_path,
                    global_cfg,
                    runtimes,
                    secrets,
                    orchestrator,
                    reconcile_session_runs,
                )
            )
            self.bind_host = "127.0.0.1"

        async def start(self):
            created.append("started")

    monkeypatch.setitem(
        sys.modules,
        "orchestrator.workbench_api",
        SimpleNamespace(WorkbenchApiServer=_WorkbenchApiServer),
    )
    global_cfg = SimpleNamespace(workbench_port=18800)
    kernel = SimpleNamespace(
        paths=SimpleNamespace(config_path=tmp_path / "config.yaml"),
        runtimes=[SimpleNamespace(name="zelda")],
        workbench_api=None,
    )
    manager = ServiceManager(kernel)

    await manager.start_workbench_api(global_cfg, {"token": "secret"})

    assert isinstance(kernel.workbench_api, _WorkbenchApiServer)
    assert created == [
        (
            tmp_path / "config.yaml",
            global_cfg,
            kernel.runtimes,
            {"token": "secret"},
            kernel,
            False,
        ),
        "started",
    ]


@pytest.mark.asyncio
async def test_start_workbench_api_is_idempotent(tmp_path, monkeypatch):
    existing = SimpleNamespace(bind_host="127.0.0.1", bound_port=18804)
    kernel = SimpleNamespace(
        paths=SimpleNamespace(config_path=tmp_path / "agents.json"),
        runtimes=[],
        workbench_api=existing,
    )
    manager = ServiceManager(kernel)

    def unexpected_server_lookup():
        raise AssertionError("an existing Core service must not be replaced")

    monkeypatch.setattr(manager, "_workbench_api_server_cls", unexpected_server_lookup)

    await manager.start_workbench_api(
        SimpleNamespace(workbench_port=18804),
        {},
    )

    assert kernel.workbench_api is existing


@pytest.mark.asyncio
async def test_start_workbench_rolls_back_bound_server_if_endpoint_publish_fails(
    tmp_path,
    monkeypatch,
):
    events = []

    class _WorkbenchApiServer:
        def __init__(self, *_args, **_kwargs):
            self.bind_host = "127.0.0.1"
            self.bound_port = 24567

        async def start(self):
            events.append("started")

        async def shutdown(self):
            events.append("shutdown")

    class _Registry:
        def publish(self, *_args, **_kwargs):
            raise RuntimeError("publication failed")

        def unpublish(self, service):
            events.append(f"unpublished:{service}")

    broker = SimpleNamespace(stop=lambda: events.append("broker-stopped"))
    monkeypatch.setitem(
        sys.modules,
        "orchestrator.workbench_api",
        SimpleNamespace(WorkbenchApiServer=_WorkbenchApiServer),
    )
    global_cfg = SimpleNamespace(instance_id="HASHI3", workbench_port=18804)
    kernel = SimpleNamespace(
        paths=SimpleNamespace(
            config_path=tmp_path / "agents.json",
            instance_id="HASHI3",
        ),
        runtimes=[],
        workbench_api=None,
        endpoint_registry=_Registry(),
        capability_broker=broker,
    )

    await ServiceManager(kernel).start_workbench_api(global_cfg, {})

    assert kernel.workbench_api is None
    assert events == [
        "started",
        "broker-stopped",
        "unpublished:workbench",
        "shutdown",
    ]


@pytest.mark.asyncio
async def test_start_workbench_publishes_actual_port_then_starts_broker(
    tmp_path,
    monkeypatch,
):
    events = []
    published_endpoint = SimpleNamespace(port=24567)

    class _WorkbenchApiServer:
        def __init__(self, *_args, **_kwargs):
            self.bind_host = "172.29.144.7"
            self.bound_port = 24567

        async def start(self):
            events.append("started")

    class _Registry:
        def publish(self, service, **kwargs):
            events.append((service, kwargs))
            return published_endpoint

    class _Broker:
        def start(self, endpoint):
            events.append(("broker", endpoint))

    monkeypatch.setitem(
        sys.modules,
        "orchestrator.workbench_api",
        SimpleNamespace(WorkbenchApiServer=_WorkbenchApiServer),
    )
    global_cfg = SimpleNamespace(instance_id="HASHI3", workbench_port=18804)
    kernel = SimpleNamespace(
        paths=SimpleNamespace(
            config_path=tmp_path / "agents.json",
            instance_id="HASHI3",
        ),
        runtimes=[],
        workbench_api=None,
        endpoint_registry=_Registry(),
        capability_broker=_Broker(),
    )

    await ServiceManager(kernel).start_workbench_api(global_cfg, {})

    assert events[0] == "started"
    assert events[1] == (
        "workbench",
        {
            "instance_id": "HASHI3",
            "host": "172.29.144.7",
            "port": 24567,
            "metadata": {"pid": os.getpid()},
        },
    )
    assert events[2] == ("broker", published_endpoint)


@pytest.mark.asyncio
async def test_stop_api_gateway_uses_the_stable_core_shutdown_contract(tmp_path):
    events = []

    class _Gateway:
        shutdown_budget_sec = 0.2

        async def stop(self):
            events.append("stopped")

    kernel = SimpleNamespace(api_gateway=_Gateway())
    manager = ServiceManager(kernel)

    assert await manager.stop_api_gateway(timeout=0.1) is True
    assert events == ["stopped"]
    assert kernel.api_gateway is None


def test_scheduler_enterprise_lease_kwargs_disabled_by_default(tmp_path):
    manager = _manager(tmp_path)
    global_cfg = SimpleNamespace(enterprise_scheduler_lease_enabled=False)
    assert manager._scheduler_enterprise_lease_kwargs(global_cfg) == {}


def test_scheduler_enterprise_lease_kwargs_builds_sqlite_store(tmp_path):
    manager = _manager(tmp_path)
    db_path = tmp_path / "enterprise.sqlite"
    IdentityService.from_path(db_path).create_organization(org_id="ORG-777", name="Acme")
    global_cfg = SimpleNamespace(
        enterprise_scheduler_lease_enabled=True,
        enterprise_database_url=f"sqlite:///{db_path}",
        organization_id="ORG-777",
        instance_id="HASHI1",
        enterprise_scheduler_lease_name="scheduler-main",
        enterprise_scheduler_lease_holder="pod-a",
        enterprise_scheduler_lease_ttl_seconds=45,
    )

    kwargs = manager._scheduler_enterprise_lease_kwargs(global_cfg)

    assert kwargs["enterprise_lease_name"] == "scheduler-main"
    assert kwargs["enterprise_lease_holder"] == "pod-a"
    assert kwargs["enterprise_lease_ttl_seconds"] == 45
    attempt = kwargs["enterprise_lease_store"].acquire(
        "scheduler-main",
        holder_id="pod-a",
        ttl_seconds=45,
    )
    assert attempt.acquired is True


def test_scheduler_enterprise_lease_kwargs_uses_bridge_home_default_db(tmp_path):
    manager = _manager(tmp_path)
    db_path = tmp_path / "state" / "enterprise.sqlite"
    IdentityService.from_path(db_path).create_organization(org_id="ORG-001", name="Acme")
    global_cfg = SimpleNamespace(
        enterprise_scheduler_lease_enabled=True,
        enterprise_database_url=None,
        organization_id=None,
        instance_id="HASHI1",
        enterprise_scheduler_lease_name="superloop-scheduler",
        enterprise_scheduler_lease_holder=None,
        enterprise_scheduler_lease_ttl_seconds=60,
    )

    kwargs = manager._scheduler_enterprise_lease_kwargs(global_cfg)

    assert kwargs["enterprise_lease_name"] == "superloop-scheduler"
    assert kwargs["enterprise_lease_holder"].startswith("HASHI1:")
    assert Path(kwargs["enterprise_lease_store"].store.db_path) == db_path


def test_scheduler_enterprise_lease_kwargs_skips_unsupported_database_url(tmp_path):
    manager = _manager(tmp_path)
    global_cfg = SimpleNamespace(
        enterprise_scheduler_lease_enabled=True,
        enterprise_database_url="postgresql://hashi@example.invalid/hashi",
    )
    assert manager._scheduler_enterprise_lease_kwargs(global_cfg) == {}


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        ("sqlite:////data/state/enterprise.sqlite", "/data/state/enterprise.sqlite"),
        ("sqlite:///relative/state.sqlite", "relative/state.sqlite"),
        ("state/enterprise.sqlite", "state/enterprise.sqlite"),
    ],
)
def test_scheduler_enterprise_database_path_resolution(tmp_path, raw_url, expected):
    manager = _manager(tmp_path)
    assert manager._resolve_enterprise_database_path(raw_url) == Path(expected)


def test_scheduler_enterprise_lease_kwargs_passes_postgres_pool_options(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    calls = {}
    fake_store = SimpleNamespace()

    def fake_from_url(database_url, **kwargs):
        calls["database_url"] = database_url
        calls.update(kwargs)
        return fake_store

    monkeypatch.setattr(EnterpriseLeaseStore, "from_url", staticmethod(fake_from_url))
    global_cfg = SimpleNamespace(
        enterprise_scheduler_lease_enabled=True,
        enterprise_database_url="postgresql://hashi@example.invalid/hashi",
        organization_id="ORG-001",
        instance_id="HASHI1",
        enterprise_scheduler_lease_name="scheduler-main",
        enterprise_scheduler_lease_holder="pod-a",
        enterprise_scheduler_lease_ttl_seconds=45,
        enterprise_scheduler_lease_pool_enabled=True,
        enterprise_scheduler_lease_pool_min_size=2,
        enterprise_scheduler_lease_pool_max_size=8,
    )

    kwargs = manager._scheduler_enterprise_lease_kwargs(global_cfg)

    assert kwargs["enterprise_lease_store"] is fake_store
    assert calls == {
        "database_url": "postgresql://hashi@example.invalid/hashi",
        "org_id": "ORG-001",
        "postgres_pool": True,
        "postgres_pool_min_size": 2,
        "postgres_pool_max_size": 8,
    }


def test_scheduler_enterprise_lease_kwargs_builds_kubernetes_store(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    calls = {}

    class _Client:
        def __init__(self):
            self.leases = {}

        def get_lease(self, namespace, name):
            return self.leases.get((namespace, name))

        def create_lease(self, lease):
            self.leases[(lease.namespace, lease.name)] = lease
            return lease

        def replace_lease(self, lease):
            self.leases[(lease.namespace, lease.name)] = lease
            return lease

        def delete_lease(self, namespace, name, *, holder_identity):
            lease = self.leases.get((namespace, name))
            if lease is None or lease.holder_identity != holder_identity:
                return False
            del self.leases[(namespace, name)]
            return True

    def fake_from_config(*, in_cluster, kubeconfig_path):
        calls["in_cluster"] = in_cluster
        calls["kubeconfig_path"] = kubeconfig_path
        return _Client()

    monkeypatch.setattr(KubernetesApiLeaseClient, "from_config", staticmethod(fake_from_config))
    global_cfg = SimpleNamespace(
        enterprise_scheduler_lease_enabled=True,
        enterprise_scheduler_lease_backend="kubernetes",
        enterprise_scheduler_lease_name="scheduler-main",
        enterprise_scheduler_lease_holder="pod-a",
        enterprise_scheduler_lease_ttl_seconds=45,
        enterprise_scheduler_lease_kubernetes_namespace="hashi-enterprise",
        enterprise_scheduler_lease_kubernetes_in_cluster=False,
        enterprise_scheduler_lease_kubeconfig_path="/tmp/kubeconfig",
        instance_id="HASHI1",
    )

    kwargs = manager._scheduler_enterprise_lease_kwargs(global_cfg)
    attempt = kwargs["enterprise_lease_store"].acquire(
        "scheduler-main",
        holder_id="pod-a",
        ttl_seconds=45,
        metadata={"component": "task-scheduler"},
    )

    assert kwargs["enterprise_lease_name"] == "scheduler-main"
    assert kwargs["enterprise_lease_holder"] == "pod-a"
    assert kwargs["enterprise_lease_ttl_seconds"] == 45
    assert attempt.acquired is True
    assert calls == {"in_cluster": False, "kubeconfig_path": "/tmp/kubeconfig"}


def test_scheduler_enterprise_lease_kwargs_rejects_unknown_backend(tmp_path):
    manager = _manager(tmp_path)
    global_cfg = SimpleNamespace(
        enterprise_scheduler_lease_enabled=True,
        enterprise_scheduler_lease_backend="zookeeper",
    )
    assert manager._scheduler_enterprise_lease_kwargs(global_cfg) == {}


@pytest.mark.asyncio
async def test_stop_scheduler_closes_enterprise_lease_store_without_false_timeout_warning(
    tmp_path,
    caplog,
):
    manager = _manager(tmp_path)
    closed = {"value": False}
    caplog.set_level(logging.INFO, logger="BridgeU.Bridge")

    class _Store:
        def close(self):
            closed["value"] = True

    async def _sleep_forever():
        await asyncio.sleep(60)

    manager.kernel.scheduler = SimpleNamespace(enterprise_lease_store=_Store())
    manager.kernel.scheduler_task = asyncio.create_task(_sleep_forever())

    await manager.stop_scheduler(timeout=0.1)

    assert closed["value"] is True
    assert manager.kernel.scheduler is None
    assert manager.kernel.scheduler_task is None
    assert "Scheduler task stopped after cancellation" in caplog.text
    assert "did not stop within" not in caplog.text
