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
