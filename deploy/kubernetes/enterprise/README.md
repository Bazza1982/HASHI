# HASHI Enterprise Kubernetes Baseline

This directory contains a minimal Kubernetes baseline for running HASHI in
`enterprise` profile. It mirrors the Docker Compose deployment shape:

- Workbench listens on port `18800`.
- `/api/health` is used for liveness and readiness probes.
- `HASHI_DEPLOYMENT_PROFILE=enterprise`.
- `HASHI_BRIDGE_HOME=/data`.
- State, workspaces, logs, and backups are persisted under `/data`.
- Connector and deployment secrets are mounted as files, not embedded in the
  manifests.
- `audit-export-cronjob.yaml` runs `hashi enterprise audit-export-live` on a
  schedule and stores its checkpoint under `/data/state`.
- `audit-export-daemon.deployment.yaml` is an optional long-running daemon
  alternative to the CronJob. Do not enable both for the same ledger.
- `external-postgres-secret.example.yaml` shows the `HASHI_ENTERPRISE_DATABASE_URL`
  contract for managed database staging.
- `pod-disruption-budget.example.yaml` is an optional availability guard for
  multi-replica staging and production maintenance windows.

This is not a full HA release. A Helm baseline is available at
`deploy/helm/hashi-enterprise`; autoscaling, multi-replica coordination,
managed ingress policy, cluster-validated network policy, and database
migration rehearsal remain production hardening steps.

## Apply

```bash
kubectl apply -k deploy/kubernetes/enterprise
```

Before applying in a real environment:

1. Build and publish a HASHI enterprise image.
2. Replace the image in `deployment.yaml`.
3. Create real secrets from your secret manager instead of using
   `secret.example.yaml`.
4. Set a production namespace, storage class, ingress, and network policy.
5. Replace `HASHI_AUDIT_EXPORT_ENDPOINT` and `HASHI_AUDIT_EXPORT_HEADER` in
   your secret manager before enabling the audit export CronJob.
6. For multi-replica staging, replace the local SQLite URL with a managed
   database URL secret based on `external-postgres-secret.example.yaml`.
7. If running two or more control-plane replicas, apply and tune
   `pod-disruption-budget.example.yaml` for voluntary disruption protection.
8. Enable `HASHI_ENTERPRISE_SCHEDULER_LEASE_ENABLED` only after validating the
   enterprise schema and lease backend used by your runtime.

## External Database Secret

The deployment consumes `HASHI_ENTERPRISE_DATABASE_URL` through
`hashi-enterprise-secrets` by default. For managed PostgreSQL, create a real
secret-manager backed secret with the same key:

```bash
kubectl apply -f deploy/kubernetes/enterprise/external-postgres-secret.example.yaml
```

Then either copy that key into `hashi-enterprise-secrets` for this raw-manifest
baseline, or use the Helm chart's `externalDatabase.enabled=true` override to
mount a dedicated database secret.

## Scheduler Lease Guard

Control-plane pods include pod-name holder wiring for scheduler leases. The raw
baseline keeps `HASHI_ENTERPRISE_SCHEDULER_LEASE_ENABLED` set to `"false"` by
default; set it to `"true"` only after the enterprise database schema is
initialized and the runtime lease backend has been validated. The Python lease
store supports SQLite paths, `sqlite:///` URLs, and PostgreSQL URLs when the
optional `psycopg` package is installed.

## Live Audit Export Daemon

For continuous export instead of scheduled one-shot jobs:

```bash
kubectl apply -f deploy/kubernetes/enterprise/audit-export-daemon.deployment.yaml
```

The daemon manifest passes `--db-lease-name audit-export` and uses the pod name
as `--db-lease-holder`, so initialize the enterprise schema before enabling it:

```bash
python hashi.py enterprise migrate --db /data/state/enterprise.sqlite
```

Use either the CronJob or daemon Deployment for a given HASHI instance. Running
both can race on the same checkpoint and duplicate delivery attempts.
