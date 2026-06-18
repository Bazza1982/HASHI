# HASHI Enterprise Helm Baseline

This chart packages the HASHI Enterprise AAI control plane for Kubernetes. It is
the next production step after the raw manifests in `deploy/kubernetes`.

The chart provides:

- configurable image, service, resource, and probe settings;
- `enterprise` profile environment wiring;
- persistent `/data` storage;
- read-only mounted connector secrets;
- optional ingress;
- optional NetworkPolicy;
- optional HPA skeleton.
- optional PodDisruptionBudget for multi-replica maintenance windows.
- optional live audit export CronJob that runs `hashi enterprise
  audit-export-live` with a persistent checkpoint under `/data/state`.
- optional external database SecretRef wiring for multi-replica staging.

It is still a baseline chart. Multi-replica correctness depends on external
state coordination, external database wiring, and task/queue coordination work
that is tracked separately in the Enterprise roadmap.

## Render

```bash
helm template hashi-enterprise deploy/helm/hashi-enterprise
```

## Install

```bash
helm upgrade --install hashi-enterprise deploy/helm/hashi-enterprise \
  --namespace hashi-enterprise --create-namespace \
  --set image.repository=ghcr.io/your-org/hashi-enterprise \
  --set image.tag=replace-me
```

Before production use:

1. Replace `templates/secret.example.yaml` with secret-manager managed values.
2. Set ingress TLS and NetworkPolicy rules for your cluster.
3. Use external state services before increasing `replicaCount`.
4. Run backup/restore and migration rehearsals against the target environment.
5. If using `schedulerLease.backend=kubernetes`, build the image with
   `--build-arg HASHI_ENTERPRISE_EXTRAS=kubernetes` or otherwise install
   `hashi-bridge[kubernetes]`.

## External Database Wiring

The default chart uses a local SQLite URL from the example secret. For
multi-replica staging, provide a managed database URL through a dedicated
Kubernetes Secret and enable the external database override:

```bash
kubectl apply -f deploy/helm/hashi-enterprise/examples/external-postgres-secret.kubernetes.yaml
```

```bash
helm upgrade --install hashi-enterprise deploy/helm/hashi-enterprise \
  --namespace hashi-enterprise --create-namespace \
  --set replicaCount=2 \
  --set externalDatabase.enabled=true \
  --set externalDatabase.secretName=hashi-enterprise-database \
  --set podDisruptionBudget.enabled=true
```

This only wires `HASHI_ENTERPRISE_DATABASE_URL` into the control-plane pods.
Treat production rollout as blocked until schema migrations, backup/restore,
connection pooling, and multi-replica coordination have been rehearsed against
the target database.

Scheduler lease settings are exposed under `schedulerLease`. They default to
disabled and use the pod name as holder identity when enabled:

```bash
helm upgrade --install hashi-enterprise deploy/helm/hashi-enterprise \
  --namespace hashi-enterprise --create-namespace \
  --set schedulerLease.enabled=true \
  --set schedulerLease.name=superloop-scheduler
```

Enable this only after the enterprise schema is initialized and the runtime
lease backend has been validated. The Python lease store supports SQLite paths,
`sqlite:///` URLs, and PostgreSQL URLs when the optional `psycopg` package is
installed.
PostgreSQL scheduler lease pooling is available under `schedulerLease.pool` and
requires optional `psycopg_pool`.

Before enabling scheduler leases against a staging database, run:

```bash
python hashi.py enterprise lease-rehearse \
  --db-url "$HASHI_ENTERPRISE_DATABASE_URL" \
  --org-id ORG-001
```

For a full PostgreSQL rehearsal checklist, see
`docs/HASHI_ENTERPRISE_POSTGRES_LEASE_REHEARSAL.md`.

For a full multi-replica staging rehearsal, start from
`examples/multi-replica-rehearsal.values.yaml` and follow
`docs/HASHI_ENTERPRISE_K8S_HA_REHEARSAL.md`.
Optional Kubernetes Lease RBAC can be enabled with
`leaderElection.rbac.enabled=true`. The scheduler lease backend defaults to
`db`; set `schedulerLease.backend=kubernetes` only when the runtime image
includes the optional Kubernetes Python package and the service account has
Lease permissions.

Example Kubernetes Lease backend install:

```bash
python tools/enterprise_k8s_backend_doctor.py --json
python tools/enterprise_k8s_image_smoke_plan.py \
  --image-tag ghcr.io/your-org/hashi-enterprise:k8s-lease \
  --namespace hashi-enterprise \
  --lease-name superloop-scheduler-smoke

docker build -f Dockerfile.enterprise \
  --build-arg HASHI_ENTERPRISE_EXTRAS=kubernetes \
  -t ghcr.io/your-org/hashi-enterprise:k8s-lease .

helm upgrade --install hashi-enterprise deploy/helm/hashi-enterprise \
  --namespace hashi-enterprise --create-namespace \
  --set image.repository=ghcr.io/your-org/hashi-enterprise \
  --set image.tag=k8s-lease \
  --set schedulerLease.enabled=true \
  --set schedulerLease.backend=kubernetes \
  --set leaderElection.rbac.enabled=true
```

Before enabling multiple scheduler replicas with this backend, run:

```bash
python hashi.py enterprise k8s-lease-rehearse \
  --namespace hashi-enterprise \
  --lease-name superloop-scheduler-smoke
```

## Live Audit Export

Enable the one-shot exporter as a Kubernetes CronJob after configuring a real
SIEM/OTLP endpoint and authorization header. For production, store those values
in a Kubernetes Secret:

```bash
kubectl apply -f deploy/helm/hashi-enterprise/examples/audit-export-secret.kubernetes.yaml
```

```bash
helm upgrade --install hashi-enterprise deploy/helm/hashi-enterprise \
  --namespace hashi-enterprise --create-namespace \
  --set auditExport.enabled=true \
  --set auditExport.endpointSecretRef.name=hashi-audit-export \
  --set auditExport.headerSecretRef.name=hashi-audit-export
```

The CronJob uses `concurrencyPolicy: Forbid` and advances
`/data/state/audit_live_export_checkpoint.json` only after a successful export
cycle.

For a long-running daemon instead of a CronJob:

```bash
helm upgrade --install hashi-enterprise deploy/helm/hashi-enterprise \
  --namespace hashi-enterprise --create-namespace \
  --set auditExport.daemon.enabled=true \
  --set auditExport.endpointSecretRef.name=hashi-audit-export \
  --set auditExport.headerSecretRef.name=hashi-audit-export
```

Use either `auditExport.enabled=true` for the CronJob or
`auditExport.daemon.enabled=true` for the daemon Deployment. Do not enable both
against the same ledger/checkpoint.

For multi-replica daemon staging with a shared enterprise database, enable the
DB lease arguments and keep the holder tied to the pod name:

```bash
helm upgrade --install hashi-enterprise deploy/helm/hashi-enterprise \
  --namespace hashi-enterprise --create-namespace \
  --set auditExport.daemon.enabled=true \
  --set auditExport.daemon.dbLease.enabled=true \
  --set auditExport.daemon.dbLease.name=audit-export \
  --set auditExport.endpointSecretRef.name=hashi-audit-export \
  --set auditExport.headerSecretRef.name=hashi-audit-export
```

Run `hashi enterprise migrate` against the target database before enabling the
daemon lease path.

If your cluster uses External Secrets Operator, adapt and apply:

```bash
# Choose one SecretStore template and adjust its provider-specific fields.
kubectl apply -f deploy/helm/hashi-enterprise/examples/secretstore-aws-secrets-manager.example.yaml
# kubectl apply -f deploy/helm/hashi-enterprise/examples/secretstore-gcp-secret-manager.example.yaml
# kubectl apply -f deploy/helm/hashi-enterprise/examples/secretstore-azure-key-vault.example.yaml
# kubectl apply -f deploy/helm/hashi-enterprise/examples/secretstore-vault.example.yaml

kubectl apply -f deploy/helm/hashi-enterprise/examples/audit-export-secret.external-secrets.yaml
```

The ExternalSecret example is not installed by the chart because it depends on
cluster-specific CRDs and secret-store configuration.
