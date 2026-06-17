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
- optional live audit export CronJob that runs `hashi enterprise
  audit-export-live` with a persistent checkpoint under `/data/state`.

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

If your cluster uses External Secrets Operator, adapt and apply:

```bash
kubectl apply -f deploy/helm/hashi-enterprise/examples/audit-export-secret.external-secrets.yaml
```

The ExternalSecret example is not installed by the chart because it depends on
cluster-specific CRDs and secret-store configuration.
