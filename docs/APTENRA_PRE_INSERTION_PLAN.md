# 插入前计划

| Field | Value |
|---|---|
| Scope | Prepare a normal HASHI release for Aptenra D-021 intake |
| HASHI baseline | `174d6bde8119ae352d2ecc36f4a88cb17294ffe4` |
| Rollback branch | `backup/aptenra-pre-insertion-20260827` |
| Candidate reported by independent review | `96d89c71` (not present in the local HASHI object database; provenance must be supplied before it can be cited as an intake source) |
| Activation | Forbidden by this plan; this work may become insertable but must remain inactive in Aptenra |

## 1. Goal

Produce one source-locked, generally released HASHI candidate that Aptenra can
evaluate through D-021 without carrying a behavioural HASHI fork. The candidate
must preserve long-Chat continuity before HER v2 Triage, bind request activity
to HASHI-owned Session and Run identity, recover terminal request state after a
HASHI restart, and provide repeatable qualification evidence.

The target state is **insertable but not activated**. Aptenra may copy or package
the exact candidate only after every HASHI gate below passes. Aptenra must not
select it in a product Runtime Lock until its own gates also pass.

## 2. Non-goals

- Do not merge, activate, or select the candidate in Aptenra.
- Do not edit Aptenra's embedded HASHI copy to hide a failed upstream gate.
- Do not add an Aptenra-only HASHI API, source branch, memory rule, or execution
  mode.
- Do not import Aptenra's local conversation history into HASHI Sessions.
- Do not bypass Runtime Lock, Compatibility Matrix, D-056, or Lane C.
- Do not delete unrelated working-tree changes, runtime data, or rollback
  evidence.

## 3. Current baseline

The pre-insertion checkpoint contains the persistent Session repository,
Session-scoped runtime propagation, Session-scoped recent exchanges and Compact
state, and prompt assembly before HER v2 Triage. Its focused baseline is 188
passing tests, plus clean Python compilation and `git diff --check`.

The legacy Aptenra path remains unsuitable as the final integration contract:
the client selects at most 16 history rows, uses isolated/ephemeral execution,
and suppresses memory persistence and Habit learning. The approved convergence
path is the generic Session API: the client sends only the current message,
HASHI `session_id`, an idempotency key, and permitted options. HASHI selects and
compacts canonical Session history before Triage.

The reported hash `96d89c71` is not resolvable in HASHI1, HASHI2, or the local
HASHI worktrees. Therefore acceptance is based on the checkpoint/final full
hashes and generated evidence, not on that unverified short hash.

## 4. Responsibility boundary

### HASHI

- Own Sessions, Messages, Runs, request identity, durable Events, history
  selection, compaction, provider input, restart reconciliation, and owner
  authorization.
- Keep `/api/v1` generic and versioned. No endpoint or runtime branch may be
  named for or conditional on Aptenra.
- Validate activity lookup against authenticated owner, Agent, request, Session,
  and Run; never echo client-submitted conversation or turn identity as proof.
- Return a durable Run-derived terminal state when the bounded live activity
  cache was lost during restart.
- Keep `persistent_session_v1` capability publication fail-closed until the
  complete HASHI qualification gate is signed off.

### Aptenra

- Consume the generic capability, Session, Message, Run, Event, snapshot, and
  ACK contracts without maintaining an authoritative parallel transcript.
- Release its owner file and turn lock on a HASHI-authoritative terminal,
  stopped, interrupted, superseded, or not-found result according to the frozen
  recovery contract. `request_activity_not_found` must not create permanent
  `primary_busy`.
- Validate `primary_agent_activity_v1` against the Host-owned mapping from
  `request_id` to `conversation_id` and `turn_id`; ignore or reject caller
  identity that does not match. Cover commentary, progress, tool start/end,
  duplicate, unknown, stale, and late activity.
- Remove the 16-row history upload and ordinary-Chat isolated/ephemeral/
  skip-memory/Habit suppression only when the Session API path is activated.
- Own Runtime Lock, Compatibility Matrix, packaged-source hashes, UI routing,
  product rollback, and candidate selection.

## 5. Working-tree handling and rollback points

1. Preserve the received worktree in checkpoint
   `174d6bde8119ae352d2ecc36f4a88cb17294ffe4`.
2. Keep `backup/aptenra-pre-insertion-20260827` fixed at that checkpoint.
3. Make all insertion-readiness changes in normal HASHI source and tests.
4. Before the final commit, require a clean index, `git diff --check`, focused
   tests, affected regression, compilation, and qualification evidence.
5. Record the final full commit hash and dirty/untracked state in the handoff.
6. Roll back code by selecting the checkpoint branch; do not delete Session
   databases or Aptenra rollback evidence. Runtime activation or rollback needs
   a separate operational decision.

## 6. Modification order

1. Freeze the generic Session/Run identity contract and remove client-specific
   naming from the Session API implementation.
2. Owner-scope request activity and add durable restart fallback.
3. Test live commentary/progress/tool activity, duplicates, unknown requests,
   cross-owner/cross-Agent access, terminal late events, and restart recovery.
4. Prove that long canonical Session history is assembled/compacted before HER
   v2 Triage while a short current request remains protected and appears once.
5. Generate a source-bound Lane C evidence document.
6. Run the full gate sequence, create the final HASHI commit, and stop before
   Aptenra intake or activation.

## 7. Gates

### Gate H1 — source and worktree

- Baseline and final commits resolve by full hash.
- No unexplained staged, modified, or untracked task file remains.
- `git diff --check` and Python compilation pass.

### Gate H2 — D-021 intake contract

- The candidate is a normal HASHI commit with no Aptenra-only API or vendored
  patch.
- `/api/v1/capabilities` remains absent until qualification; disabled routes
  return `session_api_not_ready`.
- The exact source closure and test evidence are available for Aptenra to hash
  and package.

### Gate H3 — identity and restart recovery

- Activity lookup is authorized by server-owned owner, Agent, request, Session,
  and Run records.
- Live results include canonical `session_id`, `run_id`, and `request_id`.
- Lost in-memory activity recovers from the durable Run record and exposes a
  terminal state without fabricating events.
- Duplicate, unknown, stale, late, and cross-owner activity fail safely.

### Gate H4 — pre-Triage continuity

- Canonical history comes from the active HASHI Session, not a client-supplied
  bounded history array.
- Long history is compacted or bounded before the HER v2 Triage provider call.
- The current short request remains verbatim, protected, and present once.
- Another Session's unpromoted history cannot enter the provider request.

### Gate A1 — Aptenra owner recovery (external prerequisite)

- A restored owner encountering a missing or terminal HASHI request releases
  its owner file, attachment grants, protocol state, and turn lock exactly once.
- A subsequent request is accepted instead of permanent `primary_busy`.

### Gate A2 — Host identity (external prerequisite)

- `primary_agent_activity_v1` derives conversation and turn identity from the
  Host-owned request map and rejects caller mismatch.
- Tests cover commentary, progress, tool, duplicate, unknown, stale, and late
  activity rather than only field echoing.

### Gate A3 — Runtime Lock and Compatibility Matrix (external prerequisite)

- The candidate cannot be selected until Runtime Lock and Lane C are green.
- Runtime Lock, matrix row, source tag, packaged source hashes, adapter/shim
  identity, Aptenra revision, profile, and intake evidence agree exactly.
- Last-known-good remains selectable and its evidence remains intact.

### Gate C — Lane C long Chat plus short request black box

Run from an empty test Session using the exact packaged HASHI candidate and the
target Aptenra revision/profile. Build a long multi-turn Chat above the configured
compaction threshold, then send one short request. Capture server-authoritative
Session/Run/Event records and the hashed pre-Triage provider envelope. Evidence
must bind:

- HASHI full revision and source-tree digest;
- Aptenra full revision, product profile, Runtime Lock and matrix row;
- owner, Agent, Session, context generation, request, Run, and message IDs;
- compaction generation/input boundary and provider-request digest;
- current-request occurrence count, cross-Session sentinel absence, terminal
  result, restart replay/ACK, and rollback candidate.

The evidence generator must be controlled and repeatable. Merely consuming a
handwritten external JSON fixture is not qualification.

## 8. Runtime Lock and Compatibility Matrix update sequence

These Aptenra-owned files change only after HASHI publishes the final candidate:

1. Add a non-selected Compatibility Matrix row referencing the exact HASHI
   commit and capability versions.
2. Build and hash the complete candidate bundle.
3. Run Gates A1, A2, and C against the exact Aptenra revision/profile.
4. Attach generated evidence and record every identity above.
5. Update the signed Runtime Lock only after all gates pass.
6. Select the candidate in a staged profile, retain last-known-good, then run
   product smoke and rollback drills.

No target such as `d056-target-20` may be selected merely because its source row
exists.

## 9. Go / No-Go

### Go: insertable but not activated

All H gates pass on one clean HASHI commit; the formal handoff records commands,
results, hashes, and remaining Aptenra prerequisites. Aptenra may then perform a
non-activating D-021 intake and create a non-selected matrix row.

### No-Go

Any unresolved source hash, dirty task file, owner leak, identity echo, permanent
busy reproduction, cross-Session history, missing pre-Triage evidence, skipped
Lane C, unsigned/inconsistent Runtime Lock, or selectable target before all gates
is an immediate No-Go. The candidate remains uninserted or non-selected and the
last-known-good path stays authoritative.

## 10. Activation-candidate closure (27 August 2026)

The generic HASHI candidate now includes the complete `4d61aeb40a5e3183d6aa6cd479766a90d7a2efed`
orphaned-Run reconciliation change. The final handoff must cite the single full
descendant SHA produced after all tests, never the earlier `6a4d4a1` candidate.

Phase 4 publishes independently versioned Session, Event, Control, Attachment,
Approval, and Fencing contracts. The implemented controls are cancel, fresh,
non-destructive archive/tombstone, scheduled/manual promotion, durable consumer
ACK, attachment stage/commit, approval decision bound to the originating Run
attempt, monotonically fenced execution, and durable restart interruption.
Capability publication remains fail-closed until the joint evidence is complete.

`tools/session_release_gate.py` is the controlled release gate. It builds a
source-locked package from a clean Git revision, records every tracked artifact
hash and the LKG revision, verifies the package and optional OpenSSL detached
signature, and converts a complete machine capture into a Lane C receipt. It
deliberately refuses incomplete Aptenra identity or a failing 20-message profile.

The following external inputs are mandatory and must not be simulated:

- the final Aptenra full revision and qualification profile;
- the exact Compatibility Matrix row and Runtime Lock file/hash;
- a black-box capture from the exact packaged pair, including authoritative
  Session, Run, Event and consumer ACK identity plus provider-envelope digest;
- the trusted publisher's private-key signing operation, detached signature,
  public certificate/key and approved publisher fingerprint;
- Windows verification output for package hash, detached signature, startup
  health, LKG selection, and the no-customer-state-loss rollback drill.

Until those materials are supplied, the honest release state is **HASHI controls
implemented; joint activation No-Go**. LKG remains selected and
`/api/v1/sessions` continues to return 503.
