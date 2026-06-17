from __future__ import annotations

from orchestrator.enterprise.store import SCHEMA_VERSION, EnterpriseStore


def test_enterprise_store_migrate_initializes_schema_and_version(tmp_path):
    store = EnterpriseStore(tmp_path / "enterprise.sqlite")

    result = store.migrate()

    assert result["before"] is None
    assert result["after"] == SCHEMA_VERSION
    assert store.schema_version() == SCHEMA_VERSION
    with store.connect() as con:
        row = con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'enterprise_leases'"
        ).fetchone()
    assert row is not None


def test_enterprise_store_migrate_is_idempotent(tmp_path):
    store = EnterpriseStore(tmp_path / "enterprise.sqlite")
    store.migrate()

    result = store.migrate()

    assert result["before"] == SCHEMA_VERSION
    assert result["after"] == SCHEMA_VERSION
