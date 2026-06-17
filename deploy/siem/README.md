# HASHI Enterprise SIEM Starter Pack

This directory contains starter assets for connecting HASHI Enterprise audit
export to security monitoring systems. The assets are intentionally generic:
operators should review index names, thresholds, retention, and routing before
using them in production.

## Contents

| Path | Purpose |
|------|---------|
| `hashi-audit-field-mapping.json` | Canonical field inventory for SIEM/ECS-style audit events |
| `splunk/savedsearches.conf` | Splunk alert searches for policy denies, egress, chain gaps, and exporter freshness |
| `splunk/dashboard.xml` | Compact Splunk dashboard for audit volume, outcomes, and high-risk actions |
| `elastic/hashi-audit-index-template.json` | Elasticsearch index template for `hashi-audit-*` indices |
| `elastic/kibana-detection-rules.ndjson` | Starter Kibana detection rule exports |
| `otel/otel-collector-routing.example.yaml` | OpenTelemetry Collector routing example for HASHI audit logs |

## Assumptions

- HASHI audit export uses one of the implemented formats documented in
  `docs/HASHI_ENTERPRISE_AUDIT_EXPORT_RUNBOOK.md`.
- SIEM/ECS and Elastic `_bulk` payloads include HASHI audit fields under
  `event`, `organization`, `user`, `trace`, `labels`, and `hashi.audit`.
- Splunk HEC payloads include a HASHI event envelope and duplicated fields under
  `fields` for common search predicates.
- Alert thresholds are safe starting points, not production baselines.

## Operator Checklist

- [ ] Confirm the ingest index/source/sourcetype names match your deployment.
- [ ] Confirm audit exporter checkpoint and SIEM ingest lag are monitored.
- [ ] Confirm deny/approval alerts route to the security operations channel.
- [ ] Confirm chain-gap alerts are tested with a staging ledger.
- [ ] Confirm sensitive connector payloads remain redacted after ingestion.

