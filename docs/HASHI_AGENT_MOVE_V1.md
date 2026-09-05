# HASHI Agent Move v1

## Purpose

`/move` transfers a durable Agent identity between HASHI instances. It is
separate from live task/session handoff and from the HASHI ↔ Hermes transfer
format.

The source sends a platform-neutral `agent-move-v1` archive to a target-owned
receiver. The source never writes a target filesystem path directly.

## Compatibility

- An outbound instance must implement this protocol.
- A target must advertise `agent_move_receive_v1` and accept schema version 1.
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
  secret;
- access requirements and an explicit target-rebind list.

The archive excludes active sessions, runtime state directories, virtual
environments, caches, nested repositories, external symlinks, source workzone
paths, and common plaintext credential files. Instance-level provider/OAuth,
browser, filesystem, and operating-system access remains target-owned.

Every preview reports included file count, package size, and excluded-path
count. The v1 receiver accepts packages up to 256 MiB and archives expanding to
at most 2 GiB. Oversized workspaces fail before publication; HASHI does not
silently truncate them. Project branches and large artifacts should be moved by
their own repository/artifact workflow.

## Transaction

1. The source validates the live target identity and authenticated receiver
   capability.
2. Preview builds and verifies a disposable package and changes neither side.
3. Prepare creates a checksummed package and stages it on the target. The
   target verifies it without modifying live Agent configuration.
4. An explicit operator confirmation commits the target Agent inactive.
5. Before a move cutover, the source rebuilds a disposable durable-state
   snapshot and compares it to the staged package. If memory, configuration,
   schedules, permissions, or Agent-owned credentials changed, the target is
   rolled back and a fresh prepare is required.
6. In copy mode, the transaction stops after inactive import and leaves the
   source active.
7. In move mode, the source configuration and schedules are disabled, then
   the target configuration is activated.
8. The source workspace is retained. Imported schedules stay disabled.

During the final freshness check the source Agent is briefly quiesced. Once
its source configuration is disabled, the still-running process refuses new
work and only directs the operator through the required source-first reboot.
This prevents memories from diverging in the interval between cutover and hot
reload.

A later return move may replace that exact inactive retained source copy when
its journal proves it was moved to the incoming source instance. The receiver
backs up the dormant workspace and configuration before replacement, so a
failed return cutover restores the dormant copy. Any active, unrelated, or
unproven Agent-ID/workspace collision remains a hard error; v1 never guesses at
memory merges.

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

## Activation

`/move` never starts a reboot. After a successful move, unload the source copy
first with `/reboot min` from the moved Agent. After it is offline, use the
target instance's `/reboot` menu from an already-running Agent and select the
moved Agent. This order prevents two processes from polling the same delivery
credential.

## Command Surface

- `/move` — choose Agent, target, preview/copy/move, then explicitly confirm.
- `/move <agent> <target> --dry-run` — disposable preflight.
- `/move <agent> <target> --keep-source` — prepare an inactive target copy.
- `python scripts/move_agent.py <agent> <target>` — prepare via CLI.
- `python scripts/move_agent.py --confirm <move-id>` — confirm a staged move.
- `python scripts/move_agent.py --cancel <move-id>` — roll back a staged move.

Legacy `--sync` and plaintext direct-move credentials are rejected. Offline
HASHI ↔ Hermes import/export remains on its existing, separate workflow.
