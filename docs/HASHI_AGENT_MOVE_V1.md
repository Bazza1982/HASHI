# HASHI Agent Move v1

## Source departure projection (2026-11-04)

PAO records source instance identity, source Agent lifecycle and package-keyed
departure history with successful source cleanup. The admin-only complete-Agent
Backend API projection optionally includes `agent_departures` (schema 1): source
instance identity and completed events with package ID, Agent ID, lifecycle ID,
`source_cleaned` state and completion time. Each event must agree with the final
source journal. Pending, cancelled or failed cleanup is not a departure fact;
ordinary filtered Agent reads do not expose this audit. Legacy audit entries
without enough source/lifecycle evidence remain unqualified.

An authenticated frontend must bind these facts to its verified source and
connection epoch. It may retire exactly that source lifecycle's membership and
legacy references after positive completion; roster absence or temporary
disconnection is never sufficient evidence. This projection does not copy target
groups, alter permissions or remove chat data. History is retained by package
so a later same-name Agent move cannot hide an earlier completed departure.

An optional `returns` collection qualifies a new assignment for the same lifecycle
only when the current configured Agent matches a completed, runtime-verified
incoming Move journal for this source, later than its last departure. This does
not restore any old membership. An unfinished import or a same-name recreation
is not a return receipt. Consumers persist applied departure package IDs so
replaying an old departure cannot erase a deliberate post-return assignment.

## Purpose

`/move` transfers one durable Agent identity between HASHI instances. `/clone`
creates a new Agent locally or remotely while leaving the source unchanged. These
operations are separate from live task/session handoff and from the HASHI ↔
Hermes transfer format, but deliberately share one package, receiver,
authentication, transaction journal, and registry-writing implementation.

## Clone retry and qualification audit (2026-10-29)

PAO Functions owns clone intent and recovery; Frontend Connectors project its
next action. An unfinished clone is not an unfinished source-changing Move.
Repeating a preparation with the same source, destination, requested target ID,
transfer scope and history policy reopens that transaction. This also accepts
pre-existing journals without the new requested-ID field. Changing the explicit
ID or history policy creates an independent clone. Completed/cancelled/fully
rolled-back clones do not fence future preparations. A staged Move does not
fence clone snapshots; a real source-changing cutover remains protected.

Automatic target names are selected inside the receiver mutation transaction,
not reserved by the earlier read-only resolution. They include configuration,
workspace, capability-only and unfinished-import reservations. Generated suffixes
fit the same canonical ID length limit. A Unix source ID that is a reserved
Windows filename is metadata, not a target path: Clone can use a legal explicit
ID or a safe `clone_` auto prefix. Actual target IDs and workspace members still
obey target-platform path rules. Explicit occupied IDs never overwrite data.

Preparation holds a per-transaction OS file lease through packaging/upload.
Concurrent identical requests report the existing preparation, while an abandoned
task is resumable even if its former process remains alive. Sender coordination,
receiver mutation and durable-finalizer persistence reuse `config_json`'s public
format-neutral file lock: old empty/PID lock artifacts cannot block progress, and
a live writer is not displaced because its lock looks old. Locks are scoped so
one clone's network/start wait does not serialize unrelated clone preparations.

Clone captures ordinary files into checked stable copies and databases through
SQLite backup. Its inventory and size/file-count limits cover the portable
payload, not excluded software/cache trees. Exclusion reasons remain visible;
those trees are not scanned for Move's source-deletion disclosure. Move keeps
its whole-workspace deletion inventory, but charges only the portable payload
against the transfer limit. File-size limits, stability, checksums and archive/path
validation still apply. Later source edits, new files or eligible conversation
messages cannot veto this frozen clone snapshot. Move's deletion/freshness rules
remain separate. Clone preparation does not upgrade or write the source Agent's
incarnation; the receiver assigns the new clone its independent identity.

Before showing confirmation, the receiver exercises existing import rules
without publishing registry changes. Independent known Agent configuration,
credential, schedule and capability errors are reported together. Revision and
ownership checks at commit remain necessary for a writer racing *after* staging.
Remote schema negotiation requires a valid overlapping range, not schema 1 on
every newer receiver. A transport/discovery error is not misreported as proof
that the receiver requires a restart.

An upload retry reuses the exact package ID/bytes and receiver reservation.
When the staged journal records source and destination instances, retries must
also address the same instances; a different target cannot reuse that receipt.
Definitive import-preflight rejection is different: after correcting source
configuration, retry builds a fresh snapshot rather than replaying stale invalid
metadata. Remote carries this distinction in a signed error code for JSON and
streaming uploads; unverified errors never authorize abandoning an uncertain
upload. Older receivers remain compatible but do not advertise this distinction.
Saved preparation failures expose a preparation-retry action; preparing,
prepared, confirming and completed transactions have different UI actions.
After confirmation, uncertain commit/start/finalize responses preserve progress
for the durable manager to retry. They do not implicitly stop or roll back a
potentially running clone. Cancellation is explicit and retains existing
confirmed-stop, target ownership and completed-target protections. A confirmed
rollback is terminal; a later preparation gets a fresh transaction.

The finalizer prunes delivered terminal receipts by both count and byte budget,
never the operation being written, pending work or undelivered results. An
explicit recovery retry refreshes the trusted peer snapshot and result-delivery
intent. The remaining bounded-storage/locking errors describe real pending work
or unavailable durable storage, not a reason to delete arbitrary journals.

Offline qualification covers the full prepare/confirm/activate/finalize flow in
disposable instances, duplication/concurrency, lost acknowledgements, frozen
history/files, rollback/recovery and cross-platform name mapping. This changes
shared Function ownership/locking and therefore adopts via the supported shared
Functions handoff (`/reboot max`), not an in-process reload, targeted partial
adoption or a cold Core restart. Qualification does not claim production adoption
or finish a user's staged clone without confirmation.
Remote error-code publication is adopted separately at the Remote process owner;
the same-instance clone path does not require restarting Remote.

## Stable owner and Clone qualification (2026-10-29)

PAO owns conversation identity and transfer in the Functions layer. A Telegram
account is a connector identity, not the transfer credential. Within the
personal-instance trust boundary, the existing shared-token authentication and
encrypted package authorize the transfer; the source and destination need not
use the same local owner ID. Source export is scoped to the established source
owner and Agent. The target records an internal source-to-destination binding
with the staged package, then imports history under its own owner while retaining
the source owner and message origin as provenance. This mapping is not another
credential, pairing operation or approval.

When a Workbench command has an authenticated principal, source preview,
packaging and clone retry select that principal's history, including when it
differs from legacy local configuration. The command context is bound to the
source instance and expires when dispatch ends; an untrusted explicit owner does
not grant access. The configured legacy `owner_scope_id` and Telegram-derived
values remain compatibility inputs when there is no authenticated frontend
command context. A Workbench-only instance uses its SessionStore local default
without requiring a Telegram account. Empty history is valid. For a personal
Clone, when the local default owner has no history but other unbound history
exists, preview and the staged package warn that the other history remains on
the source and was not copied. They do not guess its owner or block the Clone.
Move cleanup is scoped to the established source owner; it does not delete
unidentified history belonging to other local owners.
For a real Telegram management command, source selection uses the actor's
established Session owner and current Telegram grant, including a global
delegate's separate history. A same-instance Clone imports back into that same
owner. The live command context expires when dispatch ends; an already staged
transaction may finish from its saved, authenticated binding. These Telegram
checks apply only to Telegram commands, not to Workbench or Remote.
Governed multi-user destination mapping remains separate and has not been
qualified by this change.

Shared Functions handoff carries the retiring personal runtime's data binding
as internal continuity data. A replacement, recovery or restore defers
establishing that binding until handoff; a conflicting persisted binding is not
overwritten. If the live Session API and shared runtime disagree about their
personal data key, the handoff is refused rather than guessing which data to
adopt. The binding identifies existing local data and does not authorize a
transfer. A legacy retiring generation may omit this field; its current
fallback still derives the binding from disk after quiescence. That fallback
cannot prove an older in-memory owner if configuration changed while it was
running and therefore needs separate compatibility work and verification.
Package identity, source and
target instance, capsule digest, Agent, target owner, transfer ID and history
mode remain bound across the staged import and retries. For a personal target,
the persisted local data binding independently checks the staged destination,
including when both copies in the transaction journal change together. A missing
binding for a newly staged transfer is reported before importing; an older
journal without the field can establish its target binding once. This local
consistency check is not a source/destination owner comparison or authorization.
For a Move, the
outbound journal freezes the source owner from the packaged capsule; source
cleanup retires that owner's Sessions even if local account configuration
subsequently changes. Older journals resolve existing ownership and refuse to
infer an unrelated owner's history.

Clone can snapshot an inactive Agent, and pending messages or a busy source do
not trigger Move's drain preflight. Package consistency checks remain in force;
cloning never drains or disables source execution. A source already in Move
cutover remains fenced. Ordinary Clone does not install the source Telegram
credential. An explicitly inactive backup Clone can retain an encrypted copy
without installing or starting that connector. A Move can snapshot an
ordinary inactive Agent without starting it; its target remains inactive. The
already-moved source fence and the source-stop check still apply, and the last
active Agent may also be moved.
Authentication, encryption,
target-ID reservations, size/path/integrity checks, disabled imported schedules,
and explicit-cancellation safeguards remain mandatory. Offline checks and adoption are
separate; this repair does not require a production restart as a test.

The source sends a platform-neutral `agent-move-v1` archive to a target-owned
receiver. The source never writes a target filesystem path directly.

## Forward-only Move decision (2026-10-19)

PAO owns this Functions-layer rule. Package staging is the single mutable-state
qualification gate for a new Move: it verifies the package, compatibility,
credentials, target policy and target ID, and reserves that ID. Explicit
confirmation then durably records forward-only intent before cutover begins.

The staged result reports the source and effective destination access scopes.
A malformed source scope or destination maximum is an error. The source checks
for a changed destination maximum before recording forward intent; the receiver
also compares the staged scope decision with the current destination policy at
commit. If policy changes or becomes invalid after forward intent, the receiver
returns an authenticated rejection before publication. The source journals an
abort request; the receiver can abort only while the target is still staged.
Only an authenticated staged-abort receipt releases the source guard and marks
the transfer cancelled for a fresh preview. Lost receipts are reconciled and
retried. If the target already committed, the same Move continues forward instead.
An invalid target policy is reported while its authenticated recovery endpoint
remains reachable; it is never silently coerced to a different access scope.

After that intent exists, neither peer requalifies the transfer by hashing live
source or target files. Normal runtime changes such as Memory+ index rollover,
configuration publication, schedule state or transaction-marker cleanup cannot
veto the Move. Target online status is an execution receipt: an unavailable
runtime leaves the transaction pending and retries forward; it does not make the
already accepted package invalid.

A new Move cannot ordinarily be cancelled after forward intent. The staged-only
policy-change recovery above is permitted only with the target's authenticated
proof that no target commit occurred. Network, process-start and cleanup failures
remain in the durable manager and retry idempotently without reactivating the
source or rolling back a committed target. If the Agent must return, the
operator completes this Move and starts a separate reverse Move. Historical
legacy-transfer rollback is unchanged; Clone now follows its retry/cancellation
decision above.

## HASHI1 repair decision (2026-10-05)

The user authorized immediate repair on HASHI1 and Function reboot to apply it,
leaving the actual Move retry to the user. PAO owns the source packaging and
preparation journal; this is a Functions change, not a Core or model change.
No other instance is modified or restarted.

Cold WAL-mode memory databases reproduce the failure without a concurrent
writer: the read-only SQLite backup itself creates a WAL-index and empty WAL,
so comparing the complete raw file list invalidates its own preparation.
Only disposable reader files of successfully backed-up databases are excluded
from derived freshness. Complete inventory, deletion disclosure, durable
snapshot checksums and real-change rejection remain in force.

Regression checks cover both transfer modes, extraction of real SQLite memory,
reader-file creation/removal across preparation and confirmation, durable edits,
nonempty live WALs, similarly named ordinary files (including same-size edits),
the whole-workspace size limit, unpublished output on failure and bounded
metadata-only preparation diagnostics. The cold-WAL cases failed before the
repair; the same-size ordinary-file and preparation-diagnostic checks also
demonstrated their failures before the corresponding corrections.
The five-file focused matrix (package, coordinator, service, Remote receiver and
runtime Remote consumer) passes 140 tests. Protected Core and runtime-contract
checks pass. Generation qualification and the Core gate require committed
Function source and are checked separately before operational adoption.

Adoption uses `/reboot max` after committed-source qualification, not a
Core cold restart. The authoritative operational evidence is
`state/instance/reboot-receipts.json` plus `/api/version` and `/api/health`:
success requires the new source generation in shared Functions and every
selected Worker, terminal `online` receipt and verified readiness. Offline
tests alone do not establish production adoption or a real Move success.

### Conversation cutover follow-up

The user requested continued repair after a fresh HASHI1-to-HASHI4 Move passed
packaging/staging but failed its final durable-state check. The source journal
and receiver journal confirm complete rollback with the source retained.
Comparing the actual staged archive with a fresh source snapshot found identical
workspace contents, inventory, configuration and eligible conversation history;
only the diagnostic excluded-message count differed. Telegram's presentation-only
Move notices increment this count without entering transferred history.

PAO / Functions now derives one durable conversation projection for both the
end-of-packaging check and the final source fingerprint. Transfer identity,
capsule integrity digest and excluded-message diagnostics do not represent new
eligible history. The full archived capsule, diagnostic count, manifest summary
and integrity validation are unchanged. Eligible content, provenance, session
metadata, channel bindings and owner remain covered, as do memory/configuration.

Strengthened existing fingerprint and schema-5 cutover scenarios, plus a
concurrent-delivery packaging case, reproduced three false rejections before
the fix. The six-case focused comparison passes after the fix and continues
to reject genuine history/memory changes and capsule tampering. Component,
committed-source qualification and operational adoption remain separate checks.
The original rolled-back transaction is not retried; the operator starts a fresh
Move after repair. No receiver activation or source cleanup is performed as a test.
The six-module component run (`tests/test_agent_move_package.py`,
`tests/test_agent_move_coordinator.py`, `tests/test_agent_move_manager.py`,
`tests/test_agent_move_service.py`, `tests/test_remote_agent_move.py`,
`tests/test_runtime_remote.py`) passes 156 tests with no skips and two
third-party deprecation warnings. Protected Core and runtime-contract checks
pass. A disposable rebuild of the actual failed archive now matches its 381
eligible messages and all other durable source components despite the excluded
count increasing from 1023 to 1024. No formal SessionStore row is altered.
An in-memory mutation dropping all conversation freshness makes both genuine
history-change fences fail, confirming the protection tests exercise that
boundary; the mutation is never written to product files.

## Compatibility

- An outbound instance must implement this protocol.
- A target must advertise `agent_move_receive_v1`. New move/clone transactions
  use schema version 3 for legacy callers, schema 4 for explicit transfer
  modes, or schema 5 when the normal UI also declares conversation-history
  policy. All require `agent_transfer_lifecycle_v1`; schema 4 and later require
  `agent_transfer_modes_v1`; schema 5 additionally requires
  `agent_conversation_continuity_v1`; an exact root
  `AGENT.md` additionally requires `agent_move_retained_identity_v1`.
- Schema versions 1 and 2 remain readable so historical transaction journals
  can still be reconciled or rolled back. New clients do not expose their old
  copy/`keep_source` UI.
- A pre-feature target must be updated and reloaded before it can receive an
  Agent. A pre-feature source must be updated before it can transfer one out.
- Future senders should retain a schema-v1 exporter while schema v1 remains a
  supported compatibility floor.

## Included State

- the matching `agents.json` entry, normalized inactive for import;
- canonical `agent.md`;
- durable workspace files, including transcripts and memory databases;
- consistent SQLite snapshots rather than live WAL sidecars;
  read-only backup can create, resize or remove a SQLite `-shm` file and an
  empty `-wal`. For successfully backed-up databases only, those reader files
  are omitted from the derived freshness comparison, not from the complete
  deletion disclosure. Nonempty WALs, database edits,
  links and unrelated similarly named files remain fenced; snapshot checksums
  detect durable memory edits across preparation and confirmation;
- the Agent's schedules, imported disabled for review;
- Agent-owned secret keys, encrypted with the paired HASHI Remote shared
  secret. A move can include its Telegram credential for a single-consumer
  cutover. An ordinary clone excludes it; an explicitly inactive backup clone
  may retain it encrypted in quarantine, separate from the live connector;
- the Agent-owned capability/tool/permission declaration;
- owned Telegram delivery state, but only for a Move whose instance,
  `agent_lifecycle_id`, and actual Bot fingerprint are verifiable;
- access requirements and an explicit target-rebind list.
- for schema 5 Move, a versioned owner-checked conversation-continuity capsule;
  Clone carries one only when the operator explicitly selects read-only
  inheritance or an independent context copy.

`agent.md` is always the sole live PCM identity. On case-sensitive sources,
one additional exact, root-level, ordinary file named `AGENT.md` is preserved
by schema 2 and later as a non-authoritative attachment. It is checksummed, included in
the final freshness comparison, and never placed in the imported workspace.
Preview and transaction status identify it explicitly. During target staging,
the receiver copies it to
`state/agent_moves/incoming/<package-id>/retained-identity/AGENT.md` and records
the path and digest in transaction state. Commit, cancellation, and rollback do
not remove that preservation copy. A staging failure before the transaction
record is durable removes only that incomplete transaction and its upload.

The archive excludes active sessions, runtime state directories, queues and
in-progress work, virtual environments, caches, external
symlinks, source workzone/absolute paths, and common plaintext credential
files. Instance-level provider/OAuth, browser, filesystem, and
operating-system access remains target-owned. Imported schedules are always
disabled drafts.

The conversation capsule is not a SessionStore database copy. It contains only
terminal, visible, history-eligible user/assistant text plus the minimum source
instance/session/message ordinal, timestamp, channel-binding, provenance, and
stable origin reference needed for deterministic import. It excludes active
runs, queues, leases, approvals, backend threads, delivery claims/routes,
temporary events, media attachments, and credentials. Export is taken through
a disposable SQLite backup, so preview neither creates nor migrates the live
source SessionStore. A second snapshot at packaging completion and the normal
pre-cutover freshness fingerprint reject eligible source history that changed
after staging. Excluded-message counts are presentation diagnostics only: they
remain inside the integrity-checked archive but cannot make unchanged eligible
history stale when Move itself emits progress or confirmation notices.

An Agent name is reusable, so delivery recovery state is not keyed by name
alone. Preview remains read-only; actual preparation revision-safely assigns a
missing lifecycle ID. Move preserves that ID and carries no raw Bot token in
delivery state. The receiver validates the transferred credential, rebinds only
the instance component, and publishes the state before activation. Clone always
creates a fresh lifecycle ID and removes any inherited nested lifecycle shadow
from its target configuration, retaining other settings and the source row.
It excludes the live Telegram credential, pending recovery
state, and `undelivered/` content; an explicitly selected inactive backup may
retain an encrypted credential in quarantine. A full-workspace Move carries owned
undelivered responses and their recovery state. A narrower Move keeps them on
the source: its complete workspace and the exact owner-bound delivery record
are retained before retiring the source record. The preview and completion
report that pending responses remain on the source; the destination does not
automatically deliver them. If the source workspace was already missing, the
journal preserves the available record but does not claim the content exists.
Rollback and verified source cleanup retire only the exact incarnation, so
retry/continue cannot resurrect a stale same-name event.

Every preview reports included file count, package size, and excluded-path
count. Explicit `workspace` Moves inspect the complete source workspace for
deletion disclosure, while the default 1,000,000,000 byte (1 GB) limit applies
to portable content rather than excluded files. The limit is configurable and
negotiated as described below. Equality is accepted. Required archive/control overhead is budgeted separately;
the receiver no longer imposes an independent 256 MiB package cap. Historical
schema 1–3 packages retain their existing bounded 2 GiB expansion guard.

Explicit `identity_memory` exports canonical identity plus PCM-owned memory
paths (`is_portable_memory_path`), including the bridge SQLite database,
transcripts, saved system/context/observer state, and the registered Memory+
state, index, notepad and archive paths in `pcm_transfer`. It inventories ordinary work files
without reading their content. Oversized discarded work files do not prevent
this mode. Root `AGENT.md` is excluded in this mode and preserved as the existing
non-authoritative attachment in `workspace` mode. Nested project documents,
including nested `agent.md`, are ordinary files in full-workspace mode; a `.git`
marker no longer excludes the containing project. External links remain excluded.
An in-workspace file link is materialized as a regular file only when both its
selected name and resolved target satisfy the existing workspace exclusion rules.
This prevents an ordinary-looking alias from carrying a credential, reserved root Agent identity,
or a file inside an excluded directory. Directory links and junctions are still
excluded; their safe materialization is not yet implemented.

`/move <agent> <target>` opens the explicit scope picker. `--identity-memory` or
`--workspace` chooses the scope directly; `--dry-run` previews that choice.
The compatibility CLI uses `--transfer-mode identity_memory|workspace`.
Preview/prepared state exposes `transfer_mode`, `workspace_inventory`,
`discarded_files`, and `total_workspace_bytes`. After a verified Move, the
entire source workspace is retained in a uniquely named sibling directory,
including excluded and subsequently changed files. Its path is returned in the
completion receipt. If that filesystem cannot safely rename the directory without
replacement, it stays in its original location; if the directory is already
missing, the receipt reports this. Interrupted retention resumes from its journal.
Clone keeps its source and defaults to the same explicit full-workspace scope and 1 GB
portable-payload limit; legacy schema export requires an explicit compatibility request. Older receivers may still enforce their own whole-inventory limit. Changing scope requires a fresh preparation and confirmation.
Source freshness covers the selected content and deletion inventory, retaining
the existing exclusion for the command audit. Files changing during packaging
cause preparation to fail without publishing a new package. SQLite snapshots
and receiver payload validation retain bounded expansion checks.
Preparation failures retain the selected transfer mode and, for changed
workspaces, a bounded metadata-only difference (count and first 20 relative
paths, sizes, timestamps and link flags). File contents are never recorded.

Lifecycle HTTP calls use the PAO Worker-readiness budget with transport headroom,
including the Remote-to-local API hop. A Windows Worker taking longer than a
normal 30-second API request must not be rolled back merely because it is still
starting. Confirmed rollback is terminal for that package: the durable manager
retains the original error and requests fresh preparation instead of repeatedly
committing an already rolled-back receiver package.

Confirmation is durably accepted by the shared Functions `AgentMoveManager`;
acceptance is distinct from terminal completion. A missing background owner fails
before the source is changed.

In-process submissions wake that owner directly. Cross-process CLI submissions
publish the same durable inbox atomically; while work is pending, the owner
checks only the inbox file metadata at a bounded one-second cadence and reads
the payload only after a change. Startup and a low-frequency 30-second fallback
retain crash recovery without restoring the former quarter-second full-file
idle scan.

## Conversation continuity

Move schema 5 defaults to `history_mode=move`: the selected source owner's
eligible history is imported before target activation, with source and target
owners mapped by the authenticated transfer. Existing target messages remain
after the imported prefix, and source Sessions are archived and unbound only
after the selected target state is verified; their rows remain recoverable.

An explicit `history_mode=retain_source` archives and unbinds source Sessions
without deleting messages or attachments, retaining each original owner
partition and importing no conversation history to the target. This prevents a
later same-name Agent from automatically inheriting those Sessions. The decision is included in the
package, receiver capability negotiation, preview and transaction journal before
confirmation; the source cleanup journal rejects changing it on retry. It does
not require a source history owner to export a capsule. The owner-scoped history API
and a compatible Workbench can discover and read those retained conversations,
even after the Agent has departed; other owners cannot discover or read them.
This option does not copy pending replies or relax the separate
delivery, transaction, scheduler and single-consumer safeguards.

Clone defaults to `history_mode=none`, yielding a fresh active conversation.
The explicit alternatives are:

- `inherit_read_only`: create archived, unbound history Sessions for reference;
- `copy`: import history into independently identified active target Sessions
  and bindings.

All target Session/message IDs are newly generated. Capsule and message digests,
stable origin references, and an import-batch journal make retry idempotent even
under a different transfer ID. Import prepends history without overwriting later
target work; rollback removes only that batch's imported messages, restores its
binding changes only when the binding still has the imported value, and leaves
subsequent target messages or user rebindings intact. Stable-origin claims are
shared across transfer batches: rolling back one claimant retains a message
still referenced by another, transfers cleanup ownership, and removes it only
after the final claim is rolled back. Owner mismatch, tampering, invalid modes,
malformed provenance, and partial database writes fail atomically before the
Agent is published.

SessionStore increments `history_generation` whenever continuity import,
rollback, or moved-source retirement changes visible history. Transcript
clients send their known generation while polling. A mismatch returns a fresh
canonical snapshot with `history_reset` and `cursor_reset`; the TUI clears and
re-renders it. Canonical SessionStore rows remain authoritative—clients must
not synthesize a pass by falling back to legacy workspace `transcript.jsonl`.

## Transaction

1. The source validates the live target identity and authenticated receiver
   capability.
2. Preview builds and verifies a disposable package and changes neither side.
3. Prepare creates a checksummed package and stages it on the target. The
   target verifies it without modifying live Agent configuration.
   Owned Telegram recovery metadata is checksummed with the access declaration;
   full-workspace mode also carries referenced undelivered response files. A
   schema-5 receiver validates the continuity owner, mode, digest, summary, and
   provenance before accepting the stage.
4. An explicit operator confirmation durably commits forward-only execution
   intent before any cutover action. Before that intent, a failed preparation
   can be cancelled when it was not uploaded; a lost upload acknowledgement
   requires reconciliation with the target before rollback.
5. The shared Functions manager atomically imports target
   configuration/workspace and the continuity batch while the target Agent
   remains inactive. The staged target ID is a reservation, so another
   preflight cannot select it.
6. For a clone, the target is activated and hot-started through the Workbench
   lifecycle API, then verified as usable. The source remains active. Telegram
   is unconfigured and imported schedules stay disabled.
7. For a move, the source registry entry and schedules are disabled first. The
   target remains inactive until the source Worker and Telegram ingress are
   demonstrably stopped. The background manager then continues the transaction
   to activate and hot-start an active target. An inactive source instead carries
   an immutable inactive target choice, advertised as a receiver capability.
8. The receiver requires a live Workbench runtime receipt for an active target.
   For an inactive target it verifies both the transaction-owned inactive
   configuration and the health report's complete runtime-presence list,
   including Workers still starting or failing startup. Unknown presence cannot
   be mistaken for an offline receipt. It then removes its
   transaction markers. It does not compare mutable target workspace, PCM,
   memory, configuration, credential or schedule hashes after activation.
   Lifecycle/Bot ownership is established during import before delivery state
   is adopted; Clone imports no source delivery state.
9. Only after the runtime receipt does the source archive/unbind its formal
   conversation Sessions as the first destructive cleanup boundary. If that
   retirement succeeds, it deletes the registry entry, workspace, Agent-only
   secrets, schedules, capability declaration, and temporary package. It
   retains an audit-only move journal and destination tombstone. A cleanup
   error becomes `move_completed_cleanup_pending`; the
   already verified target remains authoritative and the source is never
   re-enabled.

Before confirmation, the UI preflight may refuse a busy source and leave the
staged transfer cancellable. Once confirmation records forward intent, the
source is quiesced and the transfer has no further mutable-state qualification
gate. Once its source configuration is disabled, the still-running process
refuses new work. The shared manager stops the source Worker and continues
automatically, including self-moves. It persists retry state and delivery
receipts independently of the initiating Worker; shared Functions recovery
resumes accepted work.
`/move continue` and CLI `--continue` remain explicit recovery actions. CLI
`--status` reads the persisted background receipt. Normal confirmation does not
require an operator to stop, start, or continue the transfer manually.
The target cannot become active before the source process is absent, which
mechanically prevents two Telegram pollers.

Historical return moves may still replace the exact inactive retained source
copy created by the legacy protocol when its journal proves ownership. New
moves clean the verified source instead. Any active, unrelated, or unproven
Agent-ID/workspace collision remains a hard error; HASHI never guesses at
memory merges.

An instance may move its last active Agent. Transfer coordination and recovery
remain available independently of that Agent; target activation still waits
until the source Worker has stopped. Target IDs are resolved
case-insensitively against both registry and workspaces: the first collision
uses `_1`, then `_2`, and so on, while `--as` accepts a legal explicit free ID.
`local` has no special meaning; omitted clone target means the current instance,
and every supplied target must resolve to one real, unique instance ID/name.

Request and response HMACs bind every state-changing Remote exchange. Both
sides keep journals so interrupted staging, commit, source-disable, activation,
verification and cleanup operations are retryable. For a new Move, failures
after forward intent always retry toward completion. Source restoration and
target rollback are not recovery paths, preventing the recovery mechanism from
creating a second live copy or invalidating a package through its own runtime
writes. Historical legacy transfers retain their compatibility recovery path.

The complete archive is carried inside an AES-256-GCM application-layer
envelope bound to the authenticated source, target, and package digest. This
keeps identity, memories, and workspace data confidential even when a
same-host or trusted-overlay Remote route uses HTTP. Agent-owned credentials
remain independently encrypted inside the archive for defense in depth.

## WSL and Windows

Archives contain relative POSIX member names only. The Windows receiver rejects
case-folding collisions, reserved device names, invalid characters, trailing
dots/spaces, and oversized path components before import. The receiver rebuilds
its own workspace path and applies only permissions supported by its operating
system. Source `workzone.json` and absolute source paths are never imported.
The source accepts only the physically lower-case canonical `agent.md` as PCM.
The exact root ordinary file `AGENT.md` has the schema-2 preservation semantics
described above. Other case variants, identity-named directories, symlinks, and
nested identity paths are rejected before packaging. Because the attachment is
stored outside the Agent workspace, a Windows target never has to materialize
`agent.md` and `AGENT.md` in the same case-insensitive directory. SQLite backup
connections are closed before their temporary snapshots are removed, including
on Windows filesystems that enforce open-file sharing locks.

## Activation and directory visibility

Schema-3 transfers use the existing authenticated Workbench lifecycle gateway,
so clone targets and verified move targets are usable immediately without an
instance reboot. The move cutover remains source-first: commit cannot activate
the target, and continue refuses while the source Worker is visible.

HChat and the Remote directory publish only active `agent_id@instance_id`
addresses. After successful source cleanup, delivery to the old local address,
Workbench HChat endpoint, Remote HChat endpoint, or protocol address returns
`agent_moved`, the new address, and an instruction to refresh the directory.
The tombstone is audit/routing metadata, not a retained Agent copy.

## Command Surface

- `/move` — choose Agent and remote target, preview, then explicitly confirm the
  source-first migration.
- `/move <agent> <target> --dry-run` — disposable preflight.
- `/move continue <transaction-id>` — finish activation/verification/cleanup
  after the source Worker is stopped.
- `/clone <agent>` — clone on the current instance using the first free ID.
- `/clone <agent> <instance>` — clone to one explicitly resolved current or
  remote instance.
- `/clone <agent> [instance] --as <new-id>` — request a legal free target ID.
- `/clone <agent> [instance] --dry-run` — disposable clone preflight.
- `python scripts/move_agent.py <agent> <target>` — prepare via CLI.
- `python scripts/move_agent.py --confirm <move-id>` — commit the target inactive
  and disable the source registry entry.
- `python scripts/move_agent.py --continue <move-id>` — after the source Worker
  stops, activate, verify, and finish cleanup.
- `python scripts/move_agent.py --cancel <move-id>` — cancel a definitively
  unpublished failure, or roll back a staged move without forward intent after
  reconciling any uncertain upload.

Legacy `--sync` is rejected. `--keep-source` produces a compatibility message
directing users to `/clone` and prepares nothing. Plaintext direct-move
credentials remain rejected. Offline HASHI ↔ Hermes import/export remains on
its existing, separate workflow.


## HASHI3 /move Remote discovery — 2026-09-07

`/move` reads the local Remote `/peers` API, the same trusted connection view
used by `/remote list`. Connected peers automatically become destinations;
no separately configured `instances.json` is required. Resolved Remote hosts,
ports, receiver capabilities and connection status come from that view.
The local instance is excluded from Move. Menus show known peers even if their
last check was unavailable, with a status label; cached liveness or capability
flags do not reject the user's choice. Before staging, the migration coordinator
verifies the actual receiver's identity and required capabilities over the
authenticated connection. Confirmation remains separate from that verification.
Recovery callbacks retain disconnected routes and retry/cancel buttons.

English and Chinese notices distinguish local Remote unavailability, an
untrusted peer view and no known target. A genuine unreachable or incompatible
receiver is reported when contacted. No UI asks the user to maintain a second target list.

This supersedes the earlier configuration-file-only fix: that fix repaired
path resolution but did not integrate Remote discovery, so its empty-directory
live result was not a successful migration-menu acceptance.

Scope: HASHI3 Frontend Connector / Functions only; no Core changes and no
HASHI1/HASHI2 code or configuration changes. Agent1 `/reboot min` is authorized.
Focused regression uses a real HTTP peer endpoint with no legacy file, relocated
code, English/Chinese menus, route propagation and connection loss before staging.
Before this change both locale cases failed to show a destination menu.
Implementation checks and live adoption evidence are recorded separately.


### HASHI3 verification receipt

- Focused: `python -m pytest -q tests/test_runtime_remote.py tests/test_ui_language.py tests/test_agent_move_coordinator.py tests/test_remote_agent_move.py`
  — 55 passed, 2 third-party deprecation warnings. No failed/skipped tests.
- Windows native: `python -m pytest -q tests/test_runtime_remote.py -k discovers_connected_peers`
  — 2 passed, 21 deselected. Both locales use explicit language contexts.
- Ruff, protected Core and whitespace checks passed.
- One authorized Agent1 `/reboot min` adopted generation
  `sha256:68ae4783ef07db71d51b88bd7eeec279959f7656b3ad4819322c835eb5d07634`.
  Core PID stayed 26056; Agent1 Worker changed from 27376 to 9412 and remained
  ACTIVE, accepting, and Telegram connected.
- Live `/move`, `/move list`, and `/move agent1` showed the agent and discovered
  destinations without a legacy instance file. HASHI1/HASHI2 advertised migration;
  connected INTEL/INTEL-WT did not advertise it; offline peers were labelled.
- Live `/move agent1 hashi2 --dry-run` authenticated the receiver and produced a
  119,973-byte disposable preview with 13 workspace files and 3 exclusions.
  No package was staged remotely, no Agent was migrated, and neither instance's
  configuration was changed. Full move/copy/recovery behavior was tested offline.
- HASHI1/HASHI2 received no code or configuration changes. Existing unrelated
  HASHI3 worktree modifications were retained outside this commit.

## Separate code and instance roots

On the shared-Functions runtime, migration reads and writes bridge_home
(or the explicit configuration directory), not the immutable generation or
source checkout. Legacy instance-directory reads and the Agent picker accept
UTF-8 with or without a BOM. Stage, confirm and recovery use the same instance
root so a split installation cannot migrate another checkout's Agent.

## Shared review checkpoint — 2026-09-08

PAO / Functions review compared `d09b5a49` with shared main `fcf6509d`:
the Remote migration implementation, command methods, and migration locale
entries already agree. Only the additional dry-run and rollback assertions
are carried into this checkpoint; no duplicate product implementation is needed.
Independent Python 3.12 verification of `tests/test_runtime_remote.py` and
`tests/test_agent_move_coordinator.py` passed all 34 tests, with no skips.
Temporary in-memory mutations each produced the expected failing test when
dry-run used the code directory, staged a target, or rollback overwrote the
original runtime-session bytes. No mutation was saved to product sources.

This is an offline review checkpoint on `fix/move-review-evidence-20260908`.
HASHI1 runtime adoption and user-terminal acceptance remain pending; shared
source equivalence and passing tests do not establish either outcome.

### Source membership and credential cleanup

After target verification, source cleanup uses Agent Directory membership semantics
inside the journaled config transaction to remove local group membership and
broadcast exclusions. It preserves dynamic selectors, qualified remote addresses,
and descriptive text. Credential protection checks declared Telegram token keys
and the canonical backend secret lookup order of remaining Agents, including
inactive Agents. Credentials used by another Agent are retained without blocking
the completed move; an ordinary string equal to the moved Agent ID is not a
consumer. Retrying cleanup uses the existing source journal and remains idempotent.

The source's listed credential keys are an inventory, not proof that each one is
required to start the selected Agent. Staging reports absent credentials as
warnings rather than rejecting the whole move. On verified source cleanup, a
credential is removed only if its current value matches the value actually
present in the protected transfer archive. Omitted credentials, later changes,
and credentials whose transfer cannot be established are retained on the
source. Startup and connector readiness still need their own truthful checks;
this inventory does not establish those checks by itself.

Schedule packaging records each original enabled state and source row separately
while keeping the imported task disabled. After verified target completion, source
cleanup removes a schedule only when the verified package and source journal prove
it is the transferred, unchanged task. New, edited, legacy-unproven or otherwise
unverifiable source schedules remain disabled on the source, with their original
rows recorded in the source journal and retained IDs reported in the move result.
The target does not automatically resume imported schedules yet; safe automated
handoff and recovery remain to be implemented.

### Authenticated streaming upload

Receivers advertise `streaming_upload=authenticated-query-gcm-v1`. Such peers
use `POST /agent-move/v1/stage-stream` with signed query fields `from_instance`,
`sha256`, `operation`, and `target_agent_id`. Request HMAC covers the canonical
query and an empty control body; the binary body retains the AES-256-GCM envelope,
binding both instance identities and the signed plaintext digest. Nonce/expiry
checks precede stream consumption. The existing authenticated JSON response proof
is still mandatory. Older peers retain the legacy JSON upload for old contracts.

Encryption, upload, decryption and service staging use bounded file reads. The
receiver caps incoming bytes, keeps temporary files private, verifies the full
GCM tag and digest before staging, and removes temporary state on truncation,
authentication or disk failures. No plaintext is imported before verification.
This transport addition does not itself change the workspace size/mode contract.

## Conversation-continuity verification receipt — 2026-09-13

- PAO/Functions owns schema-5 packaging, SessionStore import/retirement, and
  rollback; Frontend Connectors consume `history_generation` reset signals. No
  Core source changed. Implementation is on
  `fix/nightly-20260913-open-items`, based on shared main `54ddadf6`.
- Focused owner and direct-consumer regression passed 398 tests with 13 skips;
  the repository Core gate passed 667 tests with 1 skip. Tests cover owner and
  lifecycle rejection, changed-source refusal, atomic failure boundaries,
  stable-origin deduplication and shared claims, conditional binding rollback,
  history generation/reset, all Clone history modes, and cleanup ordering.
- An isolated production-package/service canary ran both WSL-to-Windows and
  Windows-to-WSL transfers through real schema-5 SQLite stores. It verified
  UTF-8 history, independent IDs and provenance, preserved target messages,
  shared bindings, source retirement, and retry idempotence in 13.94 seconds.
- The canary used temporary roots and changed no formal Agent or SessionStore.
  Formal HASHI2/HASHI3 processes were not restarted or rebooted; HASHI1/HASHI4
  were untouched by this work. Branch adoption, the HASHI4 20-Agent dry-run and
  backfill, and a real browser Workbench refresh remain separate operations.

## Legacy discovery and Move acceptance audit — 2026-10-29

The user requested a current-main audit of the older acceptance PR. PAO
Functions remain the owner. The HASHI3 candidate adds the missing regression
checks; it does not assert new live adoption.

| Requirement | Current evidence |
|---|---|
| Discovery and trusted peer identity | \`test_remote_discovery_contract.py\` covers trust invalidation and token rotation; \`test_runtime_remote.py\` covers destination projection. |
| Same-host routes and collision | \`test_remote_routing.py\` covers fallback and occupied ports. |
| Accepted versus completed | \`test_agent_move_manager.py\` covers background completion and idempotent confirmation. |
| Target verification and source retirement | \`test_agent_move_coordinator.py\` and \`test_agent_move_service.py\` cover cleanup ordering and source retirement. |
| Imported history and stale routes | \`test_agent_move_service.py\` now reopens the imported SessionStore and verifies a recreated local Agent suppresses the old tombstone. |
| Later moves | \`test_agent_move_service.py\` tests re-transfer of a previously imported Agent and rejects unproven ownership. |

On the HASHI3 candidate, the Windows Move/Remote audit suite passed 106 tests;
four tests were skipped, including the cross-platform zero-seed live canary.
The earlier PR's reported live canary was not independently repeated here.
Neither that historical report nor offline tests prove current live acceptance.

## Existing-target history backfill operator — 2026-10-03

The one-time operator for already-moved Agents reuses PAO's schema-5
conversation-continuity export, import, status and rollback owners. It is not a
second migration path: it changes no Agent registry, workspace, credential,
lifecycle state or source Session, and it never reads legacy transcripts.

Cross-platform sources export frozen capsules with their native Python and
SQLite implementation. In particular, a Windows target must not open a live WSL
SQLite/WAL database through a UNC path. Each source runs `export` against the
same strict manifest and a private capsule directory; the target consumes only
the completed capsule set:

```
python scripts/backfill_agent_history.py export --source-root <source-root> \
  --source-instance <instance> --manifest <manifest.json> \
  --capsule-dir <private-capsule-dir>
python scripts/backfill_agent_history.py dry-run --target-root <target-root> \
  --manifest <manifest.json> --capsule-dir <private-capsule-dir> > <plan.json>
python scripts/backfill_agent_history.py apply --target-root <target-root> \
  --manifest <manifest.json> --capsule-dir <private-capsule-dir> \
  --plan-file <plan.json> --backup-dir <new-backup-dir>
```

The manifest binds the exact source owner, target instance, expected entry count and
one-to-one source-instance/source-Agent/source-lifecycle/target-Agent/
target-lifecycle mappings. Source and target must carry the same lifecycle ID;
missing or changed lifecycle IDs fail closed. This
operator has no legacy name-only exception. Schema 1 uses one owner_id for
all sources. Schema 2 replaces that field with source_owners, an object
mapping every named source instance to its own established owner; source
instances may use different local owner strings. Each source checks its
mapping against its authenticated command principal, or its established local
owner for the offline operator, before export. The source owner is bound to the
schema-2 transfer identity and target plan digest. A manifest value alone does
not grant access to a source partition. Deterministic transfer IDs and
capsule names derive from that batch and mapping. Apply holds the target's
existing instance process lock, repeats target identity/lifecycle validation,
checks every owner's Runs, and requires the dry-run digest. The plan binds the
independently resolved target owner and target-state digest; the source and
target owner IDs may differ. A live authenticated command selects its own
target owner for planning, apply, status and rollback. An authenticated caller
cannot apply or roll back a different owner's plan. The offline operator can
resume from the saved plan. The full dry-run JSON keeps that selected target
when a legacy owner setting changes; apply recomputes the digest and checks the
current target state before writing. Older digest-only invocations remain
supported and resolve the current target owner. The plan is not an
authentication credential. Status and rollback check the imported batch's
source and destination ownership before acting. The original target
database and Session workspaces are privately backed up before a live
SessionStore is initialized. The backup is additive evidence; it is never
restored over newer target work. Symlinks, junctions, other reparse points,
unreadable inventory and backup paths overlapping target state fail closed.
On a partial batch failure the operator calls the existing per-transfer rollback
owner in reverse order only for claims newly created by that invocation; an
earlier successful same-batch claim is retained. `status` reads a SQLite
snapshot without initializing the live store. Explicit `rollback` uses the same
deterministic transfer IDs and holds the target process lock.

The 2026-10-03 formal scope is the twelve HASHI1-origin Agents whose source and
HASHI4 target lifecycle IDs are proven equal. The eight HASHI2-origin histories
remain in place and are excluded by user decision because their old packages do
not provide lifecycle-strength provenance; package hashes or matching names do
not authorize a legacy bypass.


## FYI v1 migration clarifications

The following operational constraints were relocated intact from the prior bootstrap during the FYI v2 migration. They remain within this document's existing ownership and authority; they are not a new state owner or proof of deployment. Future behavior changes update their owning contract and tests, not the bootstrap or this migration checklist.

### FYI v1 017 Move confirmation and Clone retry boundaries

- Move: staging checks and forward-only confirmation, with the separately
  documented authenticated staged-only policy-change recovery. Clone: PAO-owner
  snapshots, resumable retries; no Move-only gates/source lifecycle writes.
  [Move/Clone](HASHI_AGENT_MOVE_V1.md).

### Archive member capacity (source changes, 2026-11-18)

A receiver can configure its archive member budget with the positive integer
`HASHI_AGENT_MOVE_MAX_ARCHIVE_MEMBERS`; the default is 100,000. The receiver
advertises this value in its authenticated capabilities. New senders use the
advertised value during preview, packaging and upload; for a receiver without
the new field they retain the historical 100,000-member compatibility limit.
The error reports the actual member count and the limit before package output
is replaced or a source cutover begins.

A newly staged target records the accepted member count with its package
journal. Later revalidation uses that accepted count, so lowering the setting
does not invalidate an already staged package. Older staged journals retain
their historical acceptance bound. Package checksums, identity checks,
containment and archive validation still apply.

Move's source workspace inventory is separate from archive members: files
that are excluded from the portable payload no longer consume the receiver's
member allowance. An operator may set `HASHI_AGENT_MOVE_MAX_INVENTORY_ENTRIES`
to an explicit positive source-side bound; it is unset by default and does not
apply to Clone.

The receiver's compressed package budget is configurable with the positive
integer `HASHI_AGENT_MOVE_MAX_PACKAGE_BYTES`. It is advertised through the
same authenticated capability response and enforced consistently by the
streaming receiver, legacy receiver and target staging. The sender stops writing
the temporary archive when the receiver's byte budget would be exceeded and
preserves any previous output. These are limits on compressed package bytes;
the encrypted streaming envelope has its separately accounted fixed overhead.

The receiver also advertises its expanded archive budget, configurable by the
positive integer `HASHI_AGENT_MOVE_MAX_UNPACKED_BYTES`. The sender counts
uncompressed bytes across all archive members as it writes, and the receiver
checks the complete member sizes before validation/import. Existing staged
transactions retain their accepted expanded size if the setting is later
lowered; old journals retain the previous compatibility bound.

For modern transfer modes, the receiver also advertises the workspace payload
budget, configurable with the positive integer `HASHI_AGENT_MOVE_MAX_WORKSPACE_BYTES`.
It includes the live Agent identity and portable workspace files, not excluded
source inventory. Scanning and frozen copies use the target budget, including
the remaining space after each captured file; SQLite backup size is checked
after that snapshot completes. A previously staged package retains its
accepted workspace size when the setting is later lowered. Legacy packages
continue to use their existing compatibility rules.

Control JSON members are emitted to the archive in bounded UTF-8 byte chunks,
with the same canonical member content and SHA-256 values. The continuity and
snapshot digests are also updated incrementally rather than creating a second
encoded copy of the complete document.

The source still assembles its history capsule and workspace inventory in
memory, the JSON encoder can construct a large fragment for one large string,
and the receiver still reads control documents and its archive index into
memory. Individual control-member and continuity-history limits remain
separate and unchanged. Legacy JSON/base64 transfer still buffers its envelope;
use the negotiated streaming transport when available. This increment does not
by itself provide bounded-memory history import, resumable history chunks,
complete resource negotiation or proof for arbitrarily large workspaces.


## Clone Telegram credential reference

Each clone receives a fresh unbound Telegram secret reference. Existing target
credentials and references are preserved even if their names match the new
Agent; a concurrent write to the conventional Agent-name key cannot be
accidentally adopted by the clone. A manager can later set the clone's own
verified credential through the normal Telegram setup. An inactive encrypted
backup stays quarantined and does not itself start a consumer.

The backup retains its package encryption and therefore depends on the
corresponding transfer secret remaining available. A complete recovery and
controlled cross-instance single-consumer handoff are still being implemented;
the presence of an encrypted backup does not establish that the original Bot is
safe to activate on the target.
