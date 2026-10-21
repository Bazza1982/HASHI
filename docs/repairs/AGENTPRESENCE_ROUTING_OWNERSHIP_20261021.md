# Agent Presence routing ownership — 2026-10-21

## Decision

Agent Presence chooses exactly one HASHI instance. The selected HASHI instance
chooses exactly one default Agent for Agent Presence ingress. These are separate
settings with separate user entry points:

- AP tray: `HASHI instance` -> `Configure...` or `Clear binding`; AP persists
  only `instance_id + loopback Backend API origin`.
- HASHI command: `/agentpresence default <agent>` or
  `/agentpresence default off`; HASHI persists only the local default Agent.

AP does not store or submit an Agent name. HASHI ignores caller-selected Agent,
Session and channel values, resolves its own configured Agent, verifies that it
is enabled and running, and uses the isolated `agentpresence/default` Session.
Missing or unavailable configuration fails closed; neither side guesses a
route. Existing AP settings migrate by retaining the instance and Backend API
while dropping the old AP-owned Agent field.

## Ownership and scope

- Functional owner: Frontend Connector.
- Engineering layer: Agent Presence application/settings and HASHI3 Functions
  plus instance configuration.
- Protected Core and HASHI4 are outside scope and remain unchanged.

## Validation and adoption

The user requested the split routing model and both settings entries on
2026-10-21. Source validation covers the two menus, persistence, caller-Agent
rejection, instance mismatch, missing settings, disabled/offline Agents and the
existing text/PTT path. Runtime adoption is a separate operational step; this
revision does not inherit earlier restart authority.
