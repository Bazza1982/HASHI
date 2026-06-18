from __future__ import annotations

import os

import pytest

from orchestrator.enterprise import EnterpriseLeaseStore, run_enterprise_lease_rehearsal


POSTGRES_TEST_URL = os.environ.get("HASHI_ENTERPRISE_POSTGRES_TEST_URL")


@pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="set HASHI_ENTERPRISE_POSTGRES_TEST_URL to run PostgreSQL enterprise lease integration tests",
)
def test_enterprise_postgres_lease_rehearsal_against_real_database():
    pytest.importorskip("psycopg")
    org_id = os.environ.get("HASHI_ENTERPRISE_POSTGRES_TEST_ORG_ID", "ORG-001")
    store = EnterpriseLeaseStore.from_url(POSTGRES_TEST_URL, org_id=org_id)

    result = run_enterprise_lease_rehearsal(
        store,
        lease_name=os.environ.get("HASHI_ENTERPRISE_POSTGRES_TEST_LEASE", "pytest-postgres-lease-rehearsal"),
        holder_a="pytest-postgres-a",
        holder_b="pytest-postgres-b",
        ttl_seconds=30,
    )

    assert result.passed is True
    assert result.first_acquired_holder in {"pytest-postgres-a", "pytest-postgres-b"}
    assert result.blocked_holder in {"pytest-postgres-a", "pytest-postgres-b"}
    assert result.blocked_holder != result.first_acquired_holder
