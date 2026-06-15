# HASHI Enterprise AAI PRD And Development Plan

**Status:** product requirements draft.

**Date:** 2026-06-15.

**Related concept doc:** [HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md](HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md)

---

## 1. Product Goal

Create an open-source enterprise-grade version of HASHI focused on **Agent as Interface (AAI)**: governed human-AI work orchestration where users delegate work to accountable agents and administrators control identity, permissions, routing, execution, audit, deployment, and integrations.

The goal is not to turn HASHI into a generic chatbot platform. The goal is to turn HASHI into enterprise infrastructure for safe, auditable, multi-channel, multi-backend AI work.

---

## 2. Target Users

### Primary users

- executives and leaders delegating work;
- knowledge workers using agents as digital team members;
- technical teams using agents for code, research, operations, and automation;
- administrators managing access, policies, audit, and deployment.

### Secondary users

- security and compliance reviewers;
- internal platform teams;
- AI governance teams;
- open-source contributors building integrations and enterprise modules.

---

## 3. Current Baseline

HASHI already has strong foundations:

- multi-agent runtime;
- multi-backend support;
- CLI and API backends;
- Telegram, WhatsApp, Workbench, API Gateway;
- HChat and Remote;
- Nagare workflows;
- Superloop operational contracts;
- wrapper and audit modes;
- token audit and slash-command audit;
- browser routes and file transfer;
- local-first operation.

However, current HASHI is still closer to a local power-user and research-grade system than an enterprise-grade platform.

Major current gaps:

- no full organization/user/team model;
- no enterprise login/SSO;
- no unified RBAC/ABAC policy layer;
- no centralized admin console for permissions and audit;
- no unified immutable audit ledger;
- no enterprise secret management;
- limited deployment and upgrade story;
- limited multi-tenant isolation;
- limited compliance and retention controls;
- partial observability and fragmented logs;
- execution safety still depends too much on adapter/tool-specific behavior.

---

## 4. Product Requirements

### 4.0 Deployment Profiles And Enterprise Identity

Enterprise AAI should remain one HASHI codebase with profile-driven behavior, not a separate fork.

Deployment profiles:

| Profile | Meaning | Control model |
|---|---|---|
| `personal` | current HASHI style: one owner-user controls the full system | user = owner = top admin |
| `team` | small group deployment with shared projects and agents | admins and members are separated |
| `enterprise` | organization deployment with identity, policy, audit, governed channels, and operations controls | formal RBAC/ABAC, admin console, audit, approval, and deployment controls |

The term **Individual User** is reserved for enterprise identity. It means a normal human user inside a team or enterprise deployment. An individual user may delegate work to agents and inspect artifacts, but does not automatically control global policies, channels, backends, secrets, audit retention, or organization settings.

This distinction is important:

```text
Personal profile = deployment mode for one owner-user.
Individual User = governed human identity inside a team or enterprise deployment.
```

See [HASHI_ENTERPRISE_PROFILE_ADR.md](HASHI_ENTERPRISE_PROFILE_ADR.md) for the accepted decision.

### 4.0.1 Current HASHI To Enterprise Mapping

Enterprise development should not restart from zero. Existing HASHI capabilities should be promoted into governed enterprise primitives.

| Current capability | Enterprise primitive | Required evolution |
|---|---|---|
| `agents.json` and per-agent workspace config | agent registry and assignment model | move from local config file semantics to admin-managed project/role assignments |
| Telegram, WhatsApp, Workbench | channel gateway layer | add admin enablement, policy checks, ingress/egress audit, and channel-specific risk controls |
| HChat and Remote | cross-agent / cross-instance routing | add organization/project trust, route policy, and audit correlation IDs |
| token audit and slash command audit | unified audit ledger | normalize event schema, correlate by request/task, add export and retention |
| Nagare and Superloop | governed workflow orchestration | add task ownership, approval gates, evidence bundles, and admin visibility |
| tool registry and backend adapters | execution connectors | add centralized policy evaluation, tool risk levels, and verification loops |
| wrapper/audit mode | review and supervision layer | integrate with enterprise policy, approval, and evidence workflows |

This mapping should guide implementation tickets. The goal is to harden and unify existing strengths, not replace them wholesale.

### 4.1 Identity And Organization

**Goal:** HASHI must support real users and organizations, not only local single-user operation.

Requirements:

- organization entity;
- users;
- teams/groups;
- roles;
- explicit distinction between `personal` profile owner and enterprise `individual_user`;
- service accounts;
- invitation and onboarding flow;
- local dev auth mode;
- enterprise auth provider abstraction;
- OIDC/SAML-ready design;
- API tokens with scopes and expiration;
- audit record for login, token creation, token revocation, and admin changes.

Minimum v1:

- local username/password or single-admin bootstrap;
- user table;
- role table;
- admin/individual-user distinction;
- scoped API tokens.

Future:

- OIDC;
- SAML;
- SCIM user provisioning;
- organization-level policy inheritance.

---

### 4.2 RBAC And Policy Control Plane

**Goal:** every meaningful action must be governed by a consistent policy model.

Policy objects:

- user;
- team;
- project;
- workspace;
- agent;
- backend;
- tool;
- command;
- channel;
- data source;
- integration;
- risk level.

Required decisions:

- can user message this agent?
- can this agent use this backend?
- can this agent access this workspace?
- can this agent write files?
- can this agent run shell commands?
- can this task use browser automation?
- does this action need approval?
- does this output require review?

Minimum v1:

- RBAC roles:
  - owner;
  - admin;
  - operator;
  - contributor;
  - viewer;
  - auditor.
- per-project agent access;
- per-agent tool and command policy;
- approval-required policy for risky actions;
- policy audit events.

---

### 4.3 Unified Audit Ledger

**Goal:** replace fragmented logs with a consistent enterprise audit stream.

Audit event categories:

- identity events;
- login/session/token events;
- command events;
- task lifecycle events;
- tool events;
- file read/write events;
- backend selection and model events;
- approval events;
- policy deny events;
- artifact creation events;
- remote/HChat events;
- admin configuration changes.

Required fields:

- event_id;
- timestamp;
- organization_id;
- project_id;
- actor_user_id;
- actor_agent_id;
- source_channel;
- request_id;
- task_id;
- action_type;
- status;
- target_resource;
- policy_decision;
- tool_name;
- file_path;
- backend;
- model;
- error;
- evidence_refs.

Minimum v1:

- append-only JSONL or SQLite ledger;
- stable schema;
- per-project query;
- export to JSONL;
- audit dashboard in Workbench;
- retention setting.

Future:

- tamper-evident hash chain;
- SIEM export;
- OpenTelemetry integration;
- WORM/object storage backend.

Enterprise v1 should still design the schema around future tamper evidence. Even if the first store is SQLite or JSONL, the event model should include stable event IDs, parent IDs, task IDs, actor IDs, and correlation IDs so it can later be promoted into a hash-chained or SIEM-backed ledger.

---

### 4.4 Project, Workspace, And Artifact Model

**Goal:** enterprise users need work containers, not only agent workspaces.

Entities:

- organization;
- project;
- workspace;
- agent assignment;
- task;
- artifact;
- evidence bundle.

Minimum v1:

- project registry;
- workspace path registry;
- artifact records for files, reports, commits, and exports;
- task-to-artifact linking;
- evidence bundle per completed task.

Future:

- artifact versioning;
- retention rules;
- document classification;
- DLP scanning;
- external storage connectors.

---

### 4.5 Secure Execution Layer

**Goal:** agents must be able to do real work without becoming uncontrolled system users.

Requirements:

- explicit execution scope;
- filesystem boundaries;
- shell command policy;
- network egress policy;
- browser automation policy;
- per-task sandbox profile;
- approval gates for risky actions;
- completion verification before success;
- failure reporting when deliverables are missing.

Minimum v1:

- policy checks before tool execution;
- workspace-scoped file operations;
- shell allow/deny rules;
- required verification step for file-producing tasks;
- action event capture into audit ledger.

Future:

- containerized runners;
- per-task ephemeral workspaces;
- network sandboxing;
- secret injection with short-lived credentials;
- rollback and artifact attestation.

---

### 4.6 Routing And Work Orchestration

**Goal:** HASHI must route work to the right agent, backend, workflow, or human reviewer.

Routing inputs:

- user role;
- project;
- channel;
- task type;
- agent capability;
- backend availability;
- policy;
- risk level;
- queue state;
- SLA/priority.

Minimum v1:

- project-aware agent routing;
- admin-configurable agent assignments;
- task queue with status;
- human approval queue;
- failed-task retry and escalation.

Future:

- policy-based auto-routing;
- skill/capability registry;
- enterprise workflow integrations;
- workload balancing;
- multi-instance routing across HASHI nodes.

---

### 4.7 Admin Console

**Goal:** administrators need one place to govern HASHI.

Minimum v1 panels:

- users and roles;
- agents;
- projects;
- backends and models;
- tool policies;
- command policies;
- approvals;
- audit ledger;
- system health;
- task queue;
- secrets/integration status.

Future:

- compliance reports;
- risk dashboards;
- cost dashboards;
- policy simulation;
- incident review.

---

### 4.8 Enterprise Deployment

**Goal:** HASHI must be installable, upgradeable, observable, and recoverable in real environments.

Minimum v1:

- Docker image;
- docker-compose deployment;
- persistent volume layout;
- environment variable configuration;
- health endpoints;
- backup and restore guide;
- migration scripts;
- release checklist;
- production config template.

Future:

- Helm chart;
- Kubernetes deployment;
- HA control plane;
- external database support;
- external object storage;
- OpenTelemetry metrics/traces/logs;
- enterprise installer.

Deployment should run in parallel with identity, policy, and audit work. Enterprise evaluators often ask "can we install, back up, upgrade, and observe it?" before they inspect advanced agent capability.

---

### 4.9 Channel Governance And Enterprise Connectors

**Goal:** HASHI should connect to the channels organizations already use, but every channel must be governed.

Supported-channel direction:

- Workbench should be the default enterprise control and inspection surface.
- Microsoft Teams, Slack, Google Chat, and Feishu/Lark are high-priority enterprise channels.
- Telegram, WhatsApp, and voice remain useful but should be optional, policy-gated, and not presented as the default enterprise path.

Channel control requirements:

- channels disabled by default unless enabled by an administrator;
- per-organization, per-project, per-team, and per-agent channel policy;
- ingress audit for every user message;
- egress audit for every agent message or artifact share;
- channel-specific data loss controls;
- impersonation and identity binding rules;
- emergency channel disable/kill switch;
- clear separation between consumer/local channels and enterprise-approved channels.

Minimum v1:

- channel registry;
- Workbench as first admin-controlled channel;
- at least one enterprise chat connector design target;
- channel policy decisions in audit ledger;
- admin UI to enable/disable channels per project/agent.

Future:

- Microsoft Teams connector;
- Slack connector;
- Google Chat connector;
- Feishu/Lark connector;
- email connector;
- channel DLP rules;
- channel-specific retention and export.

---

### 4.10 Enterprise System Integrations

**Goal:** AAI must bridge humans and real enterprise systems.

Minimum v1 candidates:

- GitHub;
- Jira or Linear;
- Google Drive or SharePoint;
- email/SMTP;
- local filesystem and SMB/shared drive.

Integration requirements:

- scoped credentials;
- audit events;
- permission mapping;
- connector health;
- dry-run mode where possible;
- artifact linking.

The first integration wave should be intentionally narrow. A credible Enterprise v1 should ship one or two well-governed integrations rather than many shallow connectors.

---

### 4.11 Threat Model And Data Governance

**Goal:** Enterprise AAI must explicitly address security reviewers, not only product users.

Threat model areas:

- prompt injection through documents, browser content, chat messages, and remote agents;
- channel impersonation and account takeover;
- data exfiltration through chat channels, tools, browser, and integrations;
- SSRF and uncontrolled network egress;
- workspace escape and unauthorized file access;
- overbroad backend/model use that sends data outside approved regions;
- agent impersonation and sub-agent delegation ambiguity;
- secret leakage through prompts, logs, artifacts, or connector output.

Minimum v1:

- documented STRIDE-style threat model summary;
- data classification labels for tasks/artifacts;
- retention settings for audit and artifacts;
- PII-sensitive logging redaction rules;
- backend/model allowlist by project;
- emergency agent/channel revocation;
- security review checklist for new connectors.

Future:

- DLP scanning;
- regional data residency enforcement;
- customer-managed keys;
- SIEM integration;
- incident response workflows.

---

## 5. AAI User Journeys

### Journey 1: Leader delegates work

1. Leader messages an agent: "Prepare the client onboarding checklist for Project A."
2. HASHI checks project access and task policy.
3. Agent gathers context, creates a document, and links evidence.
4. HASHI records file writes, tool usage, and completion checks.
5. Leader reviews the document and asks for changes.
6. Final artifact is approved and exported.

### Journey 2: Admin governs agent capability

1. Admin opens control console.
2. Admin restricts one agent from shell execution.
3. A user later requests a shell-based task.
4. Policy denies or routes to approval.
5. Audit ledger records the policy decision.

### Journey 3: Auditor investigates work

1. Auditor searches by project and task.
2. Audit ledger shows user prompt, agent, tools, files, backend, approvals, and artifacts.
3. Auditor exports an evidence bundle.

---

## 6. Development Plan

### Phase 0: Product Definition

Deliverables:

- AAI value proposition document;
- enterprise stocktake;
- Enterprise v1 boundary;
- architecture decision record for control plane.

Acceptance:

- enterprise strategy is documented;
- non-goals are explicit;
- roadmap is approved.

### Phase 1: Identity And Admin Foundation

Deliverables:

- user/org/project data model;
- local admin bootstrap;
- role model;
- API token model;
- admin console skeleton.

Acceptance:

- at least two users with different roles can log in;
- admin can assign a project role;
- audit records admin changes.

### Phase 2: Policy Control Plane And Channel Governance

Deliverables:

- centralized policy evaluator;
- agent/tool/command/backend policy model;
- channel registry and channel policy model;
- policy decision audit events;
- approval-required actions.

Acceptance:

- blocked actions do not execute;
- policy decisions are visible in audit;
- tests cover allow, deny, and approval-required paths;
- disabled channels cannot deliver messages to agents.

### Phase 3: Unified Audit Ledger

Deliverables:

- audit event schema;
- append-only store;
- event writers for channels, commands, tasks, tools, files, approvals, and admin changes;
- Workbench audit viewer.

Acceptance:

- a completed task has a queryable audit timeline;
- export works;
- schema tests protect required fields.

### Phase 4: Secure Execution, Evidence, And Deployability

Deliverables:

- execution scope enforcement;
- file/task artifact registry;
- verification checklist for file-producing tasks;
- evidence bundle generation;
- Docker Compose deployment profile;
- backup/restore procedure;
- health endpoints.

Acceptance:

- task cannot write outside allowed workspace;
- missing promised deliverables cause failure;
- evidence bundle links output files and tool events;
- fresh enterprise deployment starts from documented steps.

### Phase 5: Operations Hardening

Deliverables:

- production config template;
- migration runner;
- operational metrics;
- upgrade rollback guide;
- connector health reporting.

Acceptance:

- upgrade path preserves data;
- health endpoint reports core services.

### Phase 6: Enterprise Integrations

Deliverables:

- GitHub connector;
- first governed enterprise chat connector from the channel governance roadmap;
- Jira/Linear connector;
- Drive/SharePoint connector;
- integration audit events.

Acceptance:

- connector credentials are scoped;
- connector actions appear in audit;
- artifacts link back to source systems.

---

## 7. Non-Goals For Enterprise v1

Enterprise v1 should not attempt:

- full SaaS billing;
- multi-region high availability;
- every enterprise connector;
- full SOC 2 automation;
- physical AI integration;
- complex marketplace packaging;
- unrestricted autonomous agent execution.
- all chat channels enabled by default.

These are future phases after the control plane is reliable.

---

## 8. Enterprise v1 MVP Cut Line

Enterprise v1 beta is credible when the following are in scope:

| Area | In v1 beta | Deferred |
|---|---|---|
| Identity | local admin, users, roles, API tokens | SAML/SCIM full enterprise provisioning |
| Projects | project registry, workspace assignment | advanced portfolio management |
| Policy | RBAC, tool/command/backend/channel policy | full ABAC policy language |
| Audit | unified ledger, export, Workbench viewer | tamper-evident WORM/SIEM production adapter |
| Channels | Workbench plus one enterprise chat connector target | every chat platform |
| Execution | workspace-scoped tools and verification checklist | full container sandbox for every task |
| Deployment | Docker Compose, backup/restore, health checks | HA Kubernetes and multi-region |
| Integrations | GitHub plus one work-management/document connector target | broad marketplace |

Out of scope for v1 beta:

- billing;
- multi-region HA;
- physical AI;
- broad connector marketplace;
- unsupervised high-risk automation;
- default consumer-channel exposure.

---

## 9. Success Metrics

Product metrics:

- time from install to first governed task;
- number of successful tasks with evidence bundles;
- percentage of tool/file actions captured in audit;
- percentage of denied actions with clear policy reason;
- task completion verification rate;
- admin configuration coverage.

Reliability metrics:

- task success rate;
- failed-task explicit error rate;
- audit write success rate;
- queue latency;
- backend failure recovery rate.

Security metrics:

- unauthorized action execution count should be zero;
- workspace escape count should be zero;
- missing audit event rate should trend to zero;
- stale credential count;
- unreviewed high-risk action count.

---

## 10. First Engineering Tickets

1. Create enterprise data model for organizations, users, projects, roles, and API tokens.
2. Add local admin login and session management for Workbench.
3. Build centralized policy evaluator.
4. Create unified audit event schema and store.
5. Migrate slash command audit into the unified audit writer.
6. Add channel registry and disabled-by-default channel policy.
7. Add task and artifact registry.
8. Add execution verification requirement for file-producing agent tasks.
9. Add Workbench admin console skeleton.
10. Add Docker Compose deployment profile.
11. Add backup/restore and migration commands.

---

## 11. Open Questions

1. Should enterprise mode be a separate runtime mode or a configuration profile? **Decision:** configuration profile in the same codebase; see [HASHI_ENTERPRISE_PROFILE_ADR.md](HASHI_ENTERPRISE_PROFILE_ADR.md).
2. Should the first persistent store be SQLite, Postgres, or a pluggable interface with SQLite default?
3. Should Workbench become the primary admin console or should admin UI be separated?
4. What is the minimum identity model for open-source self-hosted deployment?
5. How should HASHI handle organizations that want local-only operation with no SSO?
6. Which integration should be first: GitHub, Slack/Teams, Jira, or Drive/SharePoint?
7. Should agent memory be project-scoped, user-scoped, or both?
8. What actions require mandatory human approval in Enterprise v1?
9. Which enterprise chat channel should be the first governed connector: Microsoft Teams, Slack, Google Chat, or Feishu/Lark?
