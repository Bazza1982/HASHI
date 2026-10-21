# Agent Presence single-instance ingress binding — 2026-10-21

## Decision

Agent Presence is a bidirectional local Frontend Connector. Its egress mirror
remains owner-scoped and fail-open. Its text/PTT ingress is fail-closed and may
target exactly one explicit `HASHI instance + Backend API + default Agent`
binding stored by AP.

No binding means no send. AP never derives a target from the character pack,
the focused window, an Agent list, or a previous conversation. Button-down
freezes the route; button-up submits one in-memory PCM/WAV recording through the
existing HASHI voice/STT/Safe Voice path. Text uses the same target and isolated
`agentpresence/default` Session. A write with an ambiguous outcome is not
retried.

HASHI admits the reserved source only when `source=surface=agentpresence`, the
local AP token matches, and the requested instance is the current instance.
Caller-selected Session and channel values are discarded. The typed current
message context records `local_shared_token`; this authentication does not
grant private authorization or broader authority.

## Ownership and scope

- Functional owner: Frontend Connector.
- Engineering layer: HASHI3 Functions and instance configuration; Agent
  Presence application and local settings.
- Protected Core and HASHI4 are outside scope and remain unchanged.

## Authorization

The user approved implementation and authorized restarting HASHI3 and Agent
Presence for adoption on 2026-10-21. This is not Core migration authorization.

## Validation record

- Source validation: 57 relevant HASHI3 tests passed; Agent Presence release
  build, HASHI ingress console tests, call-animation state tests, Python
  compilation, JSON/i18n parsing, diff checks, and the Protected Core guard
  passed before the implementation commits.
- AP package validation: source release build passed; publish and running API
  checks remain pending at the implementation commit.
- Running HASHI3/AP adoption: pending; must be recorded separately after the
  committed generations are loaded.
- User microphone/UI acceptance: not automated; no ambient audio is captured
  for this engineering validation.
