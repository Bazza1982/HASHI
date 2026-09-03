# HER v2 WIP Journal

Status: **shadow/legacy compatibility contract; not current recovery authority**

Current authority:
[HER v2 Fixed-Session Control Plane](HER_V2_SESSION_CONTROL_PLANE.md).

The HER v2 WIP Journal is a bounded model-independent transient Context
projection retained while canonical HER Engine Session recovery completes
shadow validation and for legacy Sessions without canonical state. It is not
Agent Memory, Memory+, a continuation command, a Model Provider transcript, or
a second recovery authority. The current user request always remains
authoritative.

For current fixed HER Engine Sessions, canonical typed Session events,
active-Turn recovery state, settled checkpoints, Tool evidence, and physical
Model Provider request records are authoritative. The Journal receives only a
shadow projection and is not re-ingested by `/compact` when canonical recovery
is available. The remaining sections document the compatibility implementation
and apply only within that boundary.

## Lifecycle

1. At the start of a HER v2 turn, HASHI resolves the Journal owned by the
   current HASHI Session context generation. A bounded legacy Agent-level
   Journal is migrated into the first current Session that encounters it.
2. If prior records exist, HASHI sends a mandatory visible warning independent
   of `/verbose` and supplies only a deterministic bounded recovery summary to
   the new turn. The raw Journal is never copied into a provider request.
3. HASHI durably appends a bounded request boundary for the new turn. While the
   turn is active, selected HER v2 events are projected into small recovery
   facts only after their canonical audit records are durable.
4. An error, interruption, or other non-`COMPLETED` Ledger state preserves the
   accumulated Journal. A normal later `COMPLETED` Ledger still clears it
   atomically after the Ledger commit.
5. The user may run `/compact` at any context size. HASHI first commits a
   deterministic recovery capsule into quoted Session history and then clears
   only the exact Journal snapshot that was committed.

The Journal does not decide that the user said “continue”. Recovery summaries
are explicitly labelled quoted historical data, not instructions, permission,
completion evidence, or authority to replay side effects.

## Bounded data contract

The Journal uses `her-v2-wip-journal-v2` and enforces all of these limits before
writing:

- at most 128 records;
- at most 16 KiB per record;
- at most 512 KiB for the active Journal; and
- at most 12,000 characters in recovery context.

The first unfinished request boundary is retained while older activity details
are discarded first. Request boundaries retain only a bounded request summary,
length, digest, Session identity, and context generation. Audit rows retain
only broad stage, provider, model, attempt, event, and projected facts. Large
content becomes a length and SHA-256 marker; only bounded observable output
excerpts may be retained.

`request_received` is never copied into the Journal because that event contains
the already assembled provider request. This exclusion prevents a Journal from
copying its own recovery context back into itself and growing recursively.
Secrets identified by structured credential keys, full prompts, provider
payloads, tool catalogues, attachment manifests, and large tool output are not
written to the v2 Journal. The canonical audit system remains the evidence
source for full operational diagnosis.

## Session scope and files

New active state is stored in the current Session context workspace:

```text
<session-workspace>/backend_state/her_v2/wip_journal.jsonl
```

The retired Agent-level location is read only as a migration source:

```text
<agent-workspace>/backend_state/her_v2/wip_journal.jsonl
```

Migration first writes the bounded records into an empty Session Journal, then
compare-and-swap clears the exact legacy snapshot. A concurrent legacy append
causes the source to be preserved; duplicate recovery is safer than data loss.

The canonical HER v2 audit log remains:

```text
<base-logs-dir>/<agent>/her_v2_audit.jsonl
```

If that primary audit path is unavailable, the durable fallback remains:

```text
<agent-workspace>/backend_state/her_v2/audit_fallback.jsonl
```

## Legacy `/compact` recovery phase

For legacy Sessions without canonical recovery state, WIP recovery and ordinary
conversation compaction are independent phases of the same command. Current
canonical Sessions skip Journal re-ingestion:

1. **WIP recovery phase — always eligible.** It snapshots each active current
   Session or legacy Journal, generates a deterministic capsule without a
   model, inserts that capsule idempotently as a quoted `recovery` turn, and
   compare-and-swap clears only the committed snapshot.
2. **Conversation history phase — unchanged.** It retains the 64,000-token
   manual floor, active Quick/Light model route, normal validation, immutable
   raw archive, and atomic context pointer rules.

If capsule persistence, verification, or compare-and-swap fails, the Journal
is preserved and the conversation-history phase is not started. Retrying is
safe because recovery turns are keyed by the source Journal digest.
`/compact status` reports WIP `ACTIVE` or `CLEAR` independently of ordinary
context capacity or model-route availability.

The recovery capsule records request identities, bounded unfinished-request
summaries, event counts, selected observable activity, and failure facts. It
does not claim that unfinished work succeeded. Once committed, it participates
in ordinary Session history and can itself be included in a later normal
conversation compaction.

## Audit evidence

Filter canonical audit rows where `stage` is `wip_journal` for the ordinary
turn lifecycle:

- `wip_journal_turn_started`
- `wip_journal_context_injected`
- `wip_journal_preserved`
- `wip_journal_cleared`

The Session compaction store records the explicit recovery transaction:

- `wip_recovery_started`
- `wip_recovery_capsule_committed`
- `wip_recovery_completed`
- `wip_recovery_failed_preserved`

Lifecycle payloads contain non-content metrics and digests such as record
count, byte count, generation ID, source SHA-256, recovery turn ID, and whether
a model was invoked. This makes create, inject, preserve, commit, and clear
behaviour independently inspectable.

## Correct interpretation

- An empty Journal after a successful turn or successful WIP recovery Compact
  is expected.
- A non-empty Journal after an interrupted turn or failed recovery commit is
  expected and must produce a warning on each later HER v2 request that sees
  it.
- Receiving a recovery summary proves only that bounded context was supplied;
  it does not prove that old work was resumed or completed.
- A torn final JSONL line is ignored without hiding earlier durable records.
- Raw transcripts and canonical audit evidence are not deleted by WIP recovery
  or ordinary conversation compaction.
