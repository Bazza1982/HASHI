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
- If `schedulerLease.backend=kubernetes` is enabled, the image must include
  `hashi-bridge[kubernetes]` or be built with
  `--build-arg HASHI_ENTERPRISE_EXTRAS=kubernetes`.
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
- Optional in-cluster lease load rehearsal has passed:

```bash
kubectl apply -f deploy/kubernetes/enterprise/lease-load-rehearsal-job.example.yaml
kubectl -n hashi-enterprise logs job/hashi-enterprise-lease-load-rehearsal
```

## Install Or Upgrade

Generate a JSON command plan if you want an operator-reviewed checklist before
touching the cluster:

```bash
python tools/enterprise_k8s_ha_rehearsal_plan.py \
  --image-repository ghcr.io/example/hashi-enterprise \
  --image-tag replace-me \
  --namespace hashi-enterprise \
  --output /tmp/hashi-k8s-ha-rehearsal-plan.json
```

Use the rehearsal values file:

```bash
helm upgrade --install hashi-enterprise deploy/helm/hashi-enterprise \
  --namespace hashi-enterprise --create-namespace \
  --values deploy/helm/hashi-enterprise/examples/multi-replica-rehearsal.values.yaml \
  --set image.repository=ghcr.io/example/hashi-enterprise \
  --set image.tag=replace-me
```

The rehearsal values also enable Kubernetes Lease RBAC so the service account
has `coordination.k8s.io/leases` permissions ready for future native leader
election. Runtime code includes an injectable Kubernetes Lease coordinator and
an optional Kubernetes API adapter. The rehearsal defaults keep the scheduler
on the enterprise DB lease path; set `schedulerLease.backend=kubernetes` only
after installing the optional Kubernetes Python package in the runtime image.

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

If using Helm, run the bounded in-cluster database lease load rehearsal before
scaling scheduler replicas:

```bash
helm upgrade hashi-enterprise deploy/helm/hashi-enterprise \
  --namespace hashi-enterprise \
  --set externalDatabase.enabled=true \
  --set leaseLoadRehearsal.enabled=true \
  --set leaseLoadRehearsal.leaseCount=16 \
  --set leaseLoadRehearsal.maxWorkers=4
kubectl -n hashi-enterprise logs job/hashi-enterprise-lease-load-rehearsal
```

For a Kubernetes Lease backend smoke rehearsal, run the CLI check before
enabling multiple scheduler replicas:

```bash
python tools/enterprise_k8s_backend_doctor.py --require-installed
python tools/enterprise_k8s_image_smoke_plan.py \
  --image-tag ghcr.io/your-org/hashi-enterprise:k8s-lease \
  --namespace hashi-enterprise \
  --lease-name superloop-scheduler-smoke

python hashi.py enterprise k8s-lease-rehearse \
  --namespace hashi-enterprise \
  --lease-name superloop-scheduler-smoke
```

After enabling the scheduler backend, inspect the live Lease object:

```bash
kubectl -n hashi-enterprise get lease superloop-scheduler -o yaml
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
- The generated HA rehearsal plan does not execute Helm or kubectl; it is an
  operator-reviewed command artifact.
- This does not validate cloud-specific ingress or NetworkPolicy.
- The audit export daemon uses a singleton deployment plus DB lease controls;
  it does not move the audit ledger itself to PostgreSQL.
- Kubernetes Lease RBAC, coordinator primitives, optional API adapter, and
  scheduler backend selection wiring exist, but live cluster rehearsal remains
  future work.
