# HASHI Enterprise AAI Implementation Roadmap

**Status:** ready-to-implement roadmap.

**Date:** 2026-06-15.

**Related docs:**

- [HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md](HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md)
- [HASHI_ENTERPRISE_AAI_PRD.md](HASHI_ENTERPRISE_AAI_PRD.md)
- [HASHI_ENTERPRISE_PROFILE_ADR.md](HASHI_ENTERPRISE_PROFILE_ADR.md)

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

- SAML/OIDC/SCIM;
- full ABAC policy simulation;
- SIEM/OpenTelemetry;
- WORM storage;
- Kubernetes/HA;
- multiple enterprise connectors;
- DLP/classification.

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
- Workbench login endpoint;
- audit events for login/logout/admin bootstrap;
- personal profile maps current owner behavior to implicit top admin.

**Primary code areas:**

- `orchestrator/enterprise/identity.py`;
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

**Acceptance:**

- `org_admin` can log in.
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

**Acceptance:**

- `org_admin` can configure channels through API.
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
- stable ledger event schema version field;
- policy and channel deny/approval audit paths now dual-write into the unified ledger;
- Workbench enterprise audit APIs:
  - `GET /api/enterprise/audit` for admin-gated ledger query;
  - `GET /api/enterprise/audit/export` for admin-gated NDJSON export;
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

- slash command JSONL can now be ingested into the ledger, but live slash command dual-write is still pending;
- token audit JSONL can now be ingested into the ledger, but live token audit dual-write is still pending;
- Remote audit JSONL can now be ingested into the ledger, but live Remote/HChat dual-write is still pending;
- browser action JSONL can now be ingested into the ledger, but live browser/tool dual-write is still pending;
- HASHI-controlled tool execution now writes `tool_action_audit.jsonl`, but direct live ledger dual-write is still pending;
- auditor read-only role semantics are not yet separated from broader admin access;
- generic shell/file tool execution now has a canonical JSONL event source and ingest adapter;
- retention and SIEM mapping remain future P4 work.

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

**Acceptance:**

- A governed task produces a queryable timeline.
- Policy deny and channel deny events are never dropped.
- JSONL export contains stable schema version.

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
- `ToolRegistry.execute()` applies an enterprise browser automation gate before all `browser_*` tools;
- governed browser automation defaults closed and requires explicit `enterprise_browser_enabled` or `browser.enterprise_enabled`.
- `complete_task_with_artifact_verification()` provides an enterprise completion path that marks a task `completed` only when promised artifacts are present, otherwise marks it `failed` with a clear missing-artifact reason.

Residual P6 limitations:

- workspace boundary is wired into HASHI-controlled file tools;
- shell execution has an explicit governed enable gate; command allow/deny pattern policy still uses the existing bash `blocked_patterns`;
- network egress has a host allowlist stub for HASHI-controlled network tools;
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

Residual P7 limitations:

- Workbench approval UI is not yet implemented;
- approved requests are not yet consumed to resume blocked work automatically;
- project-aware routing is implemented only for explicit bridge `project_id`; broader channel/project resolution remains pending.
- failed-task escalation writes audit events only; notification delivery and retry routing remain pending.

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
- `ENT-094` Add agent capability registry.

**Acceptance:**

- Messages from Project A do not route to Project B agents.
- Approval-required actions do not execute until approved.
- Approval decisions write audit events.

---

### P8 - Workbench Admin Console

**Goal:** provide a human admin surface for enterprise controls.

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
- `ENT-104` Add policy viewer/editor.
- `ENT-105` Add audit timeline and export screen.
- `ENT-106` Add enterprise health screen.

**Acceptance:**

- `org_admin` can enable a channel and assign it to a project.
- `auditor` can export audit but cannot mutate settings.
- `individual_user` cannot see admin navigation.

---

### P9 - Deployment, Backup, Migration, And Operations

**Goal:** make enterprise mode installable, upgradeable, and recoverable.

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
- Dockerfile or package metadata;
- `orchestrator/enterprise/migrations/`;
- `hashi.py enterprise backup|restore|migrate`;
- `orchestrator/workbench_api.py` health endpoints.

**Tickets:**

- `ENT-110` Add enterprise Docker Compose skeleton.
- `ENT-111` Add enterprise volume layout.
- `ENT-112` Add migration runner command.
- `ENT-113` Add backup/restore command.
- `ENT-114` Add enterprise health checks.
- `ENT-115` Add upgrade and rollback documentation.

**Acceptance:**

- Fresh install can bootstrap enterprise admin.
- Backup/restore round trip preserves users, channels, policies, and audit.
- Migrations are versioned and repeatable.

---

### P10 - Enterprise Connectors

**Goal:** add governed integrations after the control plane is stable.

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

- `ENT-120` Define connector interface.
- `ENT-121` Add scoped credential store abstraction.
- `ENT-122` Add first enterprise channel connector.
- `ENT-123` Add GitHub connector with audit.
- `ENT-124` Add connector health checks.
- `ENT-125` Add credential revoke tests.

**Acceptance:**

- Connector actions produce audit events.
- Revoked credentials fail closed.
- Connector access is scoped by project and role.

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
