# Nightly repair batch 05: workspace state persistence

Date: 2026-09-12 (AEST). Parent batch: 04. Work package: W1 / HN-20260911-002.
Repository: Bazza1982/HASHI. Branch: `repair/nightly-batch-20260911`.
Source baseline: `5146092a2d4d7b6cbe2e4cd212b31127b88570f7`.
Main baseline: `19e330f985aaf6d5523fd82a3bcc8a4256b536c0`.

## Authority and scope

The user authorized continuing the next repository-owned repair and committing
it to this same repair branch. No new approval is needed for this bounded
change. PR #16 remains draft; no automatic merge, production restart, scan,
real-data inspection or live adoption is part of this batch. Batch 03 remains
deferred to the user's local environment, not completed. HN-20260911-006/007
remain excluded from the default execution scope.

Functional owner: the existing PAO workspace persistence boundary; engineering
layer: Functions. Product file: `orchestrator/workspace_state.py` only. The
existing `tests/test_workspace_state.py`, owning persistence contract and this
receipt are the other three changed files. The shared `config_json` primitive,
process-lock registry, Core, manifest, guards, workflow, dependency versions,
per-field business rules and all previous repair code stay unchanged.

Only the existing HASHI-owned `state.json` boundary is migrated. This is not
permission to rewrite Workzones, user documents, transcripts, Memory databases,
Provider payloads or ignored machine-local files.

## Observed defect and repair

The baseline store read UTF-8 strictly and silently returned an empty dict on
read errors. Its updater then used that fallback as writable state. A BOM or
malformed file could therefore lose unrelated settings on the next update.
Publication used a process-local lock and a bespoke temporary file, without
cross-process revision checks or the shared private/durability contract.

The candidate keeps public method signatures and plain-dict results. Reads
accept BOM/CRLF without rewriting; the existing read-only fallback remains.
Writes read strictly and accept only absence as empty. `update()` captures its
revision before invoking the mutator, keeps that revision even for a newly
returned plain dict, and invokes the existing file primitive with an explicit
expected revision. First creation requires absence. A competing update or
removal cannot be overwritten by this invocation. The mutator is not replayed.

`replace()` still means explicit whole-document replacement; it can remove
fields intentionally. It now refuses an unreadable existing destination and
checks for a participating writer after its own read. It cannot reconstruct an
earlier cached plain dict's revision. Ordinary edits should use `update()`, not
`replace(read())`. This batch does not claim to have inventoried every caller
that may construct a stale whole replacement.

The store delegates private candidate creation, valid UTF-8/LF output, atomic
publication, OS locking and synchronization to `config_json`. Failures before
publication preserve the destination and leave existing backups alone. A
committed durability error propagates without replay or automatic rollback.
Callbacks should only mutate the supplied document: this is not a transaction
covering arbitrary callback side effects or external services.

## Actual offline verification

A source subset was materialized through the GitHub connector. Every complete
source/dependency/test-fixture file used for local execution was compared with
its Git blob SHA before candidate editing. Local Git metadata is only a
verification fixture, not a checkout of the entire remote repository.

Runtime: Linux, CPython **3.13.5**, pytest **9.0.2**. This is not HASHI's approved
production interpreter. The full local import closure consists of the actual
workspace store, unchanged `config_json`, `process_resources`, `backend_timeout`
and `adapters/timeout_policy`. No module placeholders, import-isolation plugins,
live services, Provider calls or private instance material were used.

Command:

```text
python -m pytest -q tests/test_workspace_state.py
```

| Run | Observed result |
|---|---|
| Exact baseline store with candidate tests | 22 failed, 6 passed |
| Candidate, repeated runs | 28 passed, zero skipped/deselected |
| Temporary removal of revision checks, four concurrency cases selected | 4 failed, 24 deselected |
| Candidate restored after that negative mutation | 28 passed |

All local runs reported one `PytestUnknownMarkWarning` for the unchanged
`tests/conftest.py` contract marker because the full repository pytest config
was not materialized. This is explicitly a source-subset run. The negative
mutation was temporary and is absent from the submitted source.

Coverage includes real legacy bytes; corruption/encoding/read permission
errors before mutation; actual spawned writers racing on existing and missing
files; a new-dict mutator retaining its read revision; interference from the
shared primitive; late deletion; permission/disk-full/replacement failures;
private POSIX candidates; committed durability errors without replay; invalid
return/serialization; and actual timeout preference set/read/clear consumers.
Four original tests are retained. Fault injection is at read or filesystem
boundaries, not replacement of the store or its persistence implementation.

Compilation and `git diff --check` passed. Before advancing the remote branch,
verify its parent, four changed paths, exact candidate blob hashes and absence
of protected paths against the authoritative manifest. The complete repository
Core guard/manifest and architecture tests must run in GitHub CI; the local
source subset does not establish that full gate.

## CI, local acceptance and remaining boundaries

The unchanged architecture workflow already selects
`tests/test_workspace_state.py`; no test hook or workflow change is introduced.
Its completed result for this candidate must be read from the actual job log
and recorded in the PR conversation. This receipt does not claim that a future
run has passed. The preceding batch's architecture run had 419 passes and the
known FYI truncation failure; the FYI source, loader and test are not modified.

In an isolated checkout with HASHI's mandated interpreter and dependencies:

```text
python scripts/check_protected_core_changes.py --base 5146092a2d4d7b6cbe2e4cd212b31127b88570f7
python -m pytest -q tests/test_workspace_state.py tests/test_backend_timeout.py
python -m pytest -q
```

The complete CLI/backend timeout test module was inspected but not run locally:
it imports the broader adapter graph. The new workspace tests exercise the
actual timeout preference functions, not the adapters' execution/timeouts.
The separately curated Core gate and any affected lifecycle tests remain
distinct from the architecture workflow. Native Windows file occupancy/DACL,
macOS, full consumer integration, independent review and running Function
adoption are not established by Linux unit tests.

Old Worker generations or nonparticipating editors can still ignore the shared
lock. Do not claim adoption simply from a source pull, and do not restart a
production instance as a test shortcut. Reverting source must not restore an
old state snapshot, remove stable lock files or change preceding repairs.

W1 is still partial. Other configuration writers, the locally deferred scanner,
Provider/HER, TUI routing, metadata/media work and live adoption remain open.
