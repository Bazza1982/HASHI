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

### 4.1 Identity And Organization

**Goal:** HASHI must support real users and organizations, not only local single-user operation.

Requirements:

- organization entity;
- users;
- teams/groups;
- roles;
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
- admin/user distinction;
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

---

### 4.9 Enterprise Integrations

**Goal:** AAI must bridge humans and real enterprise systems.

Minimum v1 candidates:

- GitHub;
- Slack or Teams;
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

### Phase 2: Policy Control Plane

Deliverables:

- centralized policy evaluator;
- agent/tool/command/backend policy model;
- policy decision audit events;
- approval-required actions.

Acceptance:

- blocked actions do not execute;
- policy decisions are visible in audit;
- tests cover allow, deny, and approval-required paths.

### Phase 3: Unified Audit Ledger

Deliverables:

- audit event schema;
- append-only store;
- event writers for commands, tasks, tools, files, approvals, and admin changes;
- Workbench audit viewer.

Acceptance:

- a completed task has a queryable audit timeline;
- export works;
- schema tests protect required fields.

### Phase 4: Secure Execution And Evidence

Deliverables:

- execution scope enforcement;
- file/task artifact registry;
- verification checklist for file-producing tasks;
- evidence bundle generation.

Acceptance:

- task cannot write outside allowed workspace;
- missing promised deliverables cause failure;
- evidence bundle links output files and tool events.

### Phase 5: Deployment And Operations

Deliverables:

- Docker image;
- compose deployment;
- production config template;
- health checks;
- backup/restore;
- migration runner.

Acceptance:

- fresh enterprise deployment can be started from docs;
- upgrade path preserves data;
- health endpoint reports core services.

### Phase 6: Enterprise Integrations

Deliverables:

- GitHub connector;
- Slack or Teams connector;
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

These are future phases after the control plane is reliable.

---

## 8. Success Metrics

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

## 9. First Engineering Tickets

1. Create enterprise data model for organizations, users, projects, roles, and API tokens.
2. Add local admin login and session management for Workbench.
3. Build centralized policy evaluator.
4. Create unified audit event schema and store.
5. Migrate slash command audit into the unified audit writer.
6. Add task and artifact registry.
7. Add execution verification requirement for file-producing agent tasks.
8. Add Workbench admin console skeleton.
9. Add Docker Compose deployment profile.
10. Add backup/restore and migration commands.

---

## 10. Open Questions

1. Should enterprise mode be a separate runtime mode or a configuration profile?
2. Should the first persistent store be SQLite, Postgres, or a pluggable interface with SQLite default?
3. Should Workbench become the primary admin console or should admin UI be separated?
4. What is the minimum identity model for open-source self-hosted deployment?
5. How should HASHI handle organizations that want local-only operation with no SSO?
6. Which integration should be first: GitHub, Slack/Teams, Jira, or Drive/SharePoint?
7. Should agent memory be project-scoped, user-scoped, or both?
8. What actions require mandatory human approval in Enterprise v1?
