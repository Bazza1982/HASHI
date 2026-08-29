# HASHI AAI Enterprise v0.1.0-alpha.1 - Release Notes

Release focus: **enterprise-grade AAI deployment alpha**.

Workbench has retired. This release line now exposes enterprise controls only
through the authenticated Backend API and configured channels.

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
  export, evidence primitives, connectors, and Backend admin APIs are
  available for alpha review.
- Deployment artifacts exist for Docker Compose, raw Kubernetes, Helm, systemd
  audit export daemon mode, production hardening command plans, HA rehearsal
  plans, PostgreSQL and Kubernetes lease rehearsal, external secret examples,
  and SIEM starter packs.

## What Is Ready For Alpha Testing

- Enterprise bootstrap/control-plane primitives.
- OIDC and SAML/SCIM readiness surfaces with fail-closed verifier behavior.
- GitHub, Slack, Google Chat, Teams, and Feishu connector MVPs with health,
  dry-run, credential validation, action schemas, Backend API controls, policy
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

The release candidate cut used the following local validation snapshot. The
original snapshot also counted static source/YAML mirror tests; those modules
were later retired because they did not execute the represented artifacts.
Current artifact revalidation uses native tooling:

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

helm lint deploy/helm/hashi-enterprise
pytest -q tests/contract/test_enterprise_plan_contract.py
pytest -q tests/test_enterprise_siem_assets.py tests/test_hashi_enterprise_cli.py

python3 hashi.py --help
python3 hashi.py enterprise --help
git diff --check
```

The annotated alpha tag is:

```text
v0.1.0-alpha.1
```

It points at the final alpha evidence and documentation consistency commit for
the Enterprise AAI `0.1 Alpha` artifact freeze.

## Alpha Acceptance Checklist

Before publishing or re-cutting this release:

```text
python3 -m py_compile hashi.py setup.py
python3 -m pytest -q tests/test_enterprise_connectors.py \
  tests/test_workbench_enterprise_connectors.py \
  tests/test_enterprise_policy.py
git diff --check
```
