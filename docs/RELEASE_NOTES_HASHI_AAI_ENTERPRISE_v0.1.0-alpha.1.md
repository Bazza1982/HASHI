# HASHI AAI Enterprise v0.1.0-alpha.1 - Release Notes

Release focus: **enterprise-grade AAI deployment alpha**.

This release resets the enterprise-grade update line to `0.1.0-alpha.1`. It is
an alpha testing cut for deployments that need the full enterprise control-plane
shape available in theory, while accepting that real customer environment
validation remains pending.

## What This Alpha Claims

- Personal/local HASHI usage remains the default smooth path.
- Enterprise profile capabilities are present behind explicit configuration and
  governance gates.
- Identity, sessions, projects, roles, service tokens, SSO/SCIM primitives,
  channel governance, policy decisions, approval records, audit ledger, audit
  export, evidence primitives, connectors, and Workbench admin surfaces are
  available for alpha review.
- Deployment artifacts exist for Docker Compose, raw Kubernetes, Helm, systemd
  audit export daemon mode, production hardening command plans, HA rehearsal
  plans, PostgreSQL and Kubernetes lease rehearsal, external secret examples,
  and SIEM starter packs.

## What Is Ready For Alpha Testing

- Enterprise bootstrap/control-plane primitives.
- OIDC and SAML/SCIM readiness surfaces with fail-closed verifier behavior.
- GitHub, Slack, Google Chat, Teams, and Feishu connector MVPs with health,
  dry-run, credential validation, action schemas, Workbench controls, policy
  gates, and audit redaction.
- Unified audit ledger, tamper-evident chain verification, local/object-store
  anchor adapters, live export runner, daemon mode, checkpoint safety, and SIEM
  mapping starter assets.
- Deployment starting points for Compose, Kubernetes, Helm, systemd, audit
  export scheduling/daemon operation, secret references, and production
  validation planning.

## Known Alpha Limits

- Production enterprise-server deployment has not been fully validated in a
  customer-like environment.
- IdP-specific setup for Okta, Entra ID, OneLogin, and Ping is not yet
  separately certified.
- Slack/Teams/Google Chat/Feishu OAuth, Graph/Bot APIs, channel discovery, and
  user mapping remain post-alpha.
- Full DLP/data residency enforcement across every runtime, connector, artifact
  export, and backend path remains post-alpha.
- Production HA requires real staging rehearsal for ingress, NetworkPolicy,
  HPA/PDB, external database sizing, lease behavior, and rollback.
- SIEM dashboards and alerts are starter assets; vendor import validation is
  post-alpha.

## Final Alpha Validation Snapshot

The release candidate cut used this local/static validation snapshot:

```text
python3 -m py_compile hashi.py setup.py orchestrator/config.py \
  orchestrator/workbench_api.py

pytest -q tests/test_enterprise_connectors.py \
  tests/test_workbench_enterprise_connectors.py tests/test_enterprise_policy.py
# 91 passed

pytest -q tests/test_workbench_enterprise_policies.py \
  tests/test_workbench_enterprise_approvals.py \
  tests/test_workbench_enterprise_audit.py tests/test_enterprise_audit_ledger.py \
  tests/test_enterprise_audit_export.py tests/test_enterprise_audit_live_export.py
# 35 passed

pytest -q tests/test_enterprise_deploy_skeleton.py \
  tests/test_enterprise_helm_chart.py \
  tests/test_enterprise_production_validation_plan.py \
  tests/test_enterprise_siem_assets.py tests/test_hashi_enterprise_cli.py
# 48 passed

python3 hashi.py --help
python3 hashi.py enterprise --help
cd workbench && npm run build
git diff --check
```

The local tag candidate is:

```text
v0.1.0-alpha.1
```

No release tag is created by this document update.

## Alpha Acceptance Checklist

Before tagging this release:

```text
python3 -m py_compile hashi.py setup.py
python3 -m pytest -q tests/test_enterprise_connectors.py \
  tests/test_workbench_enterprise_connectors.py \
  tests/test_enterprise_policy.py
git diff --check
```

Workbench build should also pass when Node dependencies are installed:

```text
cd workbench && npm run build
```
