# Dependency maintenance after the Core 5 separation

Revised: 2026-11-08. Owners: PAO runtime lifecycle and Platform packaging.
This replaces PR #137's earlier coupled-runtime migration proposal. The approved
Core 5 implementation on main is the baseline; a dependency repair does not
create another Core generation merely because its runtime fingerprint changes.

The owning contracts are [Layered Runtime Boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md),
[Python Runtime Compatibility](HASHI_PYTHON_RUNTIME_COMPATIBILITY.md),
[Dependencies](DEPENDENCIES.md), and the
[Core 5 acceptance plan](CORE_RUNTIME_DECOUPLING_ACCEPTANCE_20261105.md).

## Independent identities and lifecycles

| Owner | Contents | Revision and adoption |
| --- | --- | --- |
| C | Protected Core implementation and its versioned wire contracts | An actual implementation or contract change follows the Core major-release policy |
| CR | Qualified Core-host interpreter, standard library and ABI | Independently sealed runtime revision; no third-party application distributions |
| FR | Qualified shared/Agent Functions interpreter and application dependencies | Its exact role lock, installation receipt and selector; compatible replacement keeps C and CR fixed |
| FG | Immutable Functions source artifact | Qualified with its selected exact FR and compatible Core contract |
| Optional helpers | OCR, transcription, TTS, privacy and other declared external runtimes | Their own provisioner, lock, models and versioned functional contract |

The standard application dependency lock belongs to FR. It is not a Core lock.
CR and FR need not share an executable, prefix, Python patch, package set or
runtime identity. Within each role, exact installation identity and admission
checks remain mandatory. Core owns process isolation and wire compatibility;
it does not import Telegram, backend, media or other product owners.

A vulnerability in an FR or optional-helper package calls for a repair and
qualification of that owning runtime. It does not force a Core source upgrade.
A CPython/stdlib vulnerability affecting CR requires a separately qualified CR
replacement and approved cold cutover. It consumes a new C generation only
when Core implementation or its contracts actually change.

## Dependency inputs retained by this repair

Main already carries the qualified Functions pins from the earlier proposal:
aiohttp 3.14.4, multidict 6.9.1, yarl 1.25.1, certifi 2026.10.19 and
Pillow 13.0.0. The source-install metadata retains the raised direct aiohttp
and Pillow security floors. The complete current Portable hash lock is kept,
including its newer greenlet 3.5.6; no older lock fragments are copied over it.

Package metadata, the existing exact role locks and their existing compilers
remain authoritative. This repair adds neither a second dependency catalogue
nor a common lock or interpreter for independent roles.

## Migration and rollback

1. Observe the actual active C, CR, FR and FG receipts. A source checkout or an
   audit of a development interpreter is not evidence about an active runtime.
2. Materialize a new owning role outside active environments. Verify exact
   packages, interpreter/base bytes, role, import purity, dependency hashes,
   ABI/API/schema compatibility and all affected functional profiles.
3. Qualify the candidate with the existing runtime-contract and lifecycle
   owners. Rejecting a candidate must leave the active route and durable
   request/reply identity intact. Preserve the prior installations and artifact.
4. Publish a compatible FR/FG selection through the existing atomic selector
   and Functions handoff. Core code, Core-host runtime and Core PID stay fixed.
   This uses admitted replacement processes, never in-process module reload.
5. A CR replacement uses its separate qualified cold-cutover procedure and
   authorization. Do not install into a running interpreter, copy instance
   credentials/data with source, or reinterpret a source push as deployment.

Rollback selects the retained compatible role/artifact through the same owning
lifecycle. It does not mutate or reseal the old installation. Candidate rejection,
failed preparation, handoff recovery and mixed Agent generations retain their
existing tests. A future incompatible persisted-data change must declare its
own migration/backup/rollback limits; this PR adds no data-format migration.

## Core approval scope and sole-owner management

The source manifest remains the sole definition of protected Core files.
Core API, Function API, Worker model/protocol and generation-schema declarations
in the existing runtime policy are also versioned Core wire contracts. Changing
those declarations cannot evade approval merely by leaving Core source bytes
unchanged. A staged contract change cannot be hidden by restoring the worktree.

Actual Core source/contract changes retain explicit authorization, a major reset,
the trusted approval label and a newly added matching review record. A changed
metadata contract additionally seals the exact project metadata in
reviewed_files_sha256. Independently revised role locks are not Core approval
inputs. Declared lock paths still must remain within the repository.

The existing owner_approved mode allows the actual sole repository owner to
approve after reviewing truthful technical evidence. A second human is not
required, and an AI reviewer must identify itself accurately. Agents cannot
invent owner decisions or apply approval labels to themselves. This PR changes
no protected Core implementation or wire contract, so it does not request a new
Core major or an unnecessary Core approval label.

See [review records](core-reviews/README.md) for the exact schema. The three
2026-11-05 PR #137 records remain immutable historical evidence of the earlier
coupled proposal; their seals and review scope are not approval of this later
tree. The approved Core 5 record already on main is not rewritten.

## Verification and release boundary

Regression coverage distinguishes an allowed independent dependency/interpreter
revision from a blocked unapproved Core wire change, including staged state,
typed metadata, truthful sole-owner review and stale/missing review seals.
Runtime-role, immutable installation, qualification, rejection, rollback,
function-worker, permission receipt replay and package contracts exercise the
existing migration mechanisms.

The old Windows permission failure used a one-second deadlock watchdog during
durable receipt completion. Current main includes the independently reproduced
writer-queue and unchanged-read write-amplification repairs, with shared bounded
watchdogs and all permission/replay assertions retained. See the
[Windows diagnosis](permissions/PR133_WINDOWS_CI_DIAGNOSIS_20261105.md).
The historical hosted storage/scheduling event was not measured; a green rerun
alone is not its diagnosis.

Final commit, exact test/CI results and mergeability are recorded in PR #137.
No production service modification, restart, merge, deployment or publication
is part of this repair.
