# HASHI Enterprise AAI Implementation Roadmap

**Status:** ready-to-implement roadmap.

**Date:** 2026-06-15.

**Related docs:**

- [HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md](HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md)
- [HASHI_ENTERPRISE_AAI_PRD.md](HASHI_ENTERPRISE_AAI_PRD.md)
- [HASHI_ENTERPRISE_PROFILE_ADR.md](HASHI_ENTERPRISE_PROFILE_ADR.md)
- [HASHI_ENTERPRISE_AAI_READINESS_REVIEW.md](HASHI_ENTERPRISE_AAI_READINESS_REVIEW.md)

---

## 1. Implementation Decision

HASHI Enterprise AAI will be implemented as one codebase with profile-driven behavior:

```text
personal   = current HASHI single-owner mode
team       = shared deployment with role separation
enterprise = governed organization deployment
```

`individual_user` is an enterprise identity role, not a deployment profile.

The implementation must preserve current personal usage while adding enterprise governance primitives behind explicit profiles and feature gates.

---

## 2. Delivery Strategy

The enterprise upgrade should not be delivered as one large rewrite. It should be delivered as an incremental governance layer:

1. introduce enterprise skeleton and stable contracts;
2. add identity and channel control;
3. add policy decisions and audit records early;
4. promote logs, commands, tools, tasks, and artifacts into enterprise primitives;
5. expose controls through admin APIs and Workbench;
6. harden deployment and operations;
7. add enterprise connectors only after governance is stable.

The first engineering rule is:

```text
Audit contract early. Policy contract early. Productized ledger and UI later.
```

This prevents identity, channel, policy, and execution features from creating incompatible temporary logs or ad hoc permission checks.

---

## 3. MVP Cut Lines

### Internal Enterprise Alpha

Internal alpha is ready when HASHI can run in `enterprise` profile with basic governed access, even if UI and integrations are minimal.

Required:

- P0 enterprise skeleton and profile gates;
- P1A bootstrap identity and sessions;
- P2 channel registry with default-disabled channels;
- audit event contract and stub writer;
- policy decision contract and allow/deny decisions for channels;
- admin API for users, roles, channels, and audit query stub;
- personal profile regression checks.

Not required:

- full Workbench admin console;
- SSO;
- full ABAC;
- tamper-evident audit;
- enterprise chat connectors;
- container sandbox.

### External Enterprise Beta

External beta is ready when HASHI can demonstrate governed AAI journeys with real audit export and administrator controls.

Required:

- P1B projects, memberships, API tokens, service accounts;
- P3 policy MVP for channels, commands, backend switch, file read/write, and shell;
- P4 unified audit ledger query/export;
- P5 task, artifact, and evidence model;
- P8-min Workbench admin console;
- P9-min deployment, backup, restore, and migrations;
- end-to-end journeys from PRD Section 5.

Deferred:

- IdP-specific SAML/SCIM setup guides beyond the generic deployment runbook, and SCIM 2.0 compatibility beyond baseline Users, read-only Groups, discovery, and bounded Bulk surfaces;
- full ABAC policy simulation;
- live SIEM/OpenTelemetry exporter daemon mode, dashboards, deeper transforms, and production validation for each cloud identity model beyond the CLI checkpoint/retry runner, baseline Compose/Kubernetes/Helm scheduling, generic preset runbook, Kubernetes `secretKeyRef` support, and cloud/Vault SecretStore examples;
- cloud/object-store WORM adapters beyond local filesystem sink;
- Vault hardening beyond token-auth read provider, such as AppRole/Kubernetes auth and lease renewal;
- Helm/HA hardening beyond the baseline chart;
- multiple enterprise connectors;
- full DLP enforcement across every runtime and connector path.

---

## 4. Phase Plan

### P0 - Enterprise Skeleton, Profiles, And Contracts

**Goal:** create the enterprise foundation without changing personal behavior.

**Scope:**

- `DeploymentProfile` enum: `personal`, `team`, `enterprise`;
- profile loader and config resolution;
- `orchestrator/enterprise/` package skeleton;
- enterprise store interface;
- migration runner skeleton;
- audit event schema v0;
- policy decision schema v0;
- feature gates for governed mode;
- personal regression smoke tests.

**Primary code areas:**

- `orchestrator/enterprise/profile.py`;
- `orchestrator/enterprise/store.py`;
- `orchestrator/enterprise/migrations/`;
- `orchestrator/enterprise/audit_schema.py`;
- `orchestrator/enterprise/policy.py`;
- `orchestrator/config.py`;
- `orchestrator/startup_manager.py`;
- `instance_config.yaml` schema or equivalent Layer 4 config.

**Tickets:**

- `ENT-001` Create `orchestrator/enterprise/` skeleton.
- `ENT-002` Add `DeploymentProfile` enum and profile loader.
- `ENT-003` Add governed-mode config validation.
- `ENT-004` Add audit event schema v0 and stub writer.
- `ENT-005` Add policy decision schema v0.
- `ENT-006` Add personal profile regression tests.
- `ENT-007` Document profile config examples.

**Acceptance:**

- `personal` starts with no behavior change.
- `enterprise` without required org/bootstrap config fails fast with actionable error.
- Audit and policy contracts can be imported by later phases.

---

### P1A - Bootstrap Identity And Sessions

**Goal:** add the minimum identity system needed for governed operation.

**Scope:**

- organization record;
- bootstrap owner/admin;
- users;
- role enum;
- local password/session auth;
- enterprise auth provider metadata contract for OIDC readiness;
- OIDC authorization-code start with server-side PKCE verifier storage;
- OIDC callback state/code validation before token exchange;
- OIDC token exchange request construction and claim mapping skeleton;
- OIDC RS256/JWKS ID token signature verification and claim validation;
- OIDC verified identity user upsert and session completion service;
- OIDC token endpoint exchange and JWKS fetch/cache services;
- configurable OIDC callback full login completion;
- SAML IdP metadata parser and preverified assertion claims validator;
- SAML auth provider metadata, AuthnRequest start, callback state validation, verifier-hook/preverified assertion callback, user upsert, and session completion;
- SAML XML Signature verification wiring through IdP metadata signing certificates and `xmlsec1`, with fail-closed default callback behavior when no deployment verifier is supplied;
- SCIM-style user provisioning primitive for create, update, deactivate, and reactivate lifecycle sync;
- admin-gated SCIM provisioning HTTP API for user upsert and deactivation;
- admin-gated SCIM 2.0 Users compatibility surface for list, create, get, and PATCH replace flows;
- read-only SCIM 2.0 Groups compatibility surface that maps HASHI projects to groups and active project memberships to group members;
- SCIM 2.0 discovery surface for ServiceProviderConfig, ResourceTypes, and Schemas;
- SCIM 2.0 Bulk operations safety MVP for bounded Users create/get/patch batches;
- SSO/SCIM deployment runbook for current SAML `xmlsec1` verification and SCIM 2.0 operations;
- Workbench login endpoint;
- audit events for login/logout/admin bootstrap;
- personal profile maps current owner behavior to implicit top admin.

**Primary code areas:**

- `orchestrator/enterprise/identity.py`;
- `orchestrator/enterprise/auth_providers.py`;
- `orchestrator/enterprise/oidc_flow.py`;
- `orchestrator/enterprise/oidc_exchange.py`;
- `orchestrator/enterprise/oidc_session.py`;
- `orchestrator/enterprise/oidc_http.py`;
- `orchestrator/enterprise/oidc_token.py`;
- `orchestrator/enterprise/auth_session.py`;
- `orchestrator/enterprise/store.py`;
- `orchestrator/workbench_api.py`;
- `hashi.py` or setup CLI.

**Tickets:**

- `ENT-010` Add enterprise SQLite schema v0.
- `ENT-011` Add bootstrap admin flow.
- `ENT-012` Add user table and role enum.
- `ENT-013` Add password hashing and session tokens.
- `ENT-014` Add Workbench login/logout endpoints.
- `ENT-015` Emit audit events for auth and bootstrap.
- `ENT-016` Add OIDC provider metadata skeleton. Done for config parsing, provider readiness checks, public metadata, and secret redaction; full authorization-code flow remains future work.
- `ENT-017` Add OIDC authorization start with PKCE. Done for authorization URL generation, state/nonce, server-side code verifier storage, and start audit.
- `ENT-018` Add OIDC callback validation skeleton. Done for provider error handling, state validation, code presence checks, pending-flow consumption, and callback audit.
- `ENT-019` Add OIDC token exchange request and claim mapping skeleton. Done for form-body construction, secret/verifier redaction in public responses, callback exchange preparation, and deterministic external identity mapping; live HTTP token exchange, ID token cryptographic validation, user upsert, and session creation remain future work.
- `ENT-020` Add OIDC ID token claim validation. Done for issuer, audience, expiry, not-before, issued-at, subject, nonce, and redacted public payload checks.
- `ENT-021` Add OIDC RS256/JWKS ID token signature verification. Done for compact JWT parsing, `alg=RS256` enforcement, `kid` key selection, RSA signature verification, and fail-closed handling for unsafe JWT/JWKS inputs; live JWKS fetching/cache, user upsert, and session creation remain future work.
- `ENT-022` Add OIDC verified identity session completion. Done for active-user reuse, new `individual_user` creation, optional default-project membership, session issuance, and random non-OIDC password material for OIDC-created users; live token exchange/JWKS fetch/callback wiring remains future work.
- `ENT-023` Add OIDC HTTP token exchange and JWKS cache services. Done for injectable token endpoint calls, private token response handling, JWKS response validation, and TTL cache.
- `ENT-024` Wire OIDC callback full login completion. Done behind `enterprise_oidc_complete_login`, preserving default prepared-mode compatibility while enabling token exchange, JWKS fetch/cache, ID token verification, claim mapping, user/session completion, and no-token audit/response redaction.
- `ENT-024a` Add SAML metadata and assertion validation skeleton. Done for IdP metadata parsing, SSO binding selection, signing certificate discovery, preverified assertion issuer/audience/time validation, email/display-name extraction, and DTD/entity rejection.
- `ENT-024b` Add SAML HTTP login baseline. Done for SAML auth provider config/redaction, AuthnRequest start with RelayState, pending state tracking, callback state/provider validation, base64 `SAMLResponse` decoding, verifier-hook or explicitly enabled preverified assertion validation, enterprise user upsert, session issuance, default project membership, and no assertion/token material in audit logs.
- `ENT-024c` Add production SAML XML Signature verification wiring. Done for IdP metadata signing certificate extraction, required XML Signature presence, `xmlsec1 --verify` command wiring, fail-closed missing verifier/signature handling, and Workbench callback default verification path; IdP compatibility runbooks and packaged deployment checks remain future work.
- `ENT-025` Add SCIM provisioning primitive. Done for user create/update/deactivate/reactivate, standard `userName`/`emails` extraction, default project membership, session/API token revocation on deactivation, and deterministic result payloads; full group mutation, advanced filters, and IdP schema negotiation remain future work.
- `ENT-026` Add admin-gated SCIM provisioning API. Done for `/api/enterprise/scim/users` upsert and `/api/enterprise/scim/users/deactivate`, admin auth, audit events, default project assignment, and session/API token revocation on deactivation.
- `ENT-027` Add SCIM 2.0 Users compatibility surface. Done for admin-gated `/api/enterprise/scim/v2/Users` list/create/get/PATCH, SCIM User/ListResponse payloads, `userName`, `emails.value`, and `active` filters, pagination, PATCH `active`/`displayName`, audit for write paths, and token/session revocation on deactivation.
- `ENT-028` Add IdP-facing SCIM Users service-token surface. Done for `/scim/v2/Users` list/create/get/PATCH with API-token scope gates (`scim:read`, `scim:write`, `scim:*`), org boundary checks, write-path SCIM audit events, token redaction, and fail-closed rejection of session tokens or unscoped API tokens.
- `ENT-029` Add read-only SCIM Groups compatibility surface. Done for admin-gated and IdP-facing `/scim/v2/Groups` list/get, `displayName eq` and `id eq` filters, project-to-group mapping, active project members, scoped `scim:read` token gates, and org boundary checks; group mutation and advanced filters remain future work.
- `ENT-030` Add SCIM discovery endpoints. Done for admin-gated and IdP-facing `/scim/v2/ServiceProviderConfig`, `/ResourceTypes`, `/ResourceTypes/{type}`, `/Schemas`, and `/Schemas/{schema}` with Users/Groups capability metadata and scoped `scim:read` token gates; deeper schema extension negotiation remains future work.
- `ENT-031` Add SCIM Bulk operations safety MVP. Done for admin-gated and IdP-facing `/scim/v2/Bulk`, bounded operation count, `failOnErrors`, Users POST/GET/PATCH, org boundary checks for target users, scoped `scim:write` token gates, write-path audit events, and token redaction; Groups mutation and non-User bulk operations remain future work.
- `ENT-032` Add SSO/SCIM deployment runbook. Done for SAML `xmlsec1` prerequisites, fail-closed SAML readiness checks, SCIM service token scopes, SCIM endpoint matrix, Bulk safety contract, acceptance checklist, and rollback guidance; IdP-specific setup guides remain future work.

**Acceptance:**

- `org_admin` can log in.
- Workbench can discover configured login providers without exposing provider secrets.
- OIDC start does not expose `code_verifier`; the browser receives the authorization URL while HASHI keeps verifier and nonce state for callback validation.
- OIDC callback validates state before token exchange and does not write authorization codes into audit logs.
- OIDC callback can prepare a token exchange request without exposing authorization codes, PKCE verifiers, or client secrets in browser responses or audit logs.
- OIDC ID tokens can be verified against RS256 JWKS material, then validated for enterprise SSO claims without exposing nonce or raw token material in public payloads.
- OIDC verified identities can be completed into enterprise users and sessions without granting admin roles by default.
- OIDC token endpoint and JWKS calls can be tested with injected transports and do not expose token material in public payloads.
- OIDC callback can complete full login when explicitly enabled, while prepared-mode remains available for deployments that have not configured live SSO.
- SAML metadata can be parsed safely and preverified SAML assertion claims can be validated for issuer, audience, validity window, subject, email, and display name.
- SAML login can complete through HTTP start/callback when a deployment supplies `xmlsec1`, a custom verifier hook, or explicitly enables preverified assertion handoff; unsigned or unverified assertions fail closed by default.
- SCIM-style provisioning can sync enterprise users through service, admin API, IdP-facing SCIM 2.0 Users paths, and bounded SCIM Bulk batches without restoring old sessions or API tokens after deactivation, expose HASHI projects as read-only SCIM Groups for IdP discovery, and publish SCIM ServiceProviderConfig/ResourceTypes/Schemas metadata for IdP compatibility.
- Operators have a runbook for configuring SAML verification and SCIM token scopes, validating the deployed endpoints, and rolling back SSO/SCIM integration safely.

### Enterprise Secret Providers

**Goal:** move from ad hoc in-memory secrets toward a provider-based secret resolution layer.

**Scope implemented:**

- provider interface for connector/auth secret references;
- default `env://` and `secrets://` compatibility;
- root-confined `file://` mounted secret provider;
- `k8s://namespace/name/key` mounted secret provider for Kubernetes-style volume secrets;
- fail-closed `vault://` behavior until a real Vault provider is configured.

**Tickets:**

- `ENT-110` Add pluggable secret provider interface. Done.
- `ENT-111` Add file mounted secret provider. Done.
- `ENT-112` Add Kubernetes mounted secret provider. Done.
- `ENT-113` Add live Vault API provider. Done for token-auth read path, injectable client tests, KV v1/v2 field extraction, and fail-closed missing-field handling; AppRole/Kubernetes auth, lease renewal, and policy bootstrap remain future work.

**Acceptance:**

- Existing HASHI secret refs continue to work.
- File and Kubernetes mounted secrets cannot escape their configured roots.
- Unconfigured Vault/Kubernetes references fail closed.
- `individual_user` exists as a role but cannot access admin APIs.
- Personal profile does not require login migration.

---

### P1B - Projects, Memberships, API Tokens, And Service Accounts

**Goal:** make identity useful for real work routing and automation.

**Scope:**

- projects;
- project memberships;
- role assignment per project;
- scoped API tokens;
- service accounts for loops, jobs, and automation;
- project-scoped agent lookup.

**Primary code areas:**

- `orchestrator/enterprise/identity.py`;
- `orchestrator/enterprise/projects.py`;
- `orchestrator/agent_directory.py`;
- `orchestrator/workbench_api.py`.

**Tickets:**

- `ENT-020` Add project and membership schema.
- `ENT-021` Add role assignment APIs.
- `ENT-022` Add API token creation, scopes, expiry, and revoke.
- `ENT-023` Add service account records.
- `ENT-024` Add project-scoped agent lookup.

**Acceptance:**

- A user can belong to Project A but not Project B.
- API tokens can be revoked.
- Admin changes write audit events.

---

### P2 - Channel Registry And Governance

**Goal:** make channels administrator-controlled and disabled by default in governed profiles.

**Implementation status:** completed for the current P2 code slice.

Implemented checkpoints:

- enterprise channel schema and `ChannelRegistry`;
- default channel bootstrap:
  - `workbench` registered as the control-plane channel;
  - `hchat`, `telegram`, `whatsapp`, `email`, `slack`, `teams`, `google_chat`, and `feishu` registered disabled by default;
- channel admin API:
  - `GET /api/enterprise/channels`;
  - `POST /api/enterprise/channels`;
  - `POST /api/enterprise/channels/bind`;
- unified `EnterpriseChannelGate`;
- governed-profile gates for:
  - WhatsApp ingress and egress;
  - HChat Workbench exchange ingress and egress;
  - HChat CLI send-side egress;
  - Telegram command, message, and inline callback ingress;
- deny events written to the enterprise audit JSONL writer;
- personal profile remains allow-by-default for existing single-user behavior.

Residual P2 limitations:

- channel bindings currently use pragmatic identifiers such as Telegram user id, WhatsApp phone number, and agent id;
- project/task-aware channel authorization is deferred to P3/P5/P7;
- Workbench UI for channel administration is deferred to P8-min;
- direct Remote protocol paths outside Workbench exchange still need deeper trust and policy integration.

**Scope:**

- channel registry;
- channel enable/disable;
- channel bindings to users, teams, projects, and agents;
- ingress and egress checks;
- Workbench and HChat as initial governed channels;
- Telegram/WhatsApp remain supported but must pass channel gates in team/enterprise.

**Primary code areas:**

- `orchestrator/enterprise/channels.py`;
- `orchestrator/source_policy.py`;
- Telegram command/message entry points;
- `transports/whatsapp.py`;
- `remote/protocol_manager.py`;
- `orchestrator/workbench_api.py`.

**Tickets:**

- `ENT-030` Add channel schema and registry APIs. Done for MVP.
- `ENT-031` Default all non-core channels disabled in `team` and `enterprise`. Done for MVP.
- `ENT-032` Gate inbound channel messages. Done for Telegram, WhatsApp, and HChat exchange.
- `ENT-033` Gate outbound channel replies. Done for WhatsApp and HChat send/exchange; Telegram outbound remains tied to Telegram ingress in this slice.
- `ENT-034` Add channel audit events. Done for denied channel decisions.
- `ENT-035` Add admin APIs for channel enable and binding. Done for MVP.

**Acceptance:**

- Enterprise install does not expose Telegram/WhatsApp unless enabled.
- Unauthorized channel ingress is denied with an audit event.
- Personal owner can still enable local/personal channels.

---

### P2.5 - Admin API Surface

**Goal:** make governance operable before the full UI exists.

**Scope:**

- admin APIs for users, roles, projects, channels, policies, audit query, and export;
- role-gated API middleware;
- stable JSON contracts for Workbench to consume later.

**Primary code areas:**

- `orchestrator/workbench_api.py`;
- `orchestrator/enterprise/admin_api.py`;
- `orchestrator/enterprise/auth_session.py`.

**Tickets:**

- `ENT-040` Add `/api/enterprise/users`.
- `ENT-041` Add `/api/enterprise/projects`.
- `ENT-042` Add `/api/enterprise/channels`.
- `ENT-043` Add `/api/enterprise/policies`.
- `ENT-044` Add `/api/enterprise/audit`.
- `ENT-045` Add role-gated route tests.
- `ENT-046` Add `/api/enterprise/api-tokens` create/list/revoke lifecycle management.

**Acceptance:**

- `org_admin` can configure channels through API.
- `org_admin` can create scoped API tokens, list token metadata without secret material, and revoke tokens by ID.
- `individual_user` cannot call admin APIs.
- `auditor` can read audit APIs but cannot mutate settings.

---

### P3 - Policy MVP

**Goal:** replace scattered allow/deny checks with a central policy decision path for high-value actions.

**Implementation status:** P3A foundation completed; broader execution gates remain.

Implemented checkpoints:

- `policy_rules` persistence in the enterprise SQLite store;
- `PolicyEvaluator` with action/resource matching, scoped rules, simple conditions, priorities, and decisions:
  - `allow`;
  - `deny`;
  - `approval_required`;
- `evaluate_governance_policy(...)` compatibility entry point;
- personal profile remains allow-by-default;
- governed profiles can deny configured slash commands through `FlexibleAgentRuntime._is_command_allowed(...)`;
- channel access now has a central policy overlay after registry authorization:
  - disabled or unbound channels still fail closed at the registry layer;
  - allowed registry decisions can still be denied by `channel.access` policy;
  - `approval_required` channel decisions block access until the approval queue exists;
- backend switching is now gated by `backend.switch` policy before the backend manager changes active backend;
- HASHI-controlled API tool execution is now gated before `tool_registry.execute(...)`:
  - `bash` maps to `shell.execute`;
  - `file_write` maps to `file.write`;
  - `file_read` maps to `file.read`;
  - other tool calls map to `tool.execute`;
  - blocked tool calls return explicit tool-result errors instead of executing;
- `approval_required` policy decisions now create pending `approval_requests` records with sanitized context;
- non-allow policy decisions now append `policy` audit events to `state/enterprise_audit.jsonl`;
- targeted tests for default allow, explicit deny, approval-required, helper loading, personal profile behavior, and runtime command enforcement.

Residual P3 limitations:

- command deny currently maps to the existing `command_disabled` path until P4 ledger events distinguish `policy_denied`;
- browser automation and CLI-backend internal shell/file actions are not yet hard-gated by the evaluator;
- policy audit is still JSONL append-only; P4 will replace this with query/export ledger APIs and correlation IDs;
- approval queue API/UI, deduplication, and approve/deny execution resume are not yet implemented.

**Scope:**

- RBAC-first policy evaluator;
- decisions: `allow`, `deny`, `approval_required`;
- covered resources: channel, slash command, backend switch, file read/write, shell;
- approval queue stub;
- deny and approval events written to audit.

**Primary code areas:**

- `orchestrator/enterprise/policy.py`;
- `orchestrator/flexible_agent_runtime.py`;
- `orchestrator/admin_local_testing.py`;
- `adapters/*` where backend switch/tool execution is gated;
- tool execution dispatch.

**Tickets:**

- `ENT-050` Implement `PolicyEvaluator`. Done for P3A foundation.
- `ENT-051` Add role and project context to policy input.
- `ENT-052` Wire channel checks to evaluator. Done as a policy overlay after channel registry authorization.
- `ENT-053` Wire slash command checks to evaluator. Done for runtime command allow checks.
- `ENT-054` Wire backend switch checks to evaluator. Done for `_switch_backend_mode(...)`.
- `ENT-055` Wire file read/write and shell checks to evaluator. Done for HASHI-controlled API tool registry execution.
- `ENT-056` Add approval-required stub. Done with pending approval request records.
- `ENT-057` Add policy deny audit events. Done for deny and approval-required JSONL events.

**Acceptance:**

- `individual_user` cannot run admin-only commands.
- Unauthorized shell/file write is blocked before execution.
- Policy decisions are testable without running the full runtime.
- Personal profile defaults to owner-controlled allow behavior.

---

### P4 - Unified Audit Ledger

**Goal:** promote audit stubs and fragmented logs into a queryable enterprise ledger.

**Implementation status:** P4A-C ledger foundation, dual-write, and query APIs completed; first legacy adapter completed.

Implemented checkpoints:

- `audit_events` persistence in enterprise SQLite store with indexes for org/time, event type, actor, and project;
- `EnterpriseAuditLedger` with:
  - append;
  - append from existing `AuditEvent`;
  - query by event type, actor, project, task, request, and correlation id;
  - JSONL export;
  - tamper-evident hash-chain metadata (`chain_index`, `prev_hash`, `event_hash`) on new events;
  - `verify_chain()` to detect modified, deleted, or reordered chained events;
- stable ledger event schema version field;
- policy and channel deny/approval audit paths now dual-write into the unified ledger;
- Workbench enterprise audit APIs:
  - `GET /api/enterprise/audit` for admin-gated ledger query;
  - `GET /api/enterprise/audit/export` for admin-gated NDJSON export;
  - `GET /api/enterprise/audit/export?format=siem` for SIEM/ECS-style NDJSON mapping;
  - `GET /api/enterprise/audit/export?format=otel` for OpenTelemetry log-style NDJSON mapping;
- SDK-neutral live audit export service primitive:
  - SIEM/ECS NDJSON push body;
  - ledger NDJSON push body;
  - OTLP JSON log push body;
  - chain-index checkpoint support;
  - injectable transport for enterprise deployment adapters;
  - file-backed checkpoint persistence with atomic saves;
  - retry/backoff export cycle that advances checkpoint only after successful delivery;
  - `hashi enterprise audit-export-live` CLI runner for HTTP SIEM/ledger/OTLP pushes with checkpoint, headers, timeout, retry, and batch-size controls;
- live audit export deployment scheduling:
  - Docker Compose `audit-export` profile for one-shot scheduled runs;
  - raw Kubernetes CronJob with `concurrencyPolicy: Forbid`;
  - Helm `auditExport.enabled` CronJob template with persistent `/data/state` checkpoint path;
- live audit export operator presets:
  - generic NDJSON HTTP collector preset;
  - Splunk HEC event-envelope export format and preset;
  - Elasticsearch `_bulk` create-action export format and preset;
  - Elastic/Logstash HTTP input preset;
  - OpenTelemetry Collector HTTP logs preset;
  - Helm CronJob endpoint/header `secretKeyRef` support;
  - Helm examples for plain Kubernetes Secret and External Secrets Operator `ExternalSecret`;
  - External Secrets Operator `ClusterSecretStore` examples for AWS Secrets Manager, GCP Secret Manager, Azure Key Vault, and HashiCorp Vault;
- slash command audit JSONL adapter:
  - imports legacy `slash_command_audit.jsonl` records as `slash_command` ledger events;
  - preserves legacy timestamp, actor, command, status, channel, handler, duration, error, blocked reason, and side effects;
  - uses deterministic event IDs so migration/backfill jobs can be rerun without duplicate ledger rows;
- token audit JSONL adapter:
  - imports legacy `token_audit.jsonl` records as `model_invocation` ledger events;
  - preserves request id, request fingerprint, backend, model, source, success status, token counts, tool telemetry, wrapper telemetry, and legacy context;
  - uses deterministic event IDs for safe migration/backfill reruns;
- Remote audit JSONL adapter:
  - imports legacy Hashi Remote `audit.jsonl` records as `remote` ledger events;
  - preserves HChat relay, terminal execution, pairing, and peer discovery metadata;
  - converts epoch timestamps to ISO8601 and uses deterministic event IDs for safe migration/backfill reruns;
- browser action audit JSONL adapter:
  - imports legacy `browser_action_audit.jsonl` records as `tool` ledger events;
  - preserves action, request id, browser session id, actor hints, arguments, response metadata, and elapsed time;
  - converts epoch timestamps to ISO8601 and uses deterministic event IDs for safe migration/backfill reruns;
- HASHI-controlled tool action audit source and adapter:
  - `ToolRegistry.execute()` now writes best-effort `tool_action_audit.jsonl` records for allowed, failed, and not-allowed tool calls;
  - records tool name, tool call id, agent, workspace, safety mode, redacted arguments, output snippet, status, and duration;
  - imports `tool_action_audit.jsonl` records as `tool` ledger events with deterministic event IDs;
- audit schema contract tests:
  - lock the required top-level ledger keys;
  - verify canonical event types remain queryable and exportable;
  - verify ledger context remains JSON-safe for non-primitive Python values;
- tests for append/query/export, compatibility with existing `AuditEvent`, and slash audit ingestion.

Residual P4 limitations:

- hash-chain verification detects tampering in the SQLite ledger, but it is not WORM storage and does not prevent an attacker with database write access from replacing the whole database or recomputing a chain;
- slash command JSONL can now be ingested into the ledger, but live slash command dual-write is still pending;
- token audit JSONL can now be ingested into the ledger, but live token audit dual-write is still pending;
- Remote audit JSONL can now be ingested into the ledger, but live Remote/HChat dual-write is still pending;
- browser action JSONL can now be ingested into the ledger, but live browser/tool dual-write is still pending;
- HASHI-controlled tool execution now writes `tool_action_audit.jsonl`, but direct live ledger dual-write is still pending;
- auditor read-only role semantics are not yet separated from broader admin access;
- generic shell/file tool execution now has a canonical JSONL event source and ingest adapter;
- generic object-store WORM sink is present, but cloud-specific SDK wiring and deployment runbooks remain future work;
- retention, long-running daemon orchestration, deeper vendor transforms/dashboards, and production validation for each cloud identity model remain future P4 work.

**Scope:**

- append-only SQLite-backed ledger;
- JSONL export;
- event IDs, parent IDs, request IDs, task IDs, actor IDs, correlation IDs;
- query by user, project, task, event type, and time range;
- adapters for slash command audit, token audit, HChat/Remote, tool events, file events, policy events, channel events;
- retention setting.

**Primary code areas:**

- `orchestrator/enterprise/audit_ledger.py`;
- `orchestrator/enterprise/audit_schema.py`;
- `orchestrator/enterprise/audit_adapters/`;
- `orchestrator/slash_command_audit.py`;
- token audit writer;
- `remote/protocol_manager.py`;
- tool dispatch code.

**Tickets:**

- `ENT-060` Implement append-only ledger store. Done for P4A foundation.
- `ENT-061` Add query API and pagination. Query filters and Workbench API done; pagination cursor pending.
- `ENT-062` Add JSONL export. Done for P4A foundation.
- `ENT-063` Add slash audit adapter. Done for legacy JSONL ingest with idempotent event IDs; live dual-write pending.
- `ENT-064` Add token/model invocation adapter. Done for legacy JSONL ingest with idempotent event IDs; live dual-write pending.
- `ENT-065` Add HChat/Remote adapter. Done for legacy Remote audit JSONL ingest with idempotent event IDs; live dual-write pending.
- `ENT-066` Add tool/file event adapters. Browser action legacy JSONL ingest done; HASHI-controlled tool action JSONL source and ingest adapter done; live ledger dual-write pending.
- `ENT-067` Add audit schema contract tests. Done for required ledger keys, canonical event types, export shape, and JSON-safe context.
- `ENT-068` Add tamper-evident audit hash chain. Done for new ledger events and verification API.
- `ENT-068a` Add external audit anchor manifest. Done for chain-range anchor records, anchor hash, JSON export, historical anchor verification, and tamper detection.
- `ENT-068b` Add filesystem WORM-style audit anchor sink. Done for hash-named append-only anchor writes, read-only local files, idempotent receipt handling, and receipt verification.
- `ENT-068c` Add object-store WORM-style audit anchor sink. Done for SDK-neutral no-overwrite object writes, hash-named keys, receipt verification, idempotent conflict handling, and object-lock metadata forwarding; cloud-specific client packages and deployment runbooks remain future work.
- `ENT-069` Add SIEM/OpenTelemetry audit export mappings. Done for admin-gated NDJSON export formats.
- `ENT-069a` Add live audit export service primitive. Done for SIEM/ECS NDJSON, ledger NDJSON, OTLP JSON log payloads, chain-index checkpoints, injectable transport, no-op current checkpoint behavior, and fail-closed HTTP status handling.
- `ENT-069b` Add live audit export checkpoint and retry cycle. Done for file-backed checkpoint load/save, corrupt checkpoint fail-fast, retry/backoff, no checkpoint advancement on failed attempts, and checkpoint advancement only after successful delivery.
- `ENT-069c` Add live audit export CLI runner. Done for `hashi enterprise audit-export-live` with HTTP POST transport, ledger/SIEM/OTLP format selection, operator headers, timeout, retry, batch-size, and checkpoint controls; long-running daemon orchestration and deployment-specific auth presets remain future work.
- `ENT-069d` Add live audit export deployment scheduling. Done for Docker Compose `audit-export` one-shot profile, raw Kubernetes CronJob, Helm `auditExport.enabled` CronJob template, checkpoint persistence under `/data/state`, and operator documentation; vendor-specific SIEM auth presets and long-running daemon mode remain future work.
- `ENT-069e` Add live audit export operator presets. Done for generic NDJSON, Splunk HEC event-envelope, Elasticsearch `_bulk`, Elastic/Logstash HTTP input, and OpenTelemetry Collector HTTP logs preset examples plus an operator runbook with compatibility warnings and acceptance checks; managed daemon mode, deeper transforms, dashboards, and secret-manager-native Helm wiring remain future work.
- `ENT-069f` Add Helm secret refs for audit export. Done for endpoint/header `secretKeyRef` values in the audit export CronJob, example secret keys, and operator documentation; External Secrets Operator and cloud-provider secret-store examples remain future work.
- `ENT-069g` Add audit export secret delivery examples. Done for plain Kubernetes Secret and generic External Secrets Operator `ExternalSecret` examples for audit export endpoint/header delivery; cloud-specific `SecretStore` manifests remain future work.
- `ENT-069h` Add cloud SecretStore examples. Done for External Secrets Operator `ClusterSecretStore` examples covering AWS Secrets Manager, GCP Secret Manager, Azure Key Vault, and HashiCorp Vault; production identity validation remains future work.

**Acceptance:**

- A governed task produces a queryable timeline.
- Policy deny and channel deny events are never dropped.
- JSONL export contains stable schema version.
- New ledger events carry hash-chain fields and fail verification if event content is modified.
- SIEM and OpenTelemetry export formats include event identity, actor, org, correlation, hash-chain metadata, and sanitized context.
- Live export service can push audit batches from a chain-index checkpoint without embedding external SDK dependencies.
- Live export cycle persists delivery checkpoints and retries transient failures without skipping undelivered events.
- Operators can run a one-shot live export cycle from the HASHI CLI and schedule it externally through cron, systemd, or Kubernetes CronJob.
- Raw Kubernetes and Helm deployments include baseline CronJob scheduling for the live exporter, and Compose deployments include an `audit-export` profile suitable for cron/systemd wrappers.
- Operators have preset guidance and exporter formats for generic NDJSON, Splunk HEC, Elasticsearch `_bulk`, Elastic/Logstash, and OTLP collectors, including explicit warnings where collector behavior must be validated.
- Helm operators can keep audit export endpoint/header values in Kubernetes Secrets and reference them through `auditExport.endpointSecretRef` and `auditExport.headerSecretRef`.
- Operators have example manifests for both direct Kubernetes Secret delivery and External Secrets Operator reconciliation of the same audit export secret keys.
- Operators have cloud/Vault SecretStore templates to adapt for AWS, GCP, Azure, and Vault-based secret delivery.

---

### P5 - Task, Artifact, And Evidence Model

**Goal:** make delegated work inspectable as tasks with artifacts and evidence bundles.

**Implementation status:** P5A-P5C service foundation completed.

Implemented checkpoints:

- task schema and `TaskRegistry` service:
  - task lifecycle statuses: `delegated`, `in_progress`, `awaiting_approval`, `completed`, `failed`;
  - project/user/agent scoped task records;
  - list and transition helpers;
- artifact schema and `ArtifactRegistry` service:
  - task-linked artifact records;
  - artifact type, path, metadata, and optional file hash;
- governed file-write artifact registration:
  - `ToolRegistry.execute()` auto-registers successful `file_write` artifacts when enterprise task context is present;
  - personal/non-enterprise tool calls remain unchanged;
  - `tool_action_audit.jsonl` includes org, project, task, and artifact identifiers when available;
- evidence bundle schema and `EvidenceBundleRegistry` service:
  - bundles link a task to audit event ids and artifact ids;
  - helper builds a bundle from current ledger task events and registered artifacts;
- Superloop evidence linkage:
  - `SuperloopStore.attach_evidence_bundle()` records enterprise evidence bundle ids in loop state;
  - matching taskboard tasks receive `evidence_bundle_ids` and `enterprise_evidence_bundle_id`;
  - the operation is idempotent and writes an auditable `evidence.bundle_attached` loop event;
  - closeout validation treats evidence bundle fields as valid evidence.

Residual P5 limitations:

- artifact auto-registration currently covers HASHI-controlled `file_write`; CLI backend internal writes still need event mapping or completion verification;
- runtime closeout does not yet automatically build and attach evidence bundles;
- task APIs and Workbench UI are deferred to P7/P8;
- missing promised artifact verification remains P6 work.

**Scope:**

- task registry;
- task lifecycle;
- artifact records for files, reports, commits, and exports;
- evidence bundle builder from audit slices and artifact links;
- Superloop closeout can attach evidence bundle IDs.

**Primary code areas:**

- `orchestrator/enterprise/tasks.py`;
- `orchestrator/enterprise/artifacts.py`;
- `orchestrator/enterprise/evidence.py`;
- `orchestrator/superloop_store.py`;
- tool/file write paths;
- Nagare/flow entry points.

**Tickets:**

- `ENT-070` Add task schema and state machine. Done for service-layer foundation.
- `ENT-071` Add artifact registry. Done for service-layer foundation.
- `ENT-072` Register artifacts from governed file writes. Done for `ToolRegistry` `file_write` with explicit enterprise task context.
- `ENT-073` Build evidence bundle from audit range and artifacts. Done for task-scoped ledger events and registered artifacts.
- `ENT-074` Link Superloop closeout to evidence bundles. Done at the store/taskboard layer; runtime closeout automation remains P7/P8 wiring.

**Acceptance:**

- A file-producing task links changed files to the task.
- A completed task can export an evidence bundle.
- Missing promised artifacts can fail verification in later P6.

---

### P6 - Secure Execution And Verification

**Goal:** add hard execution boundaries and completion checks.

**Scope:**

- project workspace boundary;
- shell and file operation enforcement;
- network egress allowlist stub;
- data classification and egress assessment primitive;
- browser automation policy flag;
- completion verification hook for file-producing tasks;
- explicit failure report when verification fails.

**Implementation status:** P6A-P6B service primitives completed.

Implemented checkpoints:

- `verify_promised_artifacts()` compares a task's promised artifacts with registered artifact records;
- task metadata keys `promised_artifacts`, `required_artifacts`, and `expected_artifacts` are supported;
- artifact requirements can match by full path, basename, artifact id, or type;
- `fail_task_if_promised_artifacts_missing()` provides the completion hook that marks a task failed with a clear missing-artifact reason.
- `ExecutionScope.from_project()` resolves the governed project workspace root;
- `ExecutionScope.check_path()` and `require_path()` block path traversal, out-of-workspace absolute paths, and symlink escape attempts.
- `ToolRegistry.execute()` applies the enterprise path gate before HASHI-controlled `file_read`, `file_write`, `file_list`, and `apply_patch` dispatch when org/project context is present;
- denied file-tool actions fail closed, write normal tool audit records, and do not register artifacts.
- `ToolRegistry.execute()` applies an enterprise shell gate before `bash` dispatch when org/project context is present;
- governed shell execution defaults closed and requires explicit `enterprise_shell_enabled` or `bash.enterprise_enabled`.
- `ToolRegistry.execute()` applies an enterprise network egress gate before `http_request`, `web_fetch`, and `web_search`;
- governed network egress defaults closed and supports exact hosts, `*.example.com` suffix patterns, or `*` via `enterprise_network_allow_hosts` / `network.allow_hosts`.
- `classify_text()` detects baseline enterprise-sensitive content classes, including email addresses, secret assignments, private keys, and Luhn-valid payment cards;
- `assess_data_egress()` returns allow, approval-required, or deny decisions from classification thresholds and destination-region allowlists.
- `ConnectorExecutionService` applies data-governance checks before Slack and Google Chat `message.send`; confidential content creates a `data.egress` approval request, restricted content is denied, and connector audit records redact outbound message text.
- `ToolRegistry.execute()` applies an enterprise browser automation gate before all `browser_*` tools;
- governed browser automation defaults closed and requires explicit `enterprise_browser_enabled` or `browser.enterprise_enabled`.
- `complete_task_with_artifact_verification()` provides an enterprise completion path that marks a task `completed` only when promised artifacts are present, otherwise marks it `failed` with a clear missing-artifact reason.

Residual P6 limitations:

- workspace boundary is wired into HASHI-controlled file tools;
- shell execution has an explicit governed enable gate; command allow/deny pattern policy still uses the existing bash `blocked_patterns`;
- network egress has a host allowlist stub for HASHI-controlled network tools;
- data governance is enforced for Slack/Google Chat webhook `message.send`, but is not yet automatically enforced across every connector, channel, artifact export, or backend path;
- browser automation has an explicit governed enable gate;
- the verification hook is available as an enterprise completion helper but is not yet automatically invoked by every runtime path;
- CLI backend internal writes still need tool-event mapping or post-run artifact discovery.

**Primary code areas:**

- `orchestrator/enterprise/execution.py`;
- `orchestrator/enterprise/verification.py`;
- `orchestrator/config.py`;
- tool dispatch code;
- browser mode code.

**Tickets:**

- `ENT-080` Add execution scope resolver. Done at service layer for project workspace roots.
- `ENT-081` Add path traversal block tests. Done for relative, absolute, and symlink escapes.
- `ENT-082a` Wire workspace boundary into HASHI-controlled file tools. Done for `file_read`, `file_write`, `file_list`, and `apply_patch`.
- `ENT-082` Add shell command policy checks. Done for default-deny governed shell gate; command pattern policies remain configured through existing `bash.blocked_patterns`.
- `ENT-083` Add network egress allowlist stub. Done for `http_request`, `web_fetch`, and `web_search`.
- `ENT-083a` Add data classification and egress assessment. Done for baseline sensitive-data findings, redacted snippets, classification thresholds, approval/deny decisions, and destination-region allowlists.
- `ENT-083b` Wire data-governance checks into outbound connector messages. Done for Slack and Google Chat `message.send`, approval requests for confidential data, fail-closed denial for restricted data, and audit redaction of message text/result payloads.
- `ENT-084` Add browser automation policy flag. Done for all `browser_*` tools.
- `ENT-085` Add completion verification hook. Done with complete-or-fail task helper for promised artifact checks.

**Acceptance:**

- Workspace escape attempts are blocked.
- Unapproved shell/file actions do not execute.
- A task that promises artifacts but does not produce them fails clearly.

---

### P7 - Project-Aware Routing And Approvals

**Goal:** route work by project, role, and approval state.

**Implementation status:** P7A-P7C service/API foundations completed.

Implemented checkpoints:

- `PolicyEvaluator.decide_approval_request()` supports pending approval decisions;
- approval requests can transition to `approved` or `denied` exactly once;
- approval decisions write canonical `policy` ledger events with project/task/request correlation.
- Workbench exposes approval queue API routes:
  - `GET /api/enterprise/approvals`;
  - `POST /api/enterprise/approvals/{request_id}/approve`;
  - `POST /api/enterprise/approvals/{request_id}/deny`.
- bridge routing now enforces explicit `project_id` context:
  - sender and target agents must both be assigned to the requested project;
  - cross-project targets fail before runtime enqueue;
  - legacy messages without `project_id` preserve current personal behavior.
- failed tasks can now emit canonical escalation ledger events:
  - `task.escalate_failed` records project/task correlation;
  - escalation context includes failure reason, task summary, agent/user ids, severity, and optional escalation target;
  - a helper can fail a task and record the escalation event in one operation.
- enterprise agent capability summaries are now available as a service primitive:
  - project assignments, active backend, allowed backends, allowed tools, bridge permissions, scopes, and tags are normalized;
  - summaries can be filtered by project for governed routing/admin views.

Residual P7 limitations:

- Workbench approval UI is not yet implemented;
- approved requests are not yet consumed to resume blocked work automatically;
- project-aware routing is implemented only for explicit bridge `project_id`; broader channel/project resolution remains pending.
- failed-task escalation writes audit events only; notification delivery and retry routing remain pending.
- agent capability registry is service-layer only; Workbench API/UI exposure remains pending.

**Scope:**

- project-aware message routing;
- task queue and approval queue;
- admin approval/deny;
- failed-task escalation;
- agent capability registry.

**Primary code areas:**

- `orchestrator/conversation_router.py`;
- `orchestrator/commands/queue.py`;
- `orchestrator/enterprise/tasks.py`;
- `orchestrator/enterprise/policy.py`;
- `remote/routing.py`;
- Nagare/flow entry points.

**Tickets:**

- `ENT-090` Add project-aware inbound routing. Done for bridge messages that carry explicit `project_id`, with fail-closed sender/target checks.
- `ENT-091` Add approval queue APIs. Done for Workbench admin list endpoint.
- `ENT-092` Add admin approve/deny action. Done for service and Workbench admin API routes with ledger events.
- `ENT-093` Add failed-task escalation events. Done with ledger-backed `task.escalate_failed` helpers.
- `ENT-094` Add agent capability registry. Done as a service primitive with project-filtered summaries.

**Acceptance:**

- Messages from Project A do not route to Project B agents.
- Approval-required actions do not execute until approved.
- Approval decisions write audit events.

---

### P8 - Workbench Admin Console

**Goal:** provide a human admin surface for enterprise controls.

**Implementation status:** P8-min API groundwork started.

Implemented checkpoints:

- Workbench admin API exposes project-filterable agent capability summaries at `GET /api/enterprise/agent-capabilities`.
- The endpoint is guarded by enterprise admin auth and returns normalized backend/tool/project/bridge capability data.
- Workbench admin API exposes policy rules at `GET /api/enterprise/policies` and `POST /api/enterprise/policies`.
- Policy rule creation supports action, resource, effect, scope, conditions, priority, and optional explicit rule id.
- `/api/health` includes an enterprise block in governed profiles with identity, channel registry, audit ledger, and policy evaluator readiness.
- Personal profile health keeps the legacy response shape without enterprise fields.
- `auditor` role can query and export audit records without receiving broader admin mutation rights.

Residual P8 limitations:

- Frontend admin screens are not yet implemented.
- Capability summaries are read-only; approval UI remains pending.
- Policy API supports list/create only; delete, simulation, and richer versioning remain pending.
- Enterprise health is a readiness summary, not yet a full monitoring dashboard.

**Minimum scope:**

- users and roles;
- projects;
- channels;
- policies;
- audit ledger viewer;
- audit export;
- system health.

**Primary code areas:**

- Workbench frontend;
- `orchestrator/workbench_api.py`;
- `orchestrator/enterprise/admin_api.py`.

**Tickets:**

- `ENT-100` Add role-gated admin navigation.
- `ENT-101` Add users and roles screens.
- `ENT-102` Add projects screen.
- `ENT-103` Add channel registry screen.
- `ENT-104` Add policy viewer/editor. API list/create done; frontend and advanced edit operations remain pending.
- `ENT-105` Add audit timeline and export screen.
- `ENT-106` Add enterprise health screen.
- `ENT-106A` Add enterprise service readiness to `/api/health`. Done for API-level identity/channel/audit/policy checks.
- `ENT-107` Add admin API for agent capability inventory. Done for read-only project-filtered summaries.

**Acceptance:**

- `org_admin` can enable a channel and assign it to a project.
- `auditor` can export audit but cannot mutate settings. API-level audit query/export is implemented.
- `individual_user` cannot see admin navigation.

---

### P9 - Deployment, Backup, Migration, And Operations

**Goal:** make enterprise mode installable, upgradeable, and recoverable.

**Implementation status:** P9-min service groundwork active, with Kubernetes and Helm baselines present.

Implemented checkpoints:

- Enterprise backup/restore service primitive can create `.tar.gz` archives with a JSON manifest.
- Required backup items fail fast when missing; optional missing items are recorded in the manifest.
- Restore is safe-by-default: archive paths are validated, overwrite is blocked unless explicit, and unsupported member types are rejected.
- `hashi.py enterprise backup|restore|inspect-backup` wraps the backup service for operator use.
- CLI backup defaults include `state/enterprise.sqlite`, `state/enterprise_audit.jsonl`, `agents.json`, and `agent_capabilities.json`; workspaces are opt-in to avoid unexpectedly large archives.
- Enterprise deployment skeleton added:
  - `Dockerfile.enterprise`;
  - `deploy/docker-compose.enterprise.yml`;
  - `deploy/enterprise.env.example`;
  - `deploy/kubernetes/enterprise/`;
  - `docs/HASHI_ENTERPRISE_DEPLOYMENT.md`.
- `hashi.py enterprise migrate` initializes or refreshes the enterprise SQLite schema idempotently and reports before/after schema versions.
- Kubernetes baseline manifests are present for namespace, config, example secrets, persistent data, deployment, service, and `/api/health` probes.
- Helm baseline chart is present for configurable image/service/resource/probe settings, enterprise profile wiring, persistent `/data`, read-only connector secret mounts, optional ingress, optional NetworkPolicy, and optional HPA skeleton.

Residual P9 limitations:

- Docker Compose skeleton is present but has not yet been build/run verified in CI.
- Kubernetes/Helm deployment remains baseline-grade; external database wiring, multi-replica state coordination, production ingress policy, cluster-specific NetworkPolicy validation, and autoscaling runbooks remain future work.
- Backup policy and scheduled backups are not yet implemented.
- Migration runner is a schema initializer, not yet a multi-file versioned migration framework.

**Scope:**

- Docker Compose enterprise profile;
- volume layout;
- environment config;
- migrations;
- backup/restore CLI;
- health endpoints;
- upgrade and rollback playbook.

**Primary code areas:**

- `deploy/docker-compose.enterprise.yml`;
- `deploy/kubernetes/enterprise/`;
- `deploy/helm/hashi-enterprise/`;
- Dockerfile or package metadata;
- `orchestrator/enterprise/migrations/`;
- `hashi.py enterprise backup|restore|migrate`;
- `orchestrator/workbench_api.py` health endpoints.

**Tickets:**

- `ENT-110` Add enterprise Docker Compose skeleton. Done as first-pass compose template.
- `ENT-111` Add enterprise volume layout. Done for state, workspaces, logs, and backups named volumes.
- `ENT-112` Add migration runner command. Done for idempotent schema initialization and schema version reporting.
- `ENT-113` Add backup/restore command. Done for backup, restore, and manifest inspection CLI.
- `ENT-114` Add enterprise health checks. Done for Workbench `/api/health`, Docker healthcheck, and Kubernetes liveness/readiness probe manifests.
- `ENT-115` Add upgrade and rollback documentation.
- `ENT-116` Add Kubernetes baseline manifests. Done for namespace, config map, example secret, PVC, single-replica deployment, service, data/secret mounts, and manifest contract tests; HA/Helm remains future work.
- `ENT-117` Add enterprise Helm baseline chart. Done for chart metadata, values, service account, config map, example secret, PVC, deployment, service, optional ingress, optional NetworkPolicy, optional HPA, README, and chart contract tests; true multi-replica HA remains future work.

**Acceptance:**

- Fresh install can bootstrap enterprise admin.
- Backup/restore round trip preserves users, channels, policies, and audit.
- Migrations are versioned and repeatable.

---

### P10 - Enterprise Connectors

**Goal:** add governed integrations after the control plane is stable.

**Implementation status:** P10 service interface and execution gate groundwork started.

Implemented checkpoints:

- Connector interface package added under `orchestrator/enterprise/connectors/`.
- `ConnectorAction`, `ConnectorResult`, `ConnectorHealth`, and `EnterpriseConnector` protocol define the first connector contract.
- `record_connector_event()` writes canonical connector ledger events and redacts sensitive action parameters.
- Connector scoped credential store added:
  - credentials store connector type, display name, secret reference, scopes, and status;
  - revoke marks credentials as revoked and hides them from default active queries;
  - schema version now includes `connector_credentials`.
- Connector execution gate added:
  - validates the requested connector credential exists before action execution;
  - fails closed when a credential is revoked or inactive;
  - fails closed when a credential belongs to a different organization than the active policy evaluator;
  - fails closed when the credential connector type does not match the requested connector action;
  - calls the enterprise `PolicyEvaluator` with `connector.execute` and a `connector:{type}:{action}` resource;
  - supports explicit policy deny and `approval_required` decisions before a connector implementation can run;
  - creates pending approval requests for connector actions that require approval.
- Connector registry and health probe foundation added:
  - connectors can be registered by connector type;
  - duplicate or unnamed connector types fail fast;
  - health probes normalize connector exceptions to `unhealthy` summaries;
  - health probes can write canonical connector ledger events;
  - Workbench exposes admin-gated `GET /api/enterprise/connectors/health`.
- First GitHub connector foundation added:
  - `health_check()` probes GitHub rate-limit metadata;
  - `repo.get` and `repo.read` fetch repository metadata;
  - `dry_run` returns the planned repository lookup without external calls;
  - unsupported write actions fail closed as `unsupported_action`;
  - network transport is injectable for deterministic tests.
- GitHub `issue.create` action added:
  - validates repository and title inputs;
  - supports `dry_run` without external calls;
  - sends issue title, body, and labels through GitHub's issues API when executed;
  - returns issue id, number, URL, title, and state.
- GitHub `pr.create` action added:
  - validates repository, title, head, and base inputs;
  - supports `dry_run` without external calls;
  - sends pull request title, head, base, body, and draft state through GitHub's pulls API when executed;
  - returns pull request id, number, URL, title, state, and draft state.
- GitHub `pr.merge` action added:
  - validates repository, pull number, and merge method;
  - supports `dry_run` without external calls;
  - sends merge method, optional commit title/message, and optional expected SHA through GitHub's merge API when executed;
  - returns merge SHA, merged state, and GitHub merge message.
- Gated connector execution service added:
  - every execution checks credential state and policy before invoking the connector;
  - policy deny and approval-required decisions return blocked `ConnectorResult` values without calling connector code;
  - missing connector registrations fail closed;
  - successful, blocked, failed, and approval-required attempts can write canonical connector ledger events.
- Workbench connector execution API added:
  - `POST /api/enterprise/connectors/execute` is admin-gated;
  - requests require `connector_type`, `action`, and `credential_id`;
  - the API constructs a `ConnectorAction` with actor, project, task, request, correlation, dry-run, and parameters;
  - execution always goes through `ConnectorExecutionService`;
  - responses include both connector `result` and explicit `gate` decision metadata.
- Workbench connector credential admin API added:
  - `GET /api/enterprise/connectors/credentials` lists active connector credential references;
  - `POST /api/enterprise/connectors/credentials` creates scoped connector credential references;
  - `POST /api/enterprise/connectors/credentials/{credential_id}/revoke` revokes credentials;
  - credential create and revoke operations write admin audit events.
- Connector secret reference resolver added:
  - `env://NAME` and `env:NAME` resolve from environment variables;
  - `secrets://key` and `hashi://key` resolve from HASHI's in-memory `secrets.json` mapping;
  - resolved secret metadata redacts values by default;
  - unsupported schemes and unconfigured `vault://` references fail closed.
- Connector factory added:
  - builds typed connector instances from active connector credentials;
  - resolves connector tokens through `ConnectorSecretResolver`;
  - creates GitHub connector instances with resolved tokens;
  - skips revoked credentials when building registries;
  - unsupported connector types fail closed.
- Workbench connector registry refresh added:
  - Workbench builds its in-process connector registry from active credential references at startup;
  - credential create/revoke refreshes the registry;
  - static test/injected connectors take precedence over credential-built connectors of the same type;
  - unresolved connector secrets are captured as registry errors instead of breaking Workbench startup.
- Default connector policy template added:
  - explicitly allows GitHub read-only repository metadata actions;
  - requires approval for GitHub `issue.create`, `pr.create`, and `pr.merge`;
  - requires approval for Slack `message.send` to avoid enabling outbound channel egress by default;
  - requires approval for Google Chat `message.send` to avoid enabling outbound channel egress by default;
  - template installation is idempotent.
- Default connector policy install API added:
  - `POST /api/enterprise/policies/install-defaults` is admin-gated;
  - installs or returns the default connector policy rules without duplicating them;
  - writes an admin audit event with installed rule ids.
- First enterprise channel connector added:
  - Slack incoming webhook connector supports `message.send`;
  - `health_check()` validates webhook configuration without making a network call;
  - `dry_run` returns the planned Slack payload without posting externally;
  - real execution posts text and optional block payloads through an injectable transport;
  - factory can build Slack connectors from scoped credential secret references.
- Second enterprise channel connector added:
  - Google Chat incoming webhook connector supports `message.send`;
  - `health_check()` validates webhook configuration without making a network call;
  - `dry_run` returns the planned Google Chat payload without posting externally;
  - real execution posts text and optional card payloads through an injectable transport;
  - factory can build Google Chat connectors from scoped credential secret references.
- Connector server-side validation added:
  - Workbench connector execution API rejects webhook `message.send` actions without non-empty `text` before policy/connector execution.
- Workbench connector admin UI MVP added:
  - Enterprise layout exposes connector credentials, health, and default connector policy controls;
  - admins can create connector credential references, include revoked credentials in the list, and revoke active credentials;
  - admins can refresh connector health and see registry secret-resolution errors;
  - admins can install the default connector policy template from the Workbench;
  - admins can run gated connector test actions, with dry-run enabled by default, and inspect the execution result and policy gate metadata;
  - Slack, Google Chat, and GitHub setup/test forms include safe presets and JSON parameter validation.

Residual P10 limitations:

- GitHub has repository metadata, issue creation, PR creation, and PR merge actions.
- Slack exists as an incoming webhook MVP only; Slack OAuth, Bot API, channel discovery, and user mapping are not implemented yet.
- Google Chat exists as an incoming webhook MVP only; Google Chat OAuth, space discovery, and user mapping are not implemented yet.
- No Teams or Feishu connector is implemented yet.
- Credential store records secret references only; environment and HASHI secrets can now be resolved through a dedicated resolver, while Vault/Kubernetes secret resolution remains pending.
- Connector factory currently supports GitHub, Slack, and Google Chat; Teams and Feishu factory support remains pending.
- Workbench registry refresh is in-process; multi-node registry synchronization remains future work.
- Default connector policy covers GitHub reads, GitHub writes, Slack outbound message approval, and Google Chat outbound message approval; silent auto-install remains intentionally avoided to prevent overwriting administrator policy edits.
- Workbench/admin connector execution API now uses the gated execution service.
- Connector health API exists for registered in-process connectors; built-in GitHub, Slack, and Google Chat connectors can now be constructed from credential references.
- Connector admin UI exists as a Workbench MVP; richer guided setup, OAuth flows, and broader connector-specific server-side validation remain pending.

**Scope:**

- choose one enterprise channel connector and one system connector first;
- recommended first pair: Microsoft Teams or Slack, plus GitHub;
- every connector must include scoped credentials, policy hooks, audit events, health probe, and revoke behavior.

**Primary code areas:**

- `orchestrator/enterprise/connectors/`;
- channel registry;
- audit ledger;
- policy evaluator;
- Workbench admin console.

**Tickets:**

- `ENT-120` Define connector interface. Done for service contract and audit event helper.
- `ENT-121` Add scoped credential store abstraction. Done for secret references, scopes, active listing, and revoke.
- `ENT-122` Add connector execution gate. Done for credential existence, org isolation, revoke fail-closed, type match, policy deny, and approval-required decisions.
- `ENT-126` Add first enterprise channel connector. Done for Slack incoming webhook health, dry-run, `message.send`, injectable transport, and factory construction from secret refs.
- `ENT-136` Add second enterprise channel connector. Done for Google Chat incoming webhook health, dry-run, `message.send`, injectable transport, factory construction from secret refs, default approval-required policy, Workbench presets, and server-side `message.send` text validation.
- `ENT-123` Add GitHub connector with audit. Done for health, repository metadata, issue creation, PR creation, and PR merge actions.
- `ENT-124` Add connector health checks. Done for in-process registry, normalized health summaries, ledger health events, and Workbench admin health API.
- `ENT-125` Add credential revoke tests. Done for gate-level fail-closed behavior.
- `ENT-127` Add gated connector execution service. Done for credential gate, policy gate, connector invocation, fail-closed missing connector handling, and ledger events.
- `ENT-128` Add Workbench connector execution API. Done for admin-gated execution through `ConnectorExecutionService` with result and gate metadata.
- `ENT-129` Add Workbench connector credential API. Done for admin-gated list, create, revoke, active-only listing, include-revoked listing, and audit events.
- `ENT-130` Add connector secret reference resolver. Done for env/HASHI secret refs, redacted metadata, unsupported scheme failures, and unconfigured vault fail-closed behavior.
- `ENT-131` Add connector factory. Done for GitHub connector construction from credential refs, secret resolution, revoked credential skipping, and unsupported type fail-closed behavior.
- `ENT-132` Add Workbench connector registry refresh. Done for startup refresh, create/revoke refresh, static connector precedence, and fail-soft registry errors.
- `ENT-133` Add default connector policy template. Done for GitHub read allow, GitHub write approval-required, Slack outbound message approval-required, and idempotent install.
- `ENT-134` Add default connector policy install API. Done for admin-gated install, idempotent responses, and audit event emission.
- `ENT-135` Add connector admin UI. Done for Workbench MVP covering credential create/list/revoke, health refresh, registry errors, default policy installation, gated connector dry-run/test-run execution, Slack/GitHub presets, and JSON parameter validation.

**Acceptance:**

- Connector actions produce audit events.
- Revoked credentials fail closed.
- Connector access is scoped by project and role.
- Slack credential creation refreshes the in-process registry from secret references.
- Workbench Slack dry-run execution succeeds only when connector policy allows it.
- Default connector policy requires approval for Slack outbound messages before connector code can run.
- Google Chat credential creation refreshes the in-process registry from secret references.
- Workbench Google Chat dry-run execution succeeds only when connector policy allows it.
- Default connector policy requires approval for Google Chat outbound messages before connector code can run.

---

## 5. Dependency Graph

```text
P0
 ├─ P1A ── P1B ── P5 ── P7
 │    ├─ P2 ── P2.5 ── P3 ── P6
 │    └─ P4 ─────────── P8
 └─ P9-min starts in parallel

P4 + P8 + P9-min -> External Enterprise Beta
P10 starts after beta control plane is stable
```

---

## 6. First Eight Sprint Plan

| Sprint | Main delivery | Exit condition |
|---|---|---|
| S1 | P0 skeleton, profiles, audit/policy contracts | personal unchanged; enterprise fails fast without bootstrap |
| S2 | P1A identity bootstrap and sessions | admin login works; individual user blocked from admin APIs |
| S3 | P1B projects/tokens and P2 channel registry | Done for MVP channel registry and disabled-by-default behavior |
| S4 | P2.5 admin API and P3 policy MVP start | Channel admin API done; P3 policy MVP starts next |
| S5 | P3 policy MVP and P4 ledger start | command/file/shell/backend decisions audited |
| S6 | P4 ledger query/export and P5 task/artifact start | governed task timeline query works |
| S7 | P6 secure execution and P8-min admin console | unsafe write/shell blocked; audit viewer works |
| S8 | P7 routing, P9-min deployment, beta hardening | Journey 1-3 pass in enterprise profile |

---

## 7. Existing Capability Migration Matrix

| Current capability | Current location | Enterprise destination |
|---|---|---|
| Agent definitions | `agents.json` | project-scoped agent registry and assignments |
| Command policy | `flexible_agent_runtime` command policy | `PolicyEvaluator` with personal fallback |
| Tool allowlists | `agents.json` `tools.allowed` | per-agent/per-project policy rules |
| Access scope | config access root resolution | execution boundary and project workspace scope |
| Slash command audit | `slash_command_audit.py` | unified audit adapter, then ledger writer |
| Token audit | token tracker/audit files | model/backend invocation events |
| Audit mode | `audit_mode.py` | high-risk approval and evidence workflow |
| Source policy | `source_policy.py` | channel and remote API policy input |
| Workbench admin token | `workbench_api.py` | sessions, roles, and admin APIs |
| Agent directory | `agent_directory.py` | project-scoped agent lookup |
| HChat/Remote | `remote/` | channel gate, org/project trust, audit correlation |
| Superloop evidence | Superloop store/taskboard | evidence bundles |
| Nagare/Flow | `flow/` and Nagare docs | governed workflow entry with task IDs |
| Protected core checks | protected core tooling | continues to protect core from enterprise churn |

---

## 8. Risks And Controls

| Risk | Severity | Control |
|---|---|---|
| Personal profile regression | High | personal smoke tests in P0 and every phase |
| Enterprise code leaking into protected core | High | keep primitives in `orchestrator/enterprise/`; require protected-core review |
| Scope creep | High | internal alpha and external beta cut lines are binding |
| Audit gaps | High | audit schema contract in P0; deny events are mandatory |
| Channel bypass | High | all transport ingress/egress must use channel gate |
| Policy inconsistency | High | central `PolicyEvaluator`; no new local allow/deny systems |
| SQLite scale concerns | Medium | store interface is pluggable; SQLite is MVP default |
| Workbench UI delaying governance | Medium | ship Admin API before UI |
| Connector risk | Medium | P10 deferred until governance plane is stable |

---

## 9. Ready-To-Implement Checklist

Before implementation starts:

- [ ] Create initial `orchestrator/enterprise/` package.
- [ ] Add tests for `personal` profile regression.
- [ ] Add P0 tickets to the issue tracker or taskboard.
- [ ] Decide first persistent store path for SQLite.
- [ ] Decide config source for `deployment_profile`.
- [ ] Decide first enterprise channel connector candidate for P10, but keep it out of MVP.

First implementation target:

```text
ENT-001 through ENT-007
```

These tickets create the foundation needed for every later phase.
