# HASHI Enterprise AAI Readiness Review

**Date:** 2026-06-16

**Status:** Enterprise MVP implementation is ready for review. The broader future roadmap is not complete.

Related documents:

- [HASHI_ENTERPRISE_AAI_IMPLEMENTATION_ROADMAP.md](HASHI_ENTERPRISE_AAI_IMPLEMENTATION_ROADMAP.md)
- [HASHI_ENTERPRISE_AAI_PRD.md](HASHI_ENTERPRISE_AAI_PRD.md)
- [HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md](HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md)
- [HASHI_ENTERPRISE_PROFILE_ADR.md](HASHI_ENTERPRISE_PROFILE_ADR.md)

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
- Docker/ops skeleton with backup, restore, migration, and health checks;
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
- Workbench can discover configured local/OIDC login providers without exposing client secrets.
- OIDC start returns an authorization URL while keeping the PKCE `code_verifier` server-side; callback validation consumes state and prepares a token exchange request without writing authorization codes, PKCE verifiers, or client secrets into browser responses or audit.
- OIDC ID token verification enforces compact JWT shape, `alg=RS256`, matching `kid`, RSA JWKS signing keys, signature validity, and issuer/audience/expiry/not-before/issued-at/subject/nonce claims.
- OIDC verified identities can create or reuse active enterprise users, issue sessions, and assign only `individual_user` default project membership unless an administrator changes policy.
- OIDC token endpoint and JWKS network calls are isolated behind injectable services; public token response payloads expose token presence metadata only, not token values.
- OIDC callback supports an explicitly enabled full login path from authorization code to session, while preserving default prepared mode for deployments that have not enabled live SSO completion.

### Control Plane

- Channels are modeled as administratively controlled capabilities.
- Commands, channels, tools, execution scopes, backends, and connectors can be routed through central policy decisions.
- Approval-required flows create approval records instead of silently executing high-risk actions.

### Auditability

- Unified ledger records structured events for identity, admin actions, channels, policy decisions, commands, connectors, tasks, artifacts, and adapted legacy streams.
- New ledger events include tamper-evident hash-chain fields and can be verified for in-database modification, deletion, or reordering.
- Audit export and Workbench timeline views exist for review and handoff.
- Audit export supports default ledger NDJSON plus SIEM/ECS-style and OpenTelemetry log-style NDJSON mappings.
- Sensitive connector parameters are redacted in connector audit records.

### Work And Evidence

- Tasks, artifacts, evidence bundles, verification checks, and escalation support enterprise-style review of deliverables.
- File-producing work can be checked against expected deliverables before being marked complete.

### Enterprise Connectors

- Connector interface, registry, credential store, secret resolver, execution gate, execution service, health API, and factory exist.
- GitHub connector supports repository metadata and issue creation with dry-run behavior.
- Slack incoming webhook connector supports governed `message.send` with dry-run behavior.
- Google Chat incoming webhook connector supports governed `message.send` with dry-run behavior.
- Default connector policy allows GitHub reads, requires approval for GitHub writes, and requires approval for Slack and Google Chat outbound messages.
- Workbench connector execution API rejects webhook `message.send` actions without non-empty `text` before execution.
- Workbench Enterprise console supports connector credentials, health, policy defaults, and dry-run/test-run execution.

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

- complete SAML/SCIM login flows;
- full ABAC simulator and policy preview tooling;
- WORM audit storage or external ledger anchoring;
- live SIEM push or OTLP network export;
- Kubernetes/HA deployment;
- Vault/Kubernetes secret resolver implementations;
- Slack OAuth/Bot API, channel discovery, and user mapping;
- Microsoft Teams and Feishu connectors;
- Google Chat OAuth, space discovery, and user mapping;
- GitHub PR create/merge actions;
- DLP/classification and data residency controls;
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
