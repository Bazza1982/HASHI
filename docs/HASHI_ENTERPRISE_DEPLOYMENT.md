# HASHI Enterprise Deployment Skeleton

This is the first deployable skeleton for the Enterprise AAI profile. It is intentionally conservative: governance state is mounted into named volumes, channels are not enabled by default, and production secrets should come from the deployment platform rather than committed files.

## Files

- `Dockerfile.enterprise` builds the Python runtime image.
- `deploy/docker-compose.enterprise.yml` runs the enterprise Workbench/API service.
- `deploy/enterprise.env.example` documents the minimum environment variables.

## Local Compose Trial

```bash
cp deploy/enterprise.env.example deploy/enterprise.env
docker compose -f deploy/docker-compose.enterprise.yml up --build
```

Then check:

```bash
curl http://127.0.0.1:18800/api/health
```

## Volumes

- `hashi_enterprise_state`: SQLite state, audit ledger, sessions.
- `hashi_enterprise_workspaces`: governed project workspaces.
- `hashi_enterprise_logs`: runtime logs.
- `hashi_enterprise_backups`: backup archives.

## Current Limitations

- This skeleton does not yet perform first-run admin bootstrap.
- It does not yet wire secrets from Vault/Kubernetes secrets.
- It does not yet include a migration entrypoint.
- It is not an HA/Kubernetes deployment.

## Operator Backup

Inside the container or on the host checkout:

```bash
python hashi.py enterprise backup --output backups/enterprise.tar.gz
python hashi.py enterprise inspect-backup backups/enterprise.tar.gz
```
