# Remote discovery and Agent Move final acceptance

## Scope

This record closes the repository acceptance for GitHub Issue #31. The owner is
PAO Remote / Agent Move in the Functions layer. Protected Core and HER v2 are
outside scope.

## Acceptance map

| Requirement | Observable evidence |
|---|---|
| Zero-seed discovery | The Windows/WSL live-canary contract discovers peers without `instances.json`, completes authenticated handshakes, and survives shared-token rotation. |
| Same-host routing | Route tests retain the platform route first and bounded loopback fallback only for verified same-machine peers; active port collisions fail with an actionable error. |
| Owner and generation provenance | Discovery tests invalidate cached trusted metadata after credential revision, token rotation, or rejected handshake. |
| Accepted versus completed | The shared `AgentMoveManager` returns durable acceptance before background completion, treats duplicate submission idempotently, and recovers an accepted operation after manager restart. |
| Source retirement and target activation | Coordinator/service tests require verified target activation before source cleanup, archive and unbind moved source history, and retain an audit tombstone. |
| Conversation history durability | Schema-5 tests reopen the target SessionStore after import and prove imported history and bindings survive that restart boundary. |
| Stale and duplicate routing | A newly active same-name local Agent suppresses an old Move tombstone; duplicate confirmations do not repeat cutover; same-host duplicate ports fail clearly. |
| Round-trip eligibility | Imported Agents retain transfer ownership needed for a later move, while unproven prior ownership remains blocked. |

## Repository verification

- Native Windows focused Remote/Move/HChat/routing suite: 175 passed, 6 skipped.
- Five skips require a case-sensitive Linux filesystem for the retained uppercase `AGENT.md` compatibility attachment.
- One skip is the explicitly enabled Windows/WSL live canary; it is not silently treated as a pass.
- The new restart-durability, stale-tombstone, duplicate-acceptance and same-host routing assertions pass together (9 passed).
- Linux Python 3.12 platform run using the existing HASHI2 environment read-only: 180 passed, 1 expected Windows-entrypoint skip.
- Explicit disposable Windows/WSL zero-seed canary: 1 passed in 104.33 seconds. It created no `instances.json`, completed authenticated discovery in both directions, rotated the shared token, rejected stale trust, and restored a new credential generation.
- The canary used temporary roots and did not change HASHI2/HASHI3 Agent data or restart either production instance.
- Protected-Core check is run again immediately before the branch checkpoint.

## Live acceptance boundary

Only disposable canary Agents may be used. HASHI2 and HASHI3 may be adopted or
restarted for this verification. HASHI1 and HASHI4 must not be rebooted or
restarted. A live pass must retain the before/after instance generation,
authenticated peer identity, acceptance receipt, terminal completion receipt,
source tombstone, target activation, and persisted history evidence.
