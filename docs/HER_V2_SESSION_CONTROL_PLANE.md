# HER v2 Fixed-Session Control Plane

| Field | Value |
|---|---|
| Status | Implemented; WIP retirement remains in shadow-validation phase |
| Date | 2026-08-31 |
| Scope | HASHI HER v2 fixed Engine Session |
| Session authority | HER SQLite session/event store |
| Provider state | Rebuildable transport state, never Session authority |
| Parent architecture | [HASHI System Architecture](../ARCHITECTURE.md) |

This document governs HER v2's **Engine Session** only. PAO and the HASHI
Session store retain authority over the enclosing HASHI Conversation Session,
Messages, Runs, Events, Workzones, context generation, and Engine binding. In
this document, `Provider` means a HER-internal Model Provider.

## Preserved invariants

This upgrade does not replace or weaken:

- the existing fixed HER Session;
- incremental HASHI-to-HER PCM transport after Session initiation;
- Strategy Cards and stage boundaries;
- Smart Tool receipts, policy checks, or permissions; or
- the public Direct, Strategic, and Planned execution modes.

Provider/model selection, recovery, accounting, and Compact are control-plane
concerns around that existing Session.

## Route revision and Turn freezing

`routing_revision` increases only when the effective default route changes.
`capability_revision` and `pricing_revision` identify the currently loaded code
and pricing table; saved route preferences cannot pin a later Turn to stale
capabilities or prices.

At Turn ingress HER snapshots:

- the routing revision;
- capability and pricing revisions;
- every effective Provider/model/reasoning target; and
- the current settled checkpoint reference.

An in-flight Turn keeps that immutable snapshot. A route change made while it
runs updates the default for the next Turn only. The next Turn stays in the
same HER Session and, when its route differs, rebuilds Provider context from
the latest settled HER checkpoint plus the already materialised recent
exchanges. No hidden Provider reasoning or process-local SDK state is treated
as recoverable Session state.

Before a route is stored, each newly selected Provider/model target with a
known context capacity is checked against the current effective context.
Reasoning-only changes do not repeat that model-capacity gate. A
known-insufficient target is rejected; unknown capacity remains an explicit
diagnostic rather than a false success. Route switching never launches Compact
as a side effect.

## Canonical recovery authority

The existing fixed-session SQLite store now owns these durable projections:

| Table | Authority |
|---|---|
| `her_session_events` | Append-only canonical event order |
| `her_active_turn_recovery` | Materialised `ActiveTurnRecoveryState` |
| `her_settled_checkpoints` | Latest settled, Provider-neutral Session checkpoint |
| `her_provider_requests` | Immutable facts for each real Provider request |
| `her_provider_request_valuations` | Price-versioned monetary valuation |

Each Strategy result, Plan, tool intent, tool receipt, side-effect boundary,
remaining-work projection, transition, and Provider request is written through
the canonical store. Reusing an event or Provider request ID with different
facts is a typed conflict, not an overwrite.

Non-read-only tool intent is persisted before execution. A successful receipt
settles it; failed, cancelled, missing, or ambiguous receipts remain unknown.
Unresolved side effects are never discarded by bounded history projection.

On process replacement, an active Turn is truthfully terminated as either:

- `FAILED_SAFE_REPLAY_REQUIRED`, when no unresolved side effect exists; or
- `UNKNOWN_SIDE_EFFECT`, when an intent lacks an unambiguous successful
  receipt.

The next user Turn receives this state as quoted recovery context. The source
state is not archived merely because it was read. Its evidence is copied into
the receiving Turn and remains linked across repeated crashes. Inherited
unknown effects become `reconciled` only after that receiving Turn completes
successfully. A failed or cancelled recovery Turn preserves unknown ancestry;
cancellation archives only ancestors already proven safe to replay.

## Provider-request accounting

Every physical Provider HTTP call—including an adapter-internal transport
retry—is recorded separately as soon as its response, failure, cancellation,
or timeout is observed. Records include:

- actual Provider and model;
- phase and parent request;
- input, output, thinking, cache-hit, and cache-miss Tokens;
- attempt and retry count;
- recovery/fallback kind and Compact flag;
- routing, capability, and pricing revisions;
- latency, status, cost source, and monetary value when known.

Usage facts and monetary valuation are stored separately. Each request binds
the pricing revision that applied when the request was made, without rewriting
its original usage facts. Unknown cost remains unknown; it is never converted
to zero.

`/meter summary`, `/meter session`, `/meter provider`, and `/meter turn` expose
durable Session totals and their Provider/Turn dimensions. The original
`/meter on|off|status` foreground cost-tail behavior is unchanged.

## Compact boundary

Compact remains necessary for a continuous Session, but it owns settled
conversation history only:

- a trailing active exchange is excluded;
- active tasks, tool requests, and side-effect evidence are never compacted;
- manual Compact is blocked while a Turn is active;
- route-fit checks instruct the operator to Compact only after settlement; and
- each real Compact Provider call is durably accounted.

If a fixed HER backend cannot prove that a durable Session meter is bound,
Compact fails before constructing or calling the Provider. Compact accounting
errors are terminal and never trigger a replay of an already settled request.

## WIP Journal retirement gate

The independent WIP Journal is not deleted in this release. New fixed Sessions
use canonical recovery as authority while the WIP Journal receives a bounded
shadow projection. Each completed Turn records missing and extra event IDs for
parity review. `/compact` no longer re-ingests WIP when canonical recovery is
available, preventing double recovery; legacy Sessions without canonical state
retain the old WIP recovery path.

Removal of the independent Journal and its `/compact` recovery stage requires:

1. parity across representative Direct, Strategic, and Planned Turns;
2. crash tests before and after non-read-only tool receipts;
3. repeated-crash recovery-chain tests;
4. Provider switch and Compact crash tests; and
5. an explicit follow-up decision to remove the shadow.

Until those exit criteria are accepted, WIP remains compatibility evidence and
canonical recovery remains the sole new authority.
