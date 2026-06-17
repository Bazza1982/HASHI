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

This is not a full HA release. Helm packaging, autoscaling, multi-replica
coordination, managed ingress, network policy, and external database wiring are
future hardening steps.

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
