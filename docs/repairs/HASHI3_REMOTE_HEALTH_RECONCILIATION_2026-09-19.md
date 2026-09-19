# HASHI3 Remote Health Reconciliation — 2026-09-19

## Scope and approval

- Instance: `HASHI3`.
- Checkout and branch: the native Windows HASHI3 checkout on `main`.
- Approval: the user approved a direct HASHI3 fix on 2026-09-19 after the
  stale Workbench health warning was diagnosed as HASHI-owned state.
- Operational boundary: do not restart or reboot HASHI. Allow automatic
  adoption if available; otherwise leave adoption for the user.
- Core boundary: no Core major-version migration was authorized or required.

## Implementation

- Periodic Remote trust revalidation retains the last accepted peer state
  while the replacement handshake is in flight. A definitive failed result
  still replaces that state.
- Remote lifecycle exposes a read-only current-state inspection that never
  starts, stops, or replaces the sidecar.
- Backend API health uses that inspection only while a Remote startup issue is
  latched. Recovery removes the matching Remote issue and preserves unrelated
  Agent, connector, and startup issues.
- The Remote protocol decision and Agent FYI carry the updated behavior.

## Verification and adoption

- Focused red evidence: the accepted peer was temporarily projected as
  `handshake_in_progress`; no recovery reconciliation API existed; Backend API
  health continued projecting the stale startup issue.
- Focused green evidence and the full repository result are recorded in the
  implementing commit/hand-off report.
- Live source adoption is intentionally not claimed here. No HASHI or Remote
  process was restarted by this repair. After an authorized user reboot or
  other verified generation replacement, live Backend API and Remote health
  must be checked separately before claiming the warning is cleared in the
  running instance.
