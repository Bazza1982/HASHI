# Agent Presence single-instance ingress binding — 2026-10-21

> Historical implementation/adoption record. Its combined AP-owned
> `instance + Agent` routing decision is superseded by
> [Agent Presence routing ownership](AGENTPRESENCE_ROUTING_OWNERSHIP_20261021.md).

## Decision

Agent Presence is a bidirectional local Frontend Connector. Its egress mirror
remains owner-scoped and fail-open. Its text/PTT ingress is fail-closed and may
target exactly one explicit `HASHI instance + Backend API + default Agent`
binding stored by AP.

No binding means no send. AP never derives a target from the character pack,
the focused window, an Agent list, or a previous conversation. Button-down
freezes the route; button-up submits one in-memory PCM/WAV recording through the
existing HASHI voice/STT/Safe Voice path. Text uses the same target and isolated
`agentpresence/default` Session. Slash-prefixed text follows the same registered
Frontend Connector command adapter rather than falling outside that contract. A
write with an ambiguous outcome is not retried.

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

- Implementation commits: HASHI3 `e35ddff9`, AP `5cc2bee`; the live command
  adapter correction is HASHI3 `0f6a87ab`.
- Source validation: the initial 57 relevant HASHI3 tests passed. After live
  validation exposed the missing AP command adapter, the regression suite was
  expanded and 79 relevant tests passed. Agent Presence release build, HASHI
  ingress console tests, call-animation state tests, Python compilation,
  JSON/i18n parsing, diff checks, and the Protected Core guard also passed.
- AP package validation: publish, package self-check, and language audit passed.
  Running API reported version `1.1.0`, `ready=true`, and the `audio_capture`,
  `ptt_ingress`, and `hashi_outbound` capabilities.
- Running adoption: AP was restarted and retained `agent1@HASHI3` through a
  second restart. HASHI3 `/reboot max` adopted generation
  `sha256:483d7b47931c697addfecb52b96797316ee41981cdfc97b3a96fd4eed887115e`
  with 13/13 Agents ready and zero failures. An authenticated AP command reached
  `agent1` with HTTP 200; a wrong instance returned 409 and a wrong token 401.
- User microphone/UI acceptance: not automated; no ambient audio is captured
  for this engineering validation.
