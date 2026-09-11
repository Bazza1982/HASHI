# HASHI configuration persistence

Scope: HN-20260911-002 / W1, migrated Functions-layer configuration only.
Parent: [Layered Runtime Boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md).
This contract does not claim that all configuration writers have migrated.

## Ownership and current coverage

`orchestrator/config_json.py` owns the shared file primitive: BOM-tolerant
object reads, out-of-band content revisions, process/OS write locking, private
candidates, UTF-8 without BOM and LF output, file synchronization and atomic
publication. It does not own business schemas or restart/adoption policy.

| Consumer | Functional owner | Coverage |
|---|---|---|
| `ConfigAdmin` / `agents.json` | PAO | Integrated on main at `19e330f9`; retained unchanged by the first repair batch |
| `ui_language` / `state/ui_language.json` | Frontend Connector | First repair batch migrates set/reset preference operations; catalogs, wording and actor/instance scope are unchanged |
| `AgentDirectory` group mutations / `agents.json` | PAO | Second repair batch migrates create, delete, rename and member add/remove; fresh revision before business decisions, publication before cached-view updates |
| Other writers, Memory/Wiki scan transactions | Respective existing owners | Still require inventory and separate implementation; not covered by the migrated consumers above |

Normal configuration edits must still go through the owning business operation.
Do not turn a scanner into a configuration writer, introduce a parallel storage
service, or import these Functions owners into Core.

## Publication and failure contract

A read/modify/write retains its `ConfigDocument` metadata until publication.
Copying it with `dict(document)` loses its revision and must not silently turn
an edit into a full-document replacement. A first creation explicitly requires
an absent destination (`expected_revision=None`). A stale operation raises
`ConfigConflictError`; it neither publishes nor silently retries. A new,
deliberate operation may reload the latest state and apply only its own edit.

The existing primitive gives participating writers one stable OS lock; a
process-local threading lock alone does not serialize different Workers.
Unmigrated writers and arbitrary editors do not participate in that protocol.
This is not a guarantee against their concurrent writes, and does not eliminate
the separate adoption requirement for every writer generation.

Failures before atomic replacement preserve the previous destination.
`ConfigDurabilityError.committed=True` means replacement already happened but
directory synchronization failed: do not restore an old snapshot or replay the
operation automatically. Re-read and reconcile the current state. Candidates
use the primitive's private-file policy; this batch does not change that policy.

## Language preference behavior

Display-only reads may fall back to the instance default when preferences
cannot be read. This fallback is never used as the source of an update.
Set/reset operations first read a revision-bearing document and reject
unreadable/truncated JSON, a non-object user map, and unsupported schema
versions. Legacy documents without a version remain accepted as version 1.

BOM/CRLF legacy preferences are read without rewriting the file. The next
successful explicit update publishes normalized bytes. Unrelated top-level
fields, other actors' raw locale values and other actors' stored key spellings
are preserved. Only aliases of the actor being edited are consolidated to its
existing canonical actor key; resetting that actor removes all of its aliases.
No language catalog, UI layout, Telegram destination, or TUI client setting is
changed by this persistence migration.

## Group mutation behavior

`AgentDirectory` remains the group owner. Each public mutation reads a fresh
`ConfigDocument`, applies only its group operation, and publishes through the
same file primitive as ConfigAdmin. Cached group views never supply replacement
configuration. An edit therefore retains unrelated groups, current membership
cleanup, remote addresses, dynamic selectors, extension fields and non-group
settings observed in that read. A participating write after that read causes a
revision conflict, not a silent overwrite. There is no automatic retry.

The five public operations retain their tuple return shape, messages, and
`@active` member-edit refusal. No-op decisions use the fresh document without
publishing or normalizing its bytes. Missing `groups` is accepted as empty;
missing `agents.json`, invalid JSON, a non-object group map/group, and a
non-list/non-`@active` target membership are rejected without replacing them.
This is not a new global configuration schema or a membership authorization
change. Capability grants, Agent lifecycle and Move persistence are unchanged.

A path-scoped process lock orders group edits and cache publication locally;
the existing file primitive supplies interprocess locking and revision checks.
On pre-publication failure the previous cached view and destination remain.
On committed durability error the view reflects the committed candidate and
the error still propagates. `list_groups()` returns a detached deep copy.
A view is the last observed snapshot, not a cross-process live subscription;
an explicit refresh or later mutation may observe newer external changes.

## Operator and Agent FYI

UTF-8 editor output with or without BOM is supported at these migrated read
boundaries. A conflict asks for a fresh read, not permission to overwrite.
Unreadable or incompatible saved preferences require diagnosis; do not delete
or replace them with defaults merely to make a command succeed. Preserve the
original bytes and any existing backup before a separately authorized repair.

A group-save conflict is not a successful group change. Re-read before a new
deliberate edit; do not reconstruct `agents.json` from a cached group list. A
committed durability error is not permission to undo the published change.

Do not normalize arbitrary user files, Provider wire evidence, Workzones,
Markdown, images, audio, or documents. Do not remove a persistent lock file as
an unlocking shortcut. Do not claim that pulling source adopts it into running
immutable Workers, and do not restart production as a test shortcut.

## Verification and remaining work

The focused suite is `tests/test_ui_language_persistence.py`. It uses temporary
files and real spawned processes, not live connectors. Existing direct-consumer
language tests and the repository's curated gate remain separate required
integration checks, using the approved runtime and dependency generation.

Group regressions live in the existing `tests/test_runtime_groups.py`, including
real file/spawn scenarios and the command consumer. That module is already in
the architecture CI selection; no workflow or collection hook is added. The
per-batch receipt distinguishes local source-subset evidence from full-import
CI and native/live adoption.

Memory/Wiki configuration preflight before writes, database rollback, stable
scan identity, remaining configuration writers, native Windows/macOS evidence,
and instance adoption are still open. This migration neither inspects nor
cleans historical database duplicates and must not close W1 as a whole.
