import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.enterprise import EnterpriseLeaseStore, IdentityService
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
