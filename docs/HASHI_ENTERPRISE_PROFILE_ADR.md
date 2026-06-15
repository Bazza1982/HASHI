# ADR: HASHI Enterprise Profiles And Identity Roles

**Status:** accepted.

**Date:** 2026-06-15.

**Related docs:**

- [HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md](HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md)
- [HASHI_ENTERPRISE_AAI_PRD.md](HASHI_ENTERPRISE_AAI_PRD.md)

---

## Decision

HASHI Enterprise will remain one open-source codebase with profile-driven behavior.

Enterprise AAI is not a separate fork of HASHI. It is a governed profile layer on top of the same core bridge, runtime, backend, channel, audit, and orchestration infrastructure.

The canonical deployment profiles are:

| Profile | Meaning |
|---|---|
| `personal` | current HASHI style: one owner-user controls the full system |
| `team` | small group deployment with shared agents, projects, and administrators |
| `enterprise` | organization deployment with identity, policy, audit, admin controls, governed channels, and operational controls |

The term **Individual User** is not a deployment profile. It is an enterprise identity role: a normal human user inside an organization, governed by admins, policies, projects, channel permissions, and audit controls.

---

## Rationale

HASHI already has valuable local-first and power-user behavior. Enterprise development should not remove or weaken that. The current single-owner experience remains valid as the `personal` profile.

At the same time, enterprise adoption requires a different identity model. In an enterprise deployment, an individual user is not automatically the system owner. They may delegate tasks to agents and inspect artifacts, but they should not automatically control global channels, backends, policies, secrets, audit retention, or organization-level settings.

Using both terms clearly avoids a common product ambiguity:

```text
Personal profile = deployment mode for one owner-user.
Individual User = human identity inside a governed team or enterprise deployment.
```

---

## Profile Semantics

### Personal Profile

`personal` preserves the current HASHI assumption:

- the user is the owner;
- the owner is the top administrator;
- local-first controls are available by default;
- optional channels and backends can be enabled directly by the owner;
- governance features may still exist, but the owner controls them.

This profile is suitable for:

- individual developers;
- researchers;
- local personal automation;
- power users evaluating HASHI before team or enterprise adoption.

### Team Profile

`team` introduces shared work without full enterprise overhead:

- multiple users;
- project-level agents;
- team administrators;
- basic role separation;
- channel and backend policy;
- audit visibility for shared work.

This profile is suitable for:

- small teams;
- internal labs;
- trusted workgroups;
- early enterprise pilots.

### Enterprise Profile

`enterprise` enables organization-grade control:

- organization, project, team, user, and service-account models;
- RBAC/ABAC policy;
- SSO-ready identity;
- centralized admin console;
- governed channel registry;
- unified audit ledger;
- approval gates for risky actions;
- deployment, backup, observability, and incident controls.

This profile is suitable for:

- business-critical work;
- regulated operations;
- multi-team deployments;
- enterprise governance and security review.

---

## Enterprise Identity Roles

Enterprise deployments should define roles separately from profiles.

Initial role set:

| Role | Meaning |
|---|---|
| `individual_user` | normal enterprise user who delegates work and reviews artifacts |
| `team_admin` | manages team/project users, agents, and local policies |
| `org_admin` | manages organization-wide identity, projects, channels, and configuration |
| `security_admin` | manages security policy, approvals, audit export, and incident response |
| `system_operator` | manages deployment, runtime health, backups, and upgrades |
| `auditor` | read-only audit, evidence, and compliance reviewer |

An individual user may be powerful in their own project, but they are not automatically the system owner.

---

## Channel Governance Implication

Channels should be controlled by the profile and by administrator policy.

HASHI should support enterprise channels such as Microsoft Teams, Slack, Google Chat, Feishu/Lark, Workbench, and future connectors. It may also support Telegram, WhatsApp, email, voice, and local channels.

The enterprise value is not opening every channel by default. Every channel is also a possible data-leak, impersonation, prompt-injection, and operational-risk surface.

Therefore, in `team` and `enterprise` profiles:

- channels are disabled unless explicitly enabled;
- admins decide which users, teams, projects, and agents may use each channel;
- ingress and egress are audited;
- high-risk channels can require approval or be restricted to low-risk agents;
- channels can be revoked without changing the agent implementation.

In `personal`, the owner can still enable channels directly because the owner is also the top administrator.

---

## Consequences

This decision means:

- HASHI keeps one main codebase;
- enterprise behavior is introduced through profiles, policy, and admin controls;
- existing personal workflows remain valid;
- docs and UI must avoid using `personal` to mean a normal enterprise user;
- future configuration should use explicit profile names such as `personal`, `team`, and `enterprise`;
- product language should distinguish deployment profile from identity role.

The decision does not prevent future packaging differences. A hosted or supported enterprise distribution may exist later, but it should be built from the same core architecture rather than a divergent fork.

## Implementation Defaults (P0)

- `deployment_profile` defaults to `personal` when omitted.
- `team` and `enterprise` are governed profiles and require bootstrap metadata to avoid accidental activation.
- Current one-owner behavior is preserved unless `HASHI_DEPLOYMENT_PROFILE` or `global.deployment_profile` is explicitly set.
- Deployment time overrides are supported: `HASHI_ORGANIZATION_ID` and `HASHI_ENTERPRISE_BOOTSTRAP_COMPLETE`.

### Example config snippet

```json
{
  "global": {
    "deployment_profile": "team",
    "organization_id": "acme-lab",
    "enterprise_bootstrap_complete": true
  },
  "agents": [...]
}
```

### Startup gate

When `team` or `enterprise` is active:

1. `global.organization_id` must exist;
2. `global.enterprise_bootstrap_complete` (or `HASHI_ENTERPRISE_BOOTSTRAP_COMPLETE`) must be true.

P0 implementation rejects startup at config-load time with explicit errors when either requirement is missing.
