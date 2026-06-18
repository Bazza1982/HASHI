# HASHI AAI Enterprise 0.1 Alpha Deployment Skeleton

This is the first deployable alpha skeleton for the Enterprise AAI profile. It
is intentionally conservative: governance state is mounted into named volumes,
channels are not enabled by default, and production secrets should come from the
deployment platform rather than committed files.

`0.1 Alpha` means the deployment contract and artifacts are ready for operator
review and alpha testing. It does not mean a customer enterprise server has
already passed full production validation.

## Files

- `Dockerfile.enterprise` builds the Python runtime image.
- `deploy/docker-compose.enterprise.yml` runs the enterprise Workbench/API service.
- `deploy/enterprise.env.example` documents the minimum environment variables.
- `deploy/audit-export-presets.env.example` gives safe starting presets for generic NDJSON, Splunk, Elastic/Logstash, and OTLP collectors.
- `deploy/kubernetes/enterprise/audit-export-cronjob.yaml` schedules the live audit exporter in raw Kubernetes deployments.
- `deploy/helm/hashi-enterprise/templates/audit-export-cronjob.yaml` provides the same exporter scheduling path for Helm deployments.
- `docs/HASHI_ENTERPRISE_AUDIT_EXPORT_RUNBOOK.md` documents live audit export deployment, vendor presets, and acceptance checks.
- `docs/HASHI_ENTERPRISE_SSO_SCIM_DEPLOYMENT_RUNBOOK.md` documents SAML `xmlsec1` verification and SCIM 2.0 operator setup.
- `docs/HASHI_ENTERPRISE_PRODUCTION_HARDENING_RUNBOOK.md` documents Helm ingress TLS, NetworkPolicy, HPA, and PDB rollout checks.
- `tools/enterprise_production_validation_plan.py` generates the production hardening validation command plan.

## Local Compose Trial

Enterprise alpha startup has a hard bootstrap gate. The example deployment is
for an environment whose organization/admin bootstrap state already exists. If
`HASHI_ENTERPRISE_BOOTSTRAP_COMPLETE` is not set to `true`, startup fails closed
before serving Workbench.

For this alpha skeleton:

1. create or preload the enterprise organization/admin state;
2. copy `deploy/enterprise.env.example` to `deploy/enterprise.env`;
3. set `HASHI_ENTERPRISE_BOOTSTRAP_COMPLETE=true` only after that bootstrap
   state exists;
4. then start Compose.

```bash
cp deploy/enterprise.env.example deploy/enterprise.env
# edit deploy/enterprise.env and set HASHI_ENTERPRISE_BOOTSTRAP_COMPLETE=true
docker compose -f deploy/docker-compose.enterprise.yml up --build
```

Then check:

```bash
curl http://127.0.0.1:18800/api/health
```

Run one live audit export cycle from the Compose profile:

```bash
docker compose -f deploy/docker-compose.enterprise.yml --profile audit-export run --rm audit-export-live
```

## Volumes

- `hashi_enterprise_state`: SQLite state, audit ledger, sessions.
- `hashi_enterprise_workspaces`: governed project workspaces.
- `hashi_enterprise_logs`: runtime logs.
- `hashi_enterprise_backups`: backup archives.

## Current Limitations

- This skeleton does not yet perform first-run admin bootstrap.
- It does not yet include a migration entrypoint.
- `HASHI_ENTERPRISE_BOOTSTRAP_COMPLETE=true` is a required operator assertion
  after bootstrap; leaving it false intentionally blocks governed startup.
- It is not a production-certified HA/Kubernetes deployment.
- SSO/SCIM can be configured with the deployment runbook, but IdP-specific setup guides and HA/external-database validation are still future work.
- Live audit export scheduling and baseline vendor presets are provided for Compose, raw Kubernetes, and Helm, but vendor-specific transforms and dashboards remain future work.

## Operator Backup

Inside the container or on the host checkout:

```bash
python hashi.py enterprise backup --output backups/enterprise.tar.gz
python hashi.py enterprise inspect-backup backups/enterprise.tar.gz
```
