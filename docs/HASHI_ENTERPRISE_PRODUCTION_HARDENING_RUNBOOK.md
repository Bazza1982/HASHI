# HASHI Enterprise Production Hardening Runbook

This runbook is a production readiness checklist for the Helm deployment
surface. It focuses on ingress TLS, NetworkPolicy, autoscaling, disruption
budgets, and post-render checks. It does not replace cluster-specific security
review, cloud ingress validation, or live load testing.

## Prerequisites

- HA rehearsal plan artifact reviewed.
- PostgreSQL lease rehearsal and in-cluster lease load rehearsal passed.
- Production image tag selected and scanned.
- `hashi-enterprise-database` Secret created through the approved secret
  manager path.
- Ingress controller, DNS, and certificate issuer are available in the target
  cluster.
- Namespace labels for ingress NetworkPolicy selection are confirmed.

## Render Review

Start from the hardening values example and replace placeholders:

```bash
helm template hashi-enterprise deploy/helm/hashi-enterprise \
  --namespace hashi-enterprise \
  --values deploy/helm/hashi-enterprise/examples/multi-replica-rehearsal.values.yaml \
  --values deploy/helm/hashi-enterprise/examples/production-hardening.values.yaml \
  --set image.repository=ghcr.io/your-org/hashi-enterprise \
  --set image.tag=replace-me \
  > /tmp/hashi-enterprise-production-render.yaml
```

Review for the expected resources:

```bash
grep -E "kind: (Ingress|NetworkPolicy|HorizontalPodAutoscaler|PodDisruptionBudget)" \
  /tmp/hashi-enterprise-production-render.yaml
grep -E "hashi-enterprise.example.com|hashi-enterprise-tls|ingress-nginx" \
  /tmp/hashi-enterprise-production-render.yaml
```

To generate the full operator-reviewed validation command plan:

```bash
python tools/enterprise_production_validation_plan.py \
  --image-repository ghcr.io/your-org/hashi-enterprise \
  --image-tag replace-me \
  --host hashi-enterprise.example.com \
  --ingress-namespace ingress-nginx \
  --output /tmp/hashi-enterprise-production-validation-plan.json
```

## Apply

Apply only after render review and secret validation:

```bash
helm upgrade --install hashi-enterprise deploy/helm/hashi-enterprise \
  --namespace hashi-enterprise --create-namespace \
  --values deploy/helm/hashi-enterprise/examples/multi-replica-rehearsal.values.yaml \
  --values deploy/helm/hashi-enterprise/examples/production-hardening.values.yaml \
  --set image.repository=ghcr.io/your-org/hashi-enterprise \
  --set image.tag=replace-me
```

## Checks

Confirm rollout and scaling resources:

```bash
kubectl -n hashi-enterprise rollout status deploy/hashi-enterprise
kubectl -n hashi-enterprise get ingress,networkpolicy,hpa,pdb
kubectl -n hashi-enterprise describe hpa hashi-enterprise
```

Confirm HTTPS ingress and health endpoint:

```bash
curl -fsS https://hashi-enterprise.example.com/api/health
```

Confirm NetworkPolicy assumptions before rollout completion:

```bash
kubectl get namespace ingress-nginx --show-labels
kubectl -n hashi-enterprise describe networkpolicy hashi-enterprise
```

## Rollback

If ingress, network policy, or autoscaling validation fails, disable the
hardening controls first while keeping database lease protection intact:

```bash
helm upgrade hashi-enterprise deploy/helm/hashi-enterprise \
  --namespace hashi-enterprise \
  --set ingress.enabled=false \
  --set networkPolicy.enabled=false \
  --set autoscaling.enabled=false \
  --set podDisruptionBudget.enabled=false
```

## Known Limits

- The example uses nginx and cert-manager annotations; adapt it for other
  ingress controllers.
- NetworkPolicy behavior depends on the cluster CNI implementation.
- HPA requires metrics-server or an equivalent metrics API.
- This runbook does not validate vendor WAF, private load balancers, or cloud
  identity controls.
