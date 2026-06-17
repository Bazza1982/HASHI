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

The exporter advances its checkpoint only after a successful 2xx response. Failed attempts do not skip undelivered events.

---

## 2. Deployment Paths

| Runtime | Asset | Usage |
| --- | --- | --- |
| CLI | `hashi enterprise audit-export-live` | Manual one-shot export or custom scheduler |
| Docker Compose | `audit-export` profile | Run from cron or systemd on the host |
| Raw Kubernetes | `deploy/kubernetes/enterprise/audit-export-cronjob.yaml` | Baseline CronJob |
| Helm | `auditExport.enabled=true` | Chart-managed CronJob |

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

Use a Splunk HEC raw endpoint or a collector/transformation layer that accepts NDJSON lines:

```env
HASHI_AUDIT_EXPORT_ENDPOINT=https://splunk.example.com:8088/services/collector/raw
HASHI_AUDIT_EXPORT_FORMAT=siem
HASHI_AUDIT_EXPORT_HEADER=Authorization: Splunk replace-me
```

Do not assume the standard HEC event endpoint will accept HASHI NDJSON directly. If your Splunk deployment requires HEC event envelopes, put a small transform in front of Splunk or use a collector that wraps each line.

### Elastic / Logstash

Use Elastic Agent, Logstash HTTP input, or another HTTP collector that accepts NDJSON:

```env
HASHI_AUDIT_EXPORT_ENDPOINT=https://logstash.example.com/hashi-audit
HASHI_AUDIT_EXPORT_FORMAT=siem
HASHI_AUDIT_EXPORT_HEADER=Authorization: ApiKey replace-me
```

This is not the Elasticsearch `_bulk` API format. Do not send the current `siem` output directly to `_bulk` unless a transform adds bulk action metadata lines.

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

## 6. Kubernetes Operation

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
helm upgrade --install hashi-enterprise deploy/helm/hashi-enterprise \
  --namespace hashi-enterprise --create-namespace \
  --set auditExport.enabled=true \
  --set auditExport.endpoint=https://otel-collector.example.com/v1/logs \
  --set auditExport.format=otel \
  --set-string auditExport.header='Authorization: Bearer replace-me'
```

For production, avoid putting long-lived tokens in shell history or Helm release values. Prefer external secret injection where available.

---

## 7. Acceptance Checks

- [ ] Export endpoint is reachable from the HASHI runtime.
- [ ] A first run sends at least one event or reports `attempted=0` without error.
- [ ] Checkpoint file is created under `/data/state`.
- [ ] A second run does not resend already checkpointed events.
- [ ] Collector receives the expected format (`siem`, `ledger`, or `otel`).
- [ ] Collector rejects bad credentials and HASHI does not advance the checkpoint.
- [ ] No raw connector secrets, SAML assertions, OIDC tokens, or SCIM tokens appear in exported events.

---

## 8. Current Deferred Work

- Managed long-running daemon mode.
- Vendor-specific transforms for Splunk HEC event envelopes and Elasticsearch `_bulk`.
- Secret-manager-native Helm wiring for every target platform.
- SIEM-specific dashboards, alerts, and field mappings beyond the baseline ECS-style event shape.
