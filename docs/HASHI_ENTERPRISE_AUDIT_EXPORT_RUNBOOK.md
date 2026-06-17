# HASHI Enterprise Audit Export Runbook

**Status:** operator runbook for the current live audit export implementation.

This runbook explains how to connect HASHI Enterprise's append-only audit ledger to external SIEM, log, or OpenTelemetry collectors using the implemented `hashi enterprise audit-export-live` runner.

---

## 1. Current Capability

HASHI can push new audit ledger events from a chain-index checkpoint to an HTTP endpoint.

Supported export formats:

| Format | Payload | Content type | Intended receiver |
| --- | --- | --- | --- |
| `siem` | ECS-style NDJSON | `application/x-ndjson` | SIEM/log collector that accepts newline-delimited JSON |
| `ledger` | raw ledger NDJSON | `application/x-ndjson` | HASHI-compatible archive or custom collector |
| `otel` | OTLP JSON logs body | `application/json` | OpenTelemetry Collector HTTP logs endpoint |
| `splunk-hec` | newline-delimited Splunk HEC event envelopes | `application/json` | Splunk HEC event endpoint or compatible collector |
| `elastic-bulk` | Elasticsearch `_bulk` create action/document lines | `application/x-ndjson` | index-scoped Elasticsearch `_bulk` endpoint |

The exporter advances its checkpoint only after a successful 2xx response. Failed attempts do not skip undelivered events.

---

## 2. Deployment Paths

| Runtime | Asset | Usage |
| --- | --- | --- |
| CLI | `hashi enterprise audit-export-live` | Manual one-shot export, bounded maintenance loop, or supervised daemon |
| Docker Compose | `audit-export` profile | Run from cron or systemd on the host |
| Raw Kubernetes | `deploy/kubernetes/enterprise/audit-export-cronjob.yaml` | Baseline CronJob |
| Raw Kubernetes daemon | `deploy/kubernetes/enterprise/audit-export-daemon.deployment.yaml` | Long-running Deployment alternative |
| Helm | `auditExport.enabled=true` | Chart-managed CronJob |
| Helm daemon | `auditExport.daemon.enabled=true` | Chart-managed long-running Deployment alternative |
| systemd | `packaging/systemd/hashi-enterprise-audit-export.service` | Host process supervisor |

Checkpoint path:

```text
/data/state/audit_live_export_checkpoint.json
```

Keep this checkpoint on persistent storage with the enterprise SQLite state.

---

## 3. Required Secrets And Settings

At minimum, provide:

| Setting | Purpose |
| --- | --- |
| `HASHI_AUDIT_EXPORT_ENDPOINT` | Collector HTTP endpoint |
| `HASHI_AUDIT_EXPORT_FORMAT` | `siem`, `ledger`, or `otel` |
| `HASHI_AUDIT_EXPORT_HEADER` | Authorization or routing header in `Name: value` format |
| `HASHI_AUDIT_EXPORT_BATCH_SIZE` | Events per cycle |
| `HASHI_AUDIT_EXPORT_TIMEOUT` | Per-request timeout seconds |
| `HASHI_AUDIT_EXPORT_MAX_ATTEMPTS` | Retry attempts before failure |
| `HASHI_AUDIT_EXPORT_BACKOFF` | Backoff seconds between attempts |

Use `deploy/audit-export-presets.env.example` as a starting point. Real endpoints and tokens belong in your deployment secret manager.

---

## 4. Vendor Presets

### Generic NDJSON Collector

Use this when the receiver accepts newline-delimited JSON over HTTP POST:

```env
HASHI_AUDIT_EXPORT_ENDPOINT=https://collector.example.com/hashi/audit
HASHI_AUDIT_EXPORT_FORMAT=siem
HASHI_AUDIT_EXPORT_HEADER=Authorization: Bearer replace-me
```

### Splunk

Use a Splunk HEC event endpoint or a compatible collector that accepts newline-delimited HEC event envelopes:

```env
HASHI_AUDIT_EXPORT_ENDPOINT=https://splunk.example.com:8088/services/collector/event
HASHI_AUDIT_EXPORT_FORMAT=splunk-hec
HASHI_AUDIT_EXPORT_HEADER=Authorization: Splunk replace-me
```

HASHI wraps each audit event in a HEC-style envelope with `time`, `host`, `source`, `sourcetype`, `event`, and `fields`. Validate whether your Splunk deployment accepts newline-delimited HEC event envelopes; otherwise place a collector in front of Splunk to split and forward each envelope.

### Elasticsearch `_bulk`

Use an index-scoped `_bulk` endpoint:

```env
HASHI_AUDIT_EXPORT_ENDPOINT=https://elastic.example.com/hashi-audit/_bulk
HASHI_AUDIT_EXPORT_FORMAT=elastic-bulk
HASHI_AUDIT_EXPORT_HEADER=Authorization: ApiKey replace-me
```

HASHI emits alternating `_bulk` create action lines and ECS-style document lines. Use an endpoint that already scopes the target index, such as `/hashi-audit/_bulk`, because the MVP action metadata does not set `_index`.

### Elastic Agent / Logstash HTTP Input

If you prefer a generic collector instead of `_bulk`, use the `siem` format:

```env
HASHI_AUDIT_EXPORT_ENDPOINT=https://logstash.example.com/hashi-audit
HASHI_AUDIT_EXPORT_FORMAT=siem
HASHI_AUDIT_EXPORT_HEADER=Authorization: ApiKey replace-me
```

### OpenTelemetry Collector

Use the OTLP HTTP logs endpoint:

```env
HASHI_AUDIT_EXPORT_ENDPOINT=https://otel-collector.example.com/v1/logs
HASHI_AUDIT_EXPORT_FORMAT=otel
HASHI_AUDIT_EXPORT_HEADER=Authorization: Bearer replace-me
```

The exporter sends OTLP JSON logs, not protobuf.

---

## 5. Compose Operation

After adding the preset values to `deploy/enterprise.env`:

```bash
docker compose -f deploy/docker-compose.enterprise.yml --profile audit-export run --rm audit-export-live
```

For periodic operation, call that command from cron or systemd. The service exits after one export cycle.

---

## 6. Daemon Operation

Run continuously under a process supervisor:

```bash
python hashi.py enterprise audit-export-live \
  --endpoint "$HASHI_AUDIT_EXPORT_ENDPOINT" \
  --format "$HASHI_AUDIT_EXPORT_FORMAT" \
  --header "$HASHI_AUDIT_EXPORT_HEADER" \
  --checkpoint /data/state/audit_live_export_checkpoint.json \
  --daemon \
  --interval 60
```

For maintenance windows or smoke tests, bound the loop:

```bash
python hashi.py enterprise audit-export-live \
  --endpoint "$HASHI_AUDIT_EXPORT_ENDPOINT" \
  --format "$HASHI_AUDIT_EXPORT_FORMAT" \
  --header "$HASHI_AUDIT_EXPORT_HEADER" \
  --daemon \
  --interval 10 \
  --max-cycles 3
```

Daemon mode still advances the checkpoint only after a successful export cycle. Use systemd, supervisord, Kubernetes Deployment, or another supervisor for restart policy.

systemd template:

```bash
cp packaging/systemd/hashi-enterprise-audit-export.service /etc/systemd/system/
# Replace %HASHI_ROOT% and %PYTHON% with deployment-specific paths.
systemctl daemon-reload
systemctl enable --now hashi-enterprise-audit-export.service
```

Raw Kubernetes daemon:

```bash
kubectl apply -f deploy/kubernetes/enterprise/audit-export-daemon.deployment.yaml
```

Helm daemon:

```bash
helm upgrade --install hashi-enterprise deploy/helm/hashi-enterprise \
  --namespace hashi-enterprise --create-namespace \
  --set auditExport.daemon.enabled=true \
  --set auditExport.endpointSecretRef.name=hashi-audit-export \
  --set auditExport.headerSecretRef.name=hashi-audit-export
```

Use one export mode per HASHI instance: one-shot scheduler, CronJob, or daemon. Running multiple exporters against the same ledger/checkpoint can duplicate delivery attempts.

---

## 7. Kubernetes Operation

Raw manifests:

```bash
kubectl apply -k deploy/kubernetes/enterprise
```

Before enabling production export:

- replace `HASHI_AUDIT_EXPORT_ENDPOINT` and `HASHI_AUDIT_EXPORT_HEADER` through your secret manager;
- confirm the PVC used by HASHI is mounted by the CronJob;
- confirm the CronJob cannot overlap by keeping `concurrencyPolicy: Forbid`;
- verify the collector returns 2xx only after accepting the payload.

Helm:

```bash
kubectl apply -f deploy/helm/hashi-enterprise/examples/audit-export-secret.kubernetes.yaml

helm upgrade --install hashi-enterprise deploy/helm/hashi-enterprise \
  --namespace hashi-enterprise --create-namespace \
  --set auditExport.enabled=true \
  --set auditExport.format=otel \
  --set auditExport.endpointSecretRef.name=hashi-audit-export \
  --set auditExport.headerSecretRef.name=hashi-audit-export
```

For production, avoid putting long-lived tokens in shell history or Helm release values. The chart supports `auditExport.endpointSecretRef` and `auditExport.headerSecretRef` so the CronJob can read endpoint/header values from a Kubernetes Secret.

External Secrets Operator example:

```bash
# Choose one SecretStore template and adapt it to your cloud identity model.
kubectl apply -f deploy/helm/hashi-enterprise/examples/secretstore-aws-secrets-manager.example.yaml
# kubectl apply -f deploy/helm/hashi-enterprise/examples/secretstore-gcp-secret-manager.example.yaml
# kubectl apply -f deploy/helm/hashi-enterprise/examples/secretstore-azure-key-vault.example.yaml
# kubectl apply -f deploy/helm/hashi-enterprise/examples/secretstore-vault.example.yaml

kubectl apply -f deploy/helm/hashi-enterprise/examples/audit-export-secret.external-secrets.yaml
```

The ExternalSecret manifest is intentionally an example, not part of the chart, because each enterprise cluster has its own `SecretStore` or `ClusterSecretStore` and cloud-provider mapping.

Available `ClusterSecretStore` templates:

| Provider | Example |
| --- | --- |
| AWS Secrets Manager | `deploy/helm/hashi-enterprise/examples/secretstore-aws-secrets-manager.example.yaml` |
| GCP Secret Manager | `deploy/helm/hashi-enterprise/examples/secretstore-gcp-secret-manager.example.yaml` |
| Azure Key Vault | `deploy/helm/hashi-enterprise/examples/secretstore-azure-key-vault.example.yaml` |
| HashiCorp Vault | `deploy/helm/hashi-enterprise/examples/secretstore-vault.example.yaml` |

---

## 8. Acceptance Checks

- [ ] Export endpoint is reachable from the HASHI runtime.
- [ ] A first run sends at least one event or reports `attempted=0` without error.
- [ ] Checkpoint file is created under `/data/state`.
- [ ] A second run does not resend already checkpointed events.
- [ ] Daemon mode can run with `--max-cycles` in staging and exits cleanly.
- [ ] Only one exporter mode is enabled for each HASHI instance.
- [ ] Collector receives the expected format (`siem`, `ledger`, `otel`, `splunk-hec`, or `elastic-bulk`).
- [ ] Collector rejects bad credentials and HASHI does not advance the checkpoint.
- [ ] No raw connector secrets, SAML assertions, OIDC tokens, or SCIM tokens appear in exported events.
- [ ] Helm deployments use `secretKeyRef` for endpoint/header values instead of storing tokens in chart values.
- [ ] If using External Secrets Operator, `hashi-audit-export` is reconciled before enabling the CronJob.
- [ ] If using a cloud/Vault SecretStore example, provider identity and least-privilege secret access have been reviewed by the platform/security team.
- [ ] If using the starter SIEM assets, disabled alerts have been reviewed, thresholds have been tuned, and import behavior has been tested in a staging tenant.

---

## 9. SIEM Starter Pack

Starter assets live under `deploy/siem/`.

| Asset | Purpose |
|-------|---------|
| `hashi-audit-field-mapping.json` | Canonical field inventory for ECS/SIEM, Splunk HEC, and OTLP-style audit records |
| `splunk/savedsearches.conf` | Disabled starter alerts for policy denies, high-risk egress approvals, chain gaps, and stale exporter ingestion |
| `splunk/dashboard.xml` | Compact operational dashboard for audit volume, outcomes, and denied actions |
| `elastic/hashi-audit-index-template.json` | Starter Elasticsearch index template for `hashi-audit-*` |
| `elastic/kibana-detection-rules.ndjson` | Disabled starter Kibana detection rule exports |
| `otel/otel-collector-routing.example.yaml` | OpenTelemetry Collector routing example for HASHI audit logs |

Treat these files as implementation accelerators, not compliance-certified content. Production operators should tune thresholds, index names, routing destinations, notification actions, and retention according to their security operations baseline.

---

## 10. Current Deferred Work

- Deeper vendor transforms for multi-index Elasticsearch routing and strict Splunk deployments that require one HEC event per request.
- Import-validated provider dashboards and alert packs for managed Splunk Cloud, Elastic Cloud, and vendor-specific OpenTelemetry pipelines.
- Organization-specific SIEM thresholds, notification actions, and compliance reporting packs.
