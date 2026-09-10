# HASHI Agent Move v1

## Purpose

`/move` transfers one durable Agent identity between HASHI instances. `/clone`
creates a new Agent locally or remotely while leaving the source active. These
operations are separate from live task/session handoff and from the HASHI ↔
Hermes transfer format, but deliberately share one package, receiver,
authentication, transaction journal, and registry-writing implementation.

The source sends a platform-neutral `agent-move-v1` archive to a target-owned
receiver. The source never writes a target filesystem path directly.

## Compatibility

- An outbound instance must implement this protocol.
- A target must advertise `agent_move_receive_v1`. New move/clone transactions
  use schema version 3 for legacy callers, or schema 4 for explicit transfer modes,
  and require `agent_transfer_lifecycle_v1`; schema 4 also requires
  `agent_transfer_modes_v1`; an exact root
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
- the Agent's schedules, imported disabled for review;
- Agent-owned secret keys, encrypted with the paired HASHI Remote shared
  secret. A move includes its Telegram credential for a single-consumer
  cutover; a clone always excludes every Telegram credential;
- the Agent-owned capability/tool/permission declaration;
- access requirements and an explicit target-rebind list.

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

Every preview reports included file count, package size, and excluded-path
count. Explicit `workspace` transfers inspect the complete logical workspace
inventory before compression and reject more than 1,000,000,000 bytes (1 GB).
Equality is accepted. Required archive/control overhead is budgeted separately;
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

`/move <agent> <target>` opens the explicit scope picker. `--identity-memory` or
`--workspace` chooses the scope directly; `--dry-run` previews that choice.
The compatibility CLI uses `--transfer-mode identity_memory|workspace`.
Preview/prepared state exposes `transfer_mode`, `workspace_inventory`,
`discarded_files`, and `total_workspace_bytes`. Confirmation warns that successful
Move deletes the entire source workspace, including excluded files. Clone keeps
its source and defaults to the same explicit full-workspace scope and 1 GB
preflight; legacy schema export requires an explicit compatibility request. Changing scope requires a fresh preparation and confirmation.
Source freshness covers the selected content and deletion inventory, retaining
the existing exclusion for the command audit. Files changing during packaging
cause preparation to fail without publishing a new package. SQLite snapshots
and receiver payload validation retain bounded expansion checks.

Confirmation is durably accepted by the shared Functions `AgentMoveManager`;
acceptance is distinct from terminal completion. A missing background owner fails
before the source is changed.

## Transaction

1. The source validates the live target identity and authenticated receiver
   capability.
2. Preview builds and verifies a disposable package and changes neither side.
3. Prepare creates a checksummed package and stages it on the target. The
   target verifies it without modifying live Agent configuration.
4. An explicit operator confirmation persists an execution intent; the shared
   Functions manager commits the target Agent inactive.
5. Before a move cutover, the source rebuilds a disposable durable-state
   snapshot and compares it to the staged package. If memory, configuration,
   schedules, permissions, or Agent-owned credentials changed, the target is
   rolled back and a fresh prepare is required.
6. For a clone, the target is activated and hot-started through the Workbench
   lifecycle API, then verified as usable. The source remains active. Telegram
   is unconfigured and imported schedules stay disabled.
7. For a move, the source registry entry and schedules are disabled first. The
   target remains inactive until the source Worker and Telegram ingress are
   demonstrably stopped. The background manager then continues the transaction
   to activate and hot-start the target.
8. The receiver verifies target identity, canonical PCM, durable workspace and
   memory digests, remapped Agent credentials, portable paths, disabled
   schedules, capability declaration, and live Workbench availability.
9. Only after verification does the source delete its registry entry,
   workspace, Agent-only secrets, schedules, capability declaration, and
   temporary package. It retains an audit-only move journal and destination
   tombstone. A cleanup error becomes `move_completed_cleanup_pending`; the
   already verified target remains authoritative and the source is never
   re-enabled.

During the final freshness check the source Agent is briefly quiesced. Once
its source configuration is disabled, the still-running process refuses new
work. The shared manager stops the source Worker and continues automatically,
including self-moves. It persists retry state and delivery receipts independently
of the initiating Worker; shared Functions recovery resumes accepted work.
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

Each instance must retain at least one active Agent. A move of the last active
Agent fails before packaging or target mutation and directs the user to create
or clone another Agent. Clone remains allowed. Target IDs are resolved
case-insensitively against both registry and workspaces: the first collision
uses `_1`, then `_2`, and so on, while `--as` accepts a legal explicit free ID.
`local` has no special meaning; omitted clone target means the current instance,
and every supplied target must resolve to one real, unique instance ID/name.

Request and response HMACs bind every state-changing Remote exchange. Both
sides keep journals so interrupted commit, source-disable, source-restore, and
target-rollback operations are retryable. A failed cutover restores the source
and rolls back the target when both peers remain reachable. If target rollback
cannot be confirmed, the source remains disabled rather than risking two live
copies; the same confirmation can be retried idempotently, or cancellation can
reconcile target rollback before restoring the source.

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
- `python scripts/move_agent.py --cancel <move-id>` — roll back a staged move.

Legacy `--sync` is rejected. `--keep-source` produces a compatibility message
directing users to `/clone` and prepares nothing. Plaintext direct-move
credentials remain rejected. Offline HASHI ↔ Hermes import/export remains on
its existing, separate workflow.


## HASHI3 /move Remote discovery — 2026-09-07

`/move` reads the local Remote `/peers` API, the same trusted connection view
used by `/remote list`. Connected peers automatically become destinations;
no separately configured `instances.json` is required. Resolved Remote hosts,
ports, receiver capabilities and connection status come from that view.
The local instance is excluded. Menus omit disconnected peers; `/move list`
labels disconnected and unsupported receivers. Selecting or executing a target
refreshes the view before any package is staged. The migration coordinator still
performs its authenticated identity/capability checks and explicit confirmation.
Recovery callbacks retain disconnected routes and retry/cancel buttons.

English and Chinese notices distinguish local Remote unavailability, an
untrusted peer view, no connected targets, a disconnected target and an
unsupported receiver. No UI asks the user to maintain a second target list.

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
inactive Agents. A real consumer blocks cleanup before configuration or workspace
deletion; an ordinary string equal to the moved Agent ID does not. Retrying cleanup
uses the existing source journal and remains idempotent.

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
