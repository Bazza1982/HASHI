import asyncio
import sys
from types import SimpleNamespace

import pytest

from orchestrator.enterprise import EnterpriseLeaseStore, IdentityService, KubernetesApiLeaseClient
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


@pytest.mark.asyncio
async def test_start_workbench_api_uses_reloaded_module_class(tmp_path, monkeypatch):
    created = []

    class _ReloadedWorkbenchApiServer:
        def __init__(self, config_path, global_cfg, runtimes, *, secrets=None, orchestrator=None):
            created.append(
                {
                    "config_path": config_path,
                    "global_cfg": global_cfg,
                    "runtimes": runtimes,
                    "secrets": secrets,
                    "orchestrator": orchestrator,
                }
            )
            self.bind_host = "127.0.0.1"

        async def start(self):
            created[-1]["started"] = True

    fake_module = SimpleNamespace(WorkbenchApiServer=_ReloadedWorkbenchApiServer)
    monkeypatch.setitem(sys.modules, "orchestrator.workbench_api", fake_module)
    global_cfg = SimpleNamespace(workbench_port=18800)
    kernel = SimpleNamespace(
        paths=SimpleNamespace(config_path=tmp_path / "config.yaml"),
        runtimes=[SimpleNamespace(name="zelda")],
        workbench_api=None,
    )
    manager = ServiceManager(kernel)

    await manager.start_workbench_api(global_cfg, {"token": "secret"})

    assert isinstance(kernel.workbench_api, _ReloadedWorkbenchApiServer)
    assert created == [
        {
            "config_path": tmp_path / "config.yaml",
            "global_cfg": global_cfg,
            "runtimes": kernel.runtimes,
            "secrets": {"token": "secret"},
            "orchestrator": kernel,
            "started": True,
        }
    ]


@pytest.mark.asyncio
async def test_restart_scheduler_recreates_workbench_before_scheduler(tmp_path, monkeypatch):
    events = []
    manager = _manager(tmp_path)
    manager.kernel.global_cfg = SimpleNamespace(
        authorized_id=123,
        enterprise_scheduler_lease_enabled=False,
    )
    manager.kernel.secrets = {"workbench_admin_token": "secret"}
    manager.kernel.runtimes = []
    manager.kernel.skill_manager = object()
    manager.kernel.workbench_api = object()
    manager.kernel.delivery_health_task = None
    manager.kernel.background_job_manager = None

    async def fake_restart_workbench_api():
        events.append("workbench")

    async def fake_restart_api_gateway():
        events.append("api_gateway")

    async def fake_stop_scheduler():
        events.append("stop_scheduler")
        manager.kernel.scheduler_task = None
        manager.kernel.scheduler = None

    async def fake_restart_delivery_health_watcher():
        events.append("delivery")

    async def fake_restart_background_jobs():
        events.append("background")

    class _Scheduler:
        def __init__(self, *args, **kwargs):
            events.append("scheduler_init")

        async def run(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(manager, "restart_workbench_api", fake_restart_workbench_api)
    monkeypatch.setattr(manager, "restart_api_gateway", fake_restart_api_gateway)
    monkeypatch.setattr(manager, "stop_scheduler", fake_stop_scheduler)
    monkeypatch.setattr(manager, "restart_delivery_health_watcher", fake_restart_delivery_health_watcher)
    monkeypatch.setattr(manager, "restart_background_jobs", fake_restart_background_jobs)
    monkeypatch.setitem(sys.modules, "orchestrator.scheduler", SimpleNamespace(TaskScheduler=_Scheduler))

    await manager.restart_scheduler()

    assert events == [
        "workbench",
        "api_gateway",
        "stop_scheduler",
        "scheduler_init",
        "delivery",
        "background",
    ]
    manager.kernel.scheduler_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await manager.kernel.scheduler_task


@pytest.mark.asyncio
async def test_restart_api_gateway_recreates_enabled_gateway_from_reloaded_module(tmp_path, monkeypatch):
    events = []

    class _OldGateway:
        async def stop(self):
            events.append("old_stop")

    class _ReloadedGateway:
        def __init__(self, global_cfg, secrets, workspace_root, default_model=None):
            events.append(("new_init", global_cfg, secrets, workspace_root, default_model))
            self.bind_host = "127.0.0.1"

        async def start(self):
            events.append("new_start")

    global_cfg = SimpleNamespace(api_gateway_port=18803)
    kernel = SimpleNamespace(
        paths=SimpleNamespace(
            bridge_home=tmp_path,
            workspaces_root=tmp_path / "workspaces",
        ),
        global_cfg=global_cfg,
        secrets={"xai_api_key": "secret"},
        api_gateway=_OldGateway(),
        enable_api_gateway=True,
    )
    manager = ServiceManager(kernel)
    monkeypatch.setitem(
        sys.modules,
        "orchestrator.api_gateway",
        SimpleNamespace(APIGatewayServer=_ReloadedGateway),
    )
    monkeypatch.setattr(
        manager,
        "_load_api_gateway_state",
        lambda: {"enabled": True, "default_model": "grok-4.3"},
    )

    await manager.restart_api_gateway()

    assert isinstance(kernel.api_gateway, _ReloadedGateway)
    assert events == [
        "old_stop",
        (
            "new_init",
            global_cfg,
            {"xai_api_key": "secret"},
            tmp_path / "workspaces",
            "grok-4.3",
        ),
        "new_start",
    ]


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
    assert kwargs["enterprise_lease_store"].store.db_path == db_path


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

    assert str(manager._resolve_enterprise_database_path(raw_url)) == expected


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
async def test_stop_scheduler_closes_enterprise_lease_store(tmp_path):
    manager = _manager(tmp_path)
    closed = {"value": False}

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
