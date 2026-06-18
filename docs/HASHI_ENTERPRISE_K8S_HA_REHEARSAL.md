# HASHI Enterprise Kubernetes HA Rehearsal

This runbook rehearses a staging multi-replica HASHI Enterprise rollout using
the Helm chart, external database secret wiring, scheduler DB leases, optional
PostgreSQL lease pooling, PodDisruptionBudget, and audit export singleton
controls.

## Scope

This is a staging rehearsal, not a production certification. It validates the
deployment wiring and operator workflow before enabling multi-replica scheduler
leases in a real environment.

## Prerequisites

- Published HASHI enterprise image that includes optional `psycopg` and, if
  pool mode is enabled, `psycopg_pool`.
- Kubernetes namespace for the rehearsal.
- `hashi-enterprise-database` Secret containing
  `HASHI_ENTERPRISE_DATABASE_URL`.
- Enterprise schema and organization initialized for the target org.
- PostgreSQL lease rehearsal has passed:

```bash
python hashi.py enterprise lease-rehearse \
  --db-url "$HASHI_ENTERPRISE_DATABASE_URL" \
  --org-id ORG-001
```

## Install Or Upgrade

Use the rehearsal values file:

```bash
helm upgrade --install hashi-enterprise deploy/helm/hashi-enterprise \
  --namespace hashi-enterprise --create-namespace \
  --values deploy/helm/hashi-enterprise/examples/multi-replica-rehearsal.values.yaml \
  --set image.repository=ghcr.io/example/hashi-enterprise \
  --set image.tag=replace-me
```

## Checks

Confirm the control-plane rollout:

```bash
kubectl -n hashi-enterprise rollout status deploy/hashi-enterprise
kubectl -n hashi-enterprise get pods -l app.kubernetes.io/component=enterprise
```

Confirm the audit export daemon rollout if enabled:

```bash
kubectl -n hashi-enterprise rollout status deploy/hashi-enterprise-audit-export
```

Check lease-related environment on a pod:

```bash
kubectl -n hashi-enterprise exec deploy/hashi-enterprise -- env | grep HASHI_ENTERPRISE_SCHEDULER_LEASE
```

## Failure Rehearsal

Delete one control-plane pod and verify it is replaced without duplicate
scheduler trigger work:

```bash
POD=$(kubectl -n hashi-enterprise get pods \
  -l app.kubernetes.io/component=enterprise \
  -o jsonpath='{.items[0].metadata.name}')
kubectl -n hashi-enterprise delete pod "$POD"
kubectl -n hashi-enterprise rollout status deploy/hashi-enterprise
```

Review logs for lease skips/acquisitions:

```bash
kubectl -n hashi-enterprise logs deploy/hashi-enterprise --tail=200 | grep -E "scheduler.*lease|Skipping scheduler tick"
```

## Rollback

Disable multi-replica scheduler lease rehearsal settings first:

```bash
helm upgrade hashi-enterprise deploy/helm/hashi-enterprise \
  --namespace hashi-enterprise \
  --set replicaCount=1 \
  --set schedulerLease.enabled=false \
  --set schedulerLease.pool.enabled=false \
  --set podDisruptionBudget.enabled=false
```

Then inspect pod status and logs before reattempting the rehearsal.

## Known Limits

- This does not replace always-on PostgreSQL CI.
- This does not validate cloud-specific ingress or NetworkPolicy.
- The audit export daemon uses a singleton deployment plus DB lease controls;
  it does not move the audit ledger itself to PostgreSQL.
