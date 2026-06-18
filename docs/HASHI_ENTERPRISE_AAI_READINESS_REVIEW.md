# HASHI Enterprise AAI Readiness Review

**Date:** 2026-06-16

**Status:** Enterprise MVP implementation is ready for review. The broader future roadmap is not complete.

Related documents:

- [HASHI_ENTERPRISE_AAI_IMPLEMENTATION_ROADMAP.md](HASHI_ENTERPRISE_AAI_IMPLEMENTATION_ROADMAP.md)
- [HASHI_ENTERPRISE_AAI_PRD.md](HASHI_ENTERPRISE_AAI_PRD.md)
- [HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md](HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md)
- [HASHI_ENTERPRISE_PROFILE_ADR.md](HASHI_ENTERPRISE_PROFILE_ADR.md)
- [HASHI_ENTERPRISE_AUDIT_EXPORT_RUNBOOK.md](HASHI_ENTERPRISE_AUDIT_EXPORT_RUNBOOK.md)
- [HASHI_ENTERPRISE_SSO_SCIM_DEPLOYMENT_RUNBOOK.md](HASHI_ENTERPRISE_SSO_SCIM_DEPLOYMENT_RUNBOOK.md)
- [HASHI_ENTERPRISE_POSTGRES_LEASE_REHEARSAL.md](HASHI_ENTERPRISE_POSTGRES_LEASE_REHEARSAL.md)
- [HASHI_ENTERPRISE_K8S_HA_REHEARSAL.md](HASHI_ENTERPRISE_K8S_HA_REHEARSAL.md)

---

## 1. Readiness Decision

HASHI Enterprise AAI has reached an **MVP review-ready** state for the governed AAI control plane:

- one codebase with `personal`, `team`, and `enterprise` profiles;
- enterprise identity, sessions, roles, projects, memberships, service accounts, and API tokens;
- OIDC provider metadata skeleton for enterprise SSO readiness, with secret redaction and fail-closed readiness checks;
- OIDC authorization-code start, callback state/code validation, token exchange request preparation, token endpoint exchange service, JWKS fetch/cache service, RS256/JWKS ID token signature verification, ID token claim validation, claim mapping, and verified identity session completion;
- default-disabled governed channels and channel gates;
- central policy decisions for commands, channels, backends, tools, execution, and connectors;
- unified audit ledger and adapters for existing HASHI audit streams;
- task, artifact, evidence bundle, verification, and escalation primitives;
- Workbench admin surfaces for users, channels, policies, audit, approvals, health, and connectors;
- Docker/Kubernetes ops skeleton with backup, restore, migration, and health checks;
- P10 connector MVP with GitHub, Slack, and Google Chat, scoped credentials, secret refs, policy gates, health, dry-run, audit, and admin UI.

This is **not** the end state of the enterprise product. It is the first reviewable implementation slice.

---

## 2. What Is Ready

### Governance Core

- Deployment profiles preserve current `personal` mode while enabling governed `team` and `enterprise` paths.
- Enterprise bootstrap requires explicit organization initialization.
- Identity and role primitives distinguish `individual_user` from personal owner/admin mode.
- Admin APIs and Workbench surfaces use governed session/admin checks.
- Scoped API tokens can be created, listed as metadata without secret material, and revoked through admin-gated APIs with audit events.
- Workbench can discover configured local/OIDC/SAML login providers without exposing client secrets or SAML metadata XML.
- OIDC start returns an authorization URL while keeping the PKCE `code_verifier` server-side; callback validation consumes state and prepares a token exchange request without writing authorization codes, PKCE verifiers, or client secrets into browser responses or audit.
- OIDC ID token verification enforces compact JWT shape, `alg=RS256`, matching `kid`, RSA JWKS signing keys, signature validity, and issuer/audience/expiry/not-before/issued-at/subject/nonce claims.
- OIDC verified identities can create or reuse active enterprise users, issue sessions, and assign only `individual_user` default project membership unless an administrator changes policy.
- OIDC token endpoint and JWKS network calls are isolated behind injectable services; public token response payloads expose token presence metadata only, not token values.
- OIDC callback supports an explicitly enabled full login path from authorization code to session, while preserving default prepared mode for deployments that have not enabled live SSO completion.
- SAML IdP metadata can be parsed safely, and preverified SAML assertion claims can be checked for issuer, audience, time window, subject, email, and display name.
- SAML HTTP login baseline can create AuthnRequest start payloads, track RelayState, validate callback state/provider, require a verifier hook or explicitly enabled preverified assertion handoff, upsert enterprise users, assign default project membership, and issue sessions.
- SAML XML Signature verification is wired to IdP metadata signing certificates through `xmlsec1`; Workbench SAML callback defaults to this verifier path and fails closed when assertions are unsigned, verification fails, or no verifier is available.
- SCIM-style provisioning primitives, admin-gated HTTP handlers, and IdP-facing SCIM 2.0 Users routes can create, update, list, fetch, deactivate, and reactivate users, assign default project membership, revoke sessions/API tokens on deactivation, and require scoped SCIM API tokens for `/scim/v2/Users`.
- Read-only SCIM 2.0 Groups routes can expose HASHI projects as groups with active project members through admin-gated and IdP-facing `/scim/v2/Groups` surfaces protected by scoped `scim:read` service tokens.
- SCIM 2.0 discovery routes expose ServiceProviderConfig, ResourceTypes, and Schemas metadata for Users and Groups through admin-gated and IdP-facing surfaces.
- SCIM 2.0 Bulk routes support bounded Users create/get/patch batches with `failOnErrors`, org boundary checks, scoped `scim:write` token gates, and write-path audit events.
- The SSO/SCIM deployment runbook documents `xmlsec1` prerequisites, SAML fail-closed readiness checks, SCIM service token scopes, endpoint coverage, Bulk limits, acceptance checks, and rollback steps for current deployments.

### Control Plane

- Channels are modeled as administratively controlled capabilities.
- Commands, channels, tools, execution scopes, backends, and connectors can be routed through central policy decisions.
- Approval-required flows create approval records instead of silently executing high-risk actions.
- Data governance primitives can classify baseline sensitive content and produce egress decisions from approval thresholds and destination-region allowlists.
- Slack and Google Chat webhook `message.send` actions are checked before connector execution; confidential outbound text requires approval, restricted outbound text is denied, and connector audit records redact message text.

### Auditability

- Unified ledger records structured events for identity, admin actions, channels, policy decisions, commands, connectors, tasks, artifacts, and adapted legacy streams.
- New ledger events include tamper-evident hash-chain fields and can be verified for in-database modification, deletion, or reordering.
- Audit anchors can export a chain-range manifest with start/end hash, count, and anchor hash for later storage in WORM-capable systems.
- Filesystem audit anchor sink can write hash-named read-only anchor objects with receipts and verification, providing a local WORM-style adapter for early deployments.
- Object-store audit anchor sink can write hash-named anchor objects through an SDK-neutral client protocol with no-overwrite semantics, idempotent conflict handling, receipt verification, and object-lock metadata forwarding.
- Audit export and Workbench timeline views exist for review and handoff.
- Audit export supports default ledger NDJSON plus SIEM/ECS-style and OpenTelemetry log-style NDJSON mappings.
- Live audit export service primitive can push ledger/SIEM NDJSON or OTLP JSON log payloads from a hash-chain checkpoint through an injectable enterprise transport, persist file-backed checkpoints, and retry transient failures without advancing the checkpoint before delivery succeeds.
- Live audit exporters create a checkpoint-adjacent singleton lock by default and fail closed when another exporter already holds it.
- Enterprise store includes a TTL-based database lease primitive for future leader/worker coordination.
- Live audit exporters can optionally acquire and renew an enterprise DB lease during one-shot or daemon runs.
- Raw Kubernetes and Helm audit export daemon assets can pass DB lease arguments using the pod name as holder identity.
- Superloop scheduler ticks can be guarded by enterprise DB leases when the scheduler is run in multi-replica contexts.
- Scheduler DB leases can be enabled through global config or deployment environment variables, with raw Kubernetes and Helm assets wiring the pod name as holder identity.
- Scheduler lease enforcement now guards the whole trigger tick, so secondary replicas skip heartbeat, nudge, cron, parked follow-up, and superloop trigger work while another holder owns the lease.
- Enterprise lease construction supports SQLite paths/URLs and PostgreSQL URLs through optional `psycopg`, with PostgreSQL advisory transaction locks covered by fake-driver tests.
- Operators can run `hashi enterprise lease-rehearse` against a target lease database to validate exactly-one acquisition, renew, release, and takeover behavior before enabling multi-replica scheduler leases.
- An optional PostgreSQL integration test and runbook are available for staging DSN rehearsal without making ordinary CI depend on PostgreSQL.
- A GitHub Actions PostgreSQL lease workflow now runs that integration test against a `postgres:16` service container for lease-related changes.
- PostgreSQL scheduler leases can use optional `psycopg_pool` with bounded min/max settings, and scheduler shutdown closes the lease store best-effort.
- A CI-friendly PostgreSQL lease pool load harness exercises pooled acquire/block/renew/release/takeover behavior across multiple concurrent lease rehearsals without requiring a live database.
- Operators can run `hashi enterprise lease-load-rehearse` against a staging lease database for bounded multi-lease load rehearsal with configurable lease and worker counts.
- Raw Kubernetes and Helm assets can run the bounded lease load rehearsal as an in-cluster one-shot Job before scaling scheduler replicas.
- A GitHub Actions Helm render workflow lints the enterprise chart and renders the lease load rehearsal Job before chart changes merge.
- A Kubernetes HA rehearsal plan generator can emit the Helm/kubectl command sequence for operator review before staging execution.
- A GitHub Actions workflow publishes the Kubernetes HA rehearsal command plan as an artifact without running Helm or kubectl.
- A Kubernetes HA rehearsal runbook and Helm values example combine replica count, external DB secret wiring, scheduler lease pool settings, PodDisruptionBudget, and audit-export singleton lease controls for staging rollout practice.
- Optional Kubernetes Lease RBAC assets are present for future native leader-election runtime support.
- Runtime primitives now include an injectable Kubernetes Lease coordinator with fake-client tests for acquire, renew, release, expiry takeover, and resourceVersion conflict handling.
- Runtime primitives now include an optional Kubernetes API adapter for `coordination.k8s.io/v1` Lease objects, with fake API tests for body mapping, `resourceVersion`, 404, and 409 behavior.
- Scheduler lease config can now select either the existing database lease backend or the Kubernetes Lease backend while keeping ordinary deployments on the database default.
- Packaging now exposes `hashi-bridge[kubernetes]` and an enterprise Docker build arg for images that need the Kubernetes Lease backend.
- `hashi enterprise k8s-lease-rehearse` can smoke-test the Kubernetes Lease backend using the same exactly-one acquisition semantics as the database lease rehearsal.
- `tools/enterprise_k8s_backend_doctor.py` checks the optional dependency, enterprise Docker build knob, and current environment importability before operators promote Kubernetes Lease backend images.
- `tools/enterprise_k8s_image_smoke_plan.py` emits a JSON command plan for Docker build, image import check, CLI help check, and optional cluster smoke without requiring Docker in ordinary tests.
- The `enterprise-k8s-image-smoke-plan` GitHub Actions workflow publishes that JSON command plan as an artifact without running Docker by default.
- `hashi enterprise audit-export-live` provides a one-shot operator runner for HTTP SIEM/ledger/OTLP pushes with checkpoint, retry, timeout, batch-size, and custom header controls, so deployments can schedule live export through cron, systemd, or Kubernetes CronJob without embedding vendor SDKs.
- `hashi enterprise audit-export-live --daemon` can run bounded or continuous export loops with configurable interval while preserving checkpoint safety.
- Deployment assets now include a Docker Compose `audit-export` profile, a raw Kubernetes CronJob, and a Helm-gated CronJob template for scheduled live audit export with persistent checkpoints.
- Supervisor assets include a systemd service template, raw Kubernetes daemon Deployment example, and Helm `auditExport.daemon.enabled` Deployment template.
- The live audit export runbook and preset env example cover generic NDJSON, Splunk HEC event envelopes, Elasticsearch `_bulk`, Elastic/Logstash HTTP input, and OpenTelemetry Collector HTTP logs deployment paths.
- The Helm audit export CronJob can read endpoint and authorization header values through Kubernetes `secretKeyRef`, avoiding long-lived tokens in chart values.
- Helm examples include a plain Kubernetes Secret and an External Secrets Operator `ExternalSecret` for audit export endpoint/header delivery.
- External Secrets Operator examples include AWS Secrets Manager, GCP Secret Manager, Azure Key Vault, and HashiCorp Vault `ClusterSecretStore` templates.
- Starter SIEM assets under `deploy/siem/` provide a canonical field mapping, Splunk saved searches/dashboard, Elasticsearch index template, Kibana starter rules, and an OpenTelemetry Collector routing example.
- Sensitive connector parameters are redacted in connector audit records.

### Work And Evidence

- Tasks, artifacts, evidence bundles, verification checks, and escalation support enterprise-style review of deliverables.
- File-producing work can be checked against expected deliverables before being marked complete.

### Enterprise Connectors

- Connector interface, registry, credential store, secret resolver, execution gate, execution service, health API, and factory exist.
- Secret resolution supports provider plugins, default env/HASHI secret refs, root-confined file mounted secrets, and Kubernetes-style mounted secret refs.
- Vault secret resolution supports token-auth read paths with injectable clients and KV v1/v2 field extraction.
- GitHub connector supports repository metadata and issue creation with dry-run behavior.
- Slack incoming webhook connector supports governed `message.send` with dry-run behavior.
- Google Chat incoming webhook connector supports governed `message.send` with dry-run behavior.
- Default connector policy allows GitHub reads, requires approval for GitHub writes, and requires approval for Slack and Google Chat outbound messages.
- Workbench connector execution API rejects webhook `message.send` actions without non-empty `text` before execution.
- Workbench Enterprise console supports connector credentials, health, policy defaults, and dry-run/test-run execution.

### Deployment And Operations

- Docker Compose enterprise profile mirrors the production process model with `/api/health` health checks.
- Kubernetes baseline manifests exist for namespace, config map, example secret, PVC, single-replica deployment, service, and `/api/health` liveness/readiness probes.
- Kubernetes baseline manifests include a scheduled live audit export CronJob that runs the one-shot exporter with `concurrencyPolicy: Forbid` and stores checkpoints under `/data/state`.
- Kubernetes baseline mounts `/data` for state/workspaces/logs/backups and mounts connector secrets as read-only files for provider-based secret resolution.
- Helm baseline chart packages the same enterprise deployment contract with configurable image, service, resources, probes, persistence, connector secret mount, optional ingress, optional NetworkPolicy, optional HPA skeleton, optional PodDisruptionBudget, optional external database SecretRef wiring, and optional live audit export CronJob.
- The SSO/SCIM deployment runbook gives operators a concrete path for wiring current SAML XML Signature verification and SCIM 2.0 compatibility surfaces into an enterprise IdP.
- The Kubernetes and Helm baselines are deployment starting points, not a full HA release.

---

## 3. Verification Evidence

Recent targeted checks passed:

```text
python3 -m py_compile tests/test_workbench_enterprise_connectors.py

pytest -q tests/test_workbench_enterprise_connectors.py \
  tests/test_enterprise_connectors.py \
  tests/test_enterprise_policy.py

50 passed
```

Recent Workbench build checks passed:

```text
cd workbench && npm run build
```

The connector readiness tests cover:

- Slack credential creation through Workbench API;
- registry refresh from a Slack secret reference;
- Slack dry-run execution through the Workbench connector execution API;
- policy allow path for Slack dry-run;
- default approval-required gate for Slack outbound messages;
- Google Chat credential creation, registry refresh, dry-run execution, and default approval-required gate;
- server-side rejection of webhook `message.send` without `text`.

---

## 4. Explicit Deferred Work

These are not blockers for Enterprise MVP review, but they are not complete:

- IdP-specific SAML/SCIM setup guides for Okta, Entra ID, OneLogin, and Ping beyond the generic SSO/SCIM deployment runbook, plus SCIM 2.0 compatibility beyond baseline Users/read-only Groups/discovery/Bulk surfaces, including group mutation, advanced filters, schema extension negotiation, and non-User bulk operations;
- full ABAC simulator and policy preview tooling;
- cloud-specific object-store WORM client packages and deployment runbooks for S3/GCS/Azure immutable storage;
- Vault AppRole/Kubernetes auth, lease renewal, and policy bootstrap;
- live SIEM/OTLP exporter hardening beyond the CLI runner, daemon loop, baseline Compose/Kubernetes/Helm scheduling, supervised daemon manifests, generic vendor preset runbook, Kubernetes `secretKeyRef` wiring, External Secrets examples, and starter SIEM assets, including deeper vendor transforms, import-validated dashboards/alerts, and production validation for each cloud identity model;
- Kubernetes HA deployment beyond the baseline manifests/chart, managed database URL wiring, optional PodDisruptionBudget assets, file-lock guarded audit export, DB lease primitive, audit-export DB lease wiring, superloop lease guard, scheduler lease environment wiring, whole-tick scheduler lease enforcement, optional PostgreSQL lease backend, operator lease rehearsal CLI, staging PostgreSQL rehearsal runbook, PostgreSQL lease CI, optional lease pool wiring, Kubernetes HA rehearsal assets, Kubernetes Lease RBAC assets, Kubernetes Lease coordinator abstraction, Kubernetes Lease API adapter, scheduler backend selection wiring, Kubernetes Lease packaging guard, Kubernetes Lease smoke rehearsal CLI, Kubernetes Lease packaging doctor, Kubernetes Lease image smoke plan, Kubernetes Lease image smoke plan artifact workflow, CI-friendly pool load harness, lease load rehearsal CLI, in-cluster lease load rehearsal assets, Helm render CI, HA rehearsal command plan, and HA rehearsal plan artifact workflow, including real staging pool sizing, validated production ingress/network policies, autoscaling runbooks, and live multi-replica rehearsal;
- Slack OAuth/Bot API, channel discovery, and user mapping;
- Microsoft Teams and Feishu connectors;
- Google Chat OAuth, space discovery, and user mapping;
- GitHub PR create/merge actions;
- full DLP/data residency enforcement across every runtime, non-webhook connector, channel, artifact export, and backend path;
- browser-level UI screenshot regression tests for the Workbench Enterprise console.

---

## 5. Review Recommendation

The implementation is ready for a structured review against the Enterprise MVP cut line.

Recommended review order:

1. Run personal profile regression smoke to confirm no single-user regression.
2. Run enterprise identity/channel/policy/audit tests.
3. Run task/artifact/evidence/verification tests.
4. Run connector tests and Workbench build.
5. Manually inspect Workbench Enterprise console.
6. Decide whether to tag this as Enterprise AAI alpha or continue to the next hardening sprint.

---

## 6. Completion Boundary

For nudge/task tracking, the correct completion boundary is:

- **Enterprise MVP review-ready:** yes, once final review passes.
- **Whole future-facing Enterprise AAI roadmap complete:** no.

The roadmap intentionally keeps future enterprise capabilities deferred. The completion marker should only be emitted if the active task is explicitly scoped to the MVP review-ready cut line, or if all deferred enterprise roadmap items are also implemented.
