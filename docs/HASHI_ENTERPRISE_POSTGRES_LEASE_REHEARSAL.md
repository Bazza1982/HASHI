# HASHI Enterprise PostgreSQL Lease Rehearsal

This runbook validates the enterprise DB lease path against a real PostgreSQL
database before enabling multi-replica scheduler leases.

## Prerequisites

- PostgreSQL database reachable from the operator shell.
- Optional Python driver installed in the HASHI environment:

```bash
python -m pip install "psycopg[binary]"
```

If you enable scheduler lease pooling, also install:

```bash
python -m pip install psycopg_pool
```

## One-Shot Rehearsal

Use a staging database URL, not production:

```bash
export HASHI_ENTERPRISE_POSTGRES_TEST_URL='postgresql://hashi:replace-me@localhost:5432/hashi_enterprise'
export HASHI_ENTERPRISE_POSTGRES_TEST_ORG_ID='ORG-001'
```

Run the CLI rehearsal:

```bash
python hashi.py enterprise lease-rehearse \
  --db-url "$HASHI_ENTERPRISE_POSTGRES_TEST_URL" \
  --org-id "$HASHI_ENTERPRISE_POSTGRES_TEST_ORG_ID"
```

Expected result:

- exactly one holder acquires the lease first;
- the other holder is blocked while the lease is active;
- the winning holder can renew;
- the winning holder can release;
- the blocked holder can acquire after release.

## Optional Pytest Check

The integration test is skipped unless `HASHI_ENTERPRISE_POSTGRES_TEST_URL` is
set:

```bash
HASHI_ENTERPRISE_POSTGRES_TEST_URL="$HASHI_ENTERPRISE_POSTGRES_TEST_URL" \
HASHI_ENTERPRISE_POSTGRES_TEST_ORG_ID="$HASHI_ENTERPRISE_POSTGRES_TEST_ORG_ID" \
pytest -q tests/test_enterprise_postgres_integration.py
```

## Always-On CI

The GitHub Actions workflow `.github/workflows/enterprise-postgres-lease.yml`
runs the same integration test against a `postgres:16` service container on
lease-related changes. It installs `psycopg[binary]`, sets
`HASHI_ENTERPRISE_POSTGRES_TEST_URL`, and exercises the acquire/block/renew/
release/takeover rehearsal path.

## Notes

- The rehearsal creates the `enterprise_leases` table if it does not exist.
- If your database already uses the full enterprise schema with organization
  foreign keys, ensure `HASHI_ENTERPRISE_POSTGRES_TEST_ORG_ID` exists first.
- Passing this rehearsal or the always-on CI workflow does not replace full
  multi-replica Kubernetes rollout testing; it only validates lease backend
  behavior.
- Scheduler lease pooling is optional and controlled by
  `HASHI_ENTERPRISE_SCHEDULER_LEASE_POOL_ENABLED`,
  `HASHI_ENTERPRISE_SCHEDULER_LEASE_POOL_MIN_SIZE`, and
  `HASHI_ENTERPRISE_SCHEDULER_LEASE_POOL_MAX_SIZE`.
