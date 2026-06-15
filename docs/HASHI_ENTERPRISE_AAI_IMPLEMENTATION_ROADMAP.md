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

- `ENT-050` Implement `PolicyEvaluator`.
- `ENT-051` Add role and project context to policy input.
- `ENT-052` Wire channel checks to evaluator.
- `ENT-053` Wire slash command checks to evaluator.
- `ENT-054` Wire backend switch checks to evaluator.
- `ENT-055` Wire file read/write and shell checks to evaluator.
- `ENT-056` Add approval-required stub.
- `ENT-057` Add policy deny audit events.

**Acceptance:**

- `individual_user` cannot run admin-only commands.
- Unauthorized shell/file write is blocked before execution.
- Policy decisions are testable without running the full runtime.
- Personal profile defaults to owner-controlled allow behavior.

---

### P4 - Unified Audit Ledger

**Goal:** promote audit stubs and fragmented logs into a queryable enterprise ledger.

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

- `ENT-060` Implement append-only ledger store.
- `ENT-061` Add query API and pagination.
- `ENT-062` Add JSONL export.
- `ENT-063` Add slash audit adapter.
- `ENT-064` Add token/model invocation adapter.
- `ENT-065` Add HChat/Remote adapter.
- `ENT-066` Add tool/file event adapters.
- `ENT-067` Add audit schema contract tests.

**Acceptance:**

- A governed task produces a queryable timeline.
- Policy deny and channel deny events are never dropped.
- JSONL export contains stable schema version.

---

### P5 - Task, Artifact, And Evidence Model

**Goal:** make delegated work inspectable as tasks with artifacts and evidence bundles.

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

- `ENT-070` Add task schema and state machine.
- `ENT-071` Add artifact registry.
- `ENT-072` Register artifacts from governed file writes.
- `ENT-073` Build evidence bundle from audit range and artifacts.
- `ENT-074` Link Superloop closeout to evidence bundles.

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

**Primary code areas:**

- `orchestrator/enterprise/execution.py`;
- `orchestrator/enterprise/verification.py`;
- `orchestrator/config.py`;
- tool dispatch code;
- browser mode code.

**Tickets:**

- `ENT-080` Add execution scope resolver.
- `ENT-081` Add path traversal block tests.
- `ENT-082` Add shell command policy checks.
- `ENT-083` Add network egress allowlist stub.
- `ENT-084` Add browser automation policy flag.
- `ENT-085` Add completion verification hook.

**Acceptance:**

- Workspace escape attempts are blocked.
- Unapproved shell/file actions do not execute.
- A task that promises artifacts but does not produce them fails clearly.

---

### P7 - Project-Aware Routing And Approvals

**Goal:** route work by project, role, and approval state.

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

- `ENT-090` Add project-aware inbound routing.
- `ENT-091` Add approval queue APIs.
- `ENT-092` Add admin approve/deny action.
- `ENT-093` Add failed-task escalation events.
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
