# 多会话前台插入计划

| Field | Value |
|---|---|
| Scope | Qualify a generic agentic frontend against HASHI Persistent Session API v1 |
| HASHI baseline | `174d6bde8119ae352d2ecc36f4a88cb17294ffe4` |
| Rollback branch | `backup/multi-session-pre-insertion-20260827` |
| Activation | Fail closed until the complete joint qualification passes |

## 1. Goal

Provide one source-locked HASHI release that any conforming desktop, web,
mobile, IDE, or operations frontend can integrate without carrying a behavioural
HASHI fork. HASHI remains authoritative for Sessions, Messages, Runs, Events,
context, memory, execution controls, attachments, approvals, and fencing.

Insertion is based on protocol and declared size limits, not a frontend brand,
product revision allow-list, or private release channel. A qualification receipt
records the tested client revision for traceability, but that revision is not a
hard-coded admission rule.

## 2. Non-goals

- Do not add client-specific endpoints, runtime branches, memory policies, or
  execution modes.
- Do not import a frontend's historical local Chat archive into new Sessions.
- Do not make HASHI authoritative for window layout, unsent drafts, packaging,
  installation, or non-agent product data.
- Do not activate an incomplete client by bypassing capability negotiation,
  attachment, approval, fencing, replay, or rollback gates.

## 3. Responsibility boundary

### HASHI

- Own canonical Session, Message, Run and Event identity.
- Select and compact canonical Session history before HER v2 Triage.
- Enforce owner, deployment, Agent, Session, Run and fencing boundaries.
- Provide durable replay, consumer ACK, snapshots and restart interruption.
- Keep capability publication fail-closed until the complete HASHI gate passes.

### Conforming frontend

- Discover versioned capabilities and limits before enabling features.
- Submit the current message, `session_id`, idempotency key, permitted options,
  and committed attachment references only.
- Treat HASHI Messages, Runs and Events as authoritative; local projections are
  disposable and deduplicated by HASHI IDs.
- Derive conversation and turn correlation from its own request map instead of
  accepting caller-supplied identity as authority.
- Release local owner/turn locks on terminal, stopped, interrupted,
  superseded, or authoritative not-found recovery results.
- Enforce final product-domain authorization immediately before product-owned
  mutations.

## 4. Generic insertion contract

A client is eligible for non-activating insertion when it:

1. negotiates the required Session, Event, Control, Attachment, Approval and
   Fencing versions;
2. respects advertised message, attachment, page and history-size limits;
3. uses opaque HASHI IDs without decoding or rebinding them;
4. supports idempotent Run creation and at-least-once Event delivery;
5. persists no second authoritative sent-message archive;
6. reconstructs visible state from snapshot plus ordered Events;
7. fails closed when any mandatory capability or version is absent; and
8. retains a verified last-known-good client/runtime pair.

No product name or client revision is compiled into HASHI admission logic.

## 5. HASHI gates

### H1 — Source and worktree

- Baseline and final commits resolve by full SHA.
- Worktree is clean and the source package covers every tracked artifact.
- Formatting, compilation, focused tests and affected regression pass.

### H2 — Identity and recovery

- Activity lookup is authorized by HASHI-owned owner, Agent, request, Session
  and Run records.
- Live and recovered results expose canonical `session_id`, `run_id`, and
  `request_id`.
- Restart reconciliation terminalizes orphaned queued/running Runs, advances
  fencing, preserves Messages and emits one durable interruption Event.
- Unknown, duplicate, cross-owner, cross-Agent, stale and late activity fails
  safely.

### H3 — Context isolation

- Canonical history comes from the selected HASHI Session.
- Long history is compacted or bounded before HER v2 Triage.
- The current request remains verbatim and occurs once.
- Another Session's unpromoted sentinel never reaches the provider envelope.
- `fresh` cannot cross an active Run and starts a new context generation.

### H4 — Durable controls

- Cancel ends the Run and fences late writers.
- Archive/tombstone is non-destructive for canonical customer state.
- Promotion is idempotent and provenance-preserving.
- Consumer ACK cannot advance beyond issued Events.
- Attachment stage/commit is owner-scoped and digest-bound.
- Approval decisions are bound to the originating Run attempt and fencing
  token; expired approvals fail closed.

## 6. Size-qualified black-box profile

Run against an empty Session using the exact packaged HASHI candidate and one
declared client build. The profile declares `required_history_messages` rather
than relying on a product-specific fixed number. The generated capture binds:

- HASHI and client full revisions;
- client profile, deployment-lock digest and compatibility record;
- owner, Agent, Session, context generation, Message, Run and request IDs;
- Event consumer, issued sequence and acknowledged sequence;
- provider-envelope digest and current-request occurrence count;
- configured history threshold and actual generated history size;
- cross-Session sentinel occurrence count and terminal state; and
- last-known-good candidate.

The profile passes only when actual history meets or exceeds its declared size,
the current request occurs once, the cross-Session sentinel occurs zero times,
and the Run reaches the expected terminal state. Evidence must be generated by
the controlled harness, not written as a passing fixture.

## 7. Immutable package and signature

`tools/session_release_gate.py`:

- builds from a clean tracked source closure;
- records every artifact SHA-256 and the rollback revision;
- verifies manifest and source-archive hashes;
- optionally verifies a trusted publisher's detached OpenSSL signature; and
- converts a complete machine capture into a qualification receipt.

The trusted publisher's private key remains outside HASHI. Missing signature or
Windows verification evidence is reported as missing; it is never simulated.

## 8. Activation and rollback

1. Register the candidate as non-selected.
2. Build, hash and sign the immutable package.
3. Run the declared size-qualified black-box profile.
4. Verify startup health and capability versions.
5. Select the candidate only after every required gate passes.
6. Preserve the last-known-good runtime and its evidence.
7. Exercise rollback without deleting or rewriting Session, Message, Run,
   Event, ACK, attachment, approval, promotion or audit state.

## 9. Go / No-Go

**Insertable, not activated:** HASHI gates pass on one clean SHA and the client
meets the generic protocol and declared size contract.

**Activation Go:** package signature, joint black-box evidence, health gate and
state-preserving rollback receipt all pass for the exact packaged pair.

**No-Go:** any missing capability, identity echo, owner leak, stale fencing
write, cross-Session history, unissued ACK, incomplete attachment/approval
contract, inconsistent package hash, unsigned required package, or state-losing
rollback keeps the last-known-good path selected.
