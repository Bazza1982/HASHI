# Core v5 — complete main integration, 2026-11-06

Functional owner: PAO lifecycle and permissions. Engineering placement:
Functions and platform provisioning. This is source integration into the
existing Core v5 development branch, not release adoption.

## Scope and preserved histories

Merge complete main `38bb778cc0cb24f4e1feb8bba4334d9b8805cc7f` into
`feature/core-runtime-decoupling-20261105`, starting at
`0321a57014d48acc157d8485e82bc8d3694207e4`.
The common ancestor is `6f78ac7f45b25111e68a737a5e6588152341b6a2`.
Retain both histories through an ordinary merge; do not copy selected patches,
rebase away either side, or replace the complete main permission implementation.

The complete main interval changes 137 paths. Of these, 131 non-overlapping
paths retain main's exact indexed blobs and modes. The 2,614 branch-only or
unchanged paths retain their exact pre-merge blobs and modes. Six shared paths
receive explicit review, including the three content conflicts below.

## Conflict decisions

| Path | Resolution and reason |
| --- | --- |
| `scripts/provision_transcription_runtime.py` | Keep main's permission-aware `write_agent_config_json` publication and canonical Fingerprint ID. Retain the branch's isolated `-I -B` probe/model commands, so published helper seals are not changed by bytecode generation. Keep revision-bearing configuration documents; remove only the unused generic-writer import. |
| `scripts/provision_tts_runtime.py` | Keep the same permission-aware publisher and main's canonical Fingerprint ID, together with the existing `-I -B` probe/model preparation. No direct generic Agent configuration publication remains. |
| `tests/test_permission_decision_commands.py` | Retain the explicitly injected authority clock, exact 120-second expiry assertion, and main's before/after authority-time bounds. Partial scope, idempotent retry, decision-count and revision-conflict assertions remain. No product clock or approval lifetime changes. |

The automatically merged English and Chinese catalogs retain all 17 main
changes and all three Core v5 identity labels without duplicate JSON keys.
The Agent deletion test retains the isolated fixture's explicit absence of an
installed Remote task; separate credential-reader and principal-failure cases
remain present and are executed. Product credential cleanup and permissions
code from main is preserved.

Protected Core files remain byte-identical to the pre-merge candidate.
Core source digest:
`sha256:cbad4ed2987d209665d12af2c1c8c2ce05a3f492f11abfa8592f051fcce705ac`.
The independent review digest remains
`sha256:337b569d82580d478cd28d962cf4414591f19ba4e6cd69a07e8a4eaa902f8dc3`.

## Pre-commit evidence and remaining gates

The resolved working tree passed 144 focused native Windows cases across
permission decisions/publication, transcription/TTS/OCR provisioning, language
preferences, Agent deletion and Fingerprint execution. No skips or assertion
relaxations were introduced. Ruff, indexed whitespace, staged Core protection,
and the complete preservation audit passed. These checks bind the pre-commit
resolved tree; exact committed-head CI and artifact results belong in the PR
and retained native receipts, not a self-referential commit identifier here.

The initial and final original-dependency evidence and same-Core actual
five-package upgrade/rollback remain separate historical receipts. PR #137 is
not merged as-is. Final integration must rerun the complete offline and
permission scopes and qualify affected real release artifacts.

Main integration authorization does not approve merging the PR, deployment,
active-environment modification, an existing Core restart, or service
installation. Independent human Core review, complete real-account/frontend
and native L2/WAC acceptance, elevated install/reset/rollback and operator
cutover remain separate gates. Do not represent source or build success as
runtime adoption.

## Permission CI compatibility

The first merged-head permission workflow still enforced an unconditional
comparison to main, so it rejected the already-authorized Core major migration
as an ordinary feature edit before executing permission regressions.

Use the event baseline for push checks and the PR base for full-branch checks.
Only the existing trusted `core-change-approved` PR label acknowledges the
major migration; the canonical checker still enforces version and independent
same-generation review. Missing/zero push baselines fail closed against main.
No check is skipped and no failure is marked successful.

Executing the actual Bash gate reproduced the original false rejection.
After repair, the existing-generation push passes, an unapproved Core PR
still exits 2, and an approved major without human review still exits 4.
Missing and all-zero baselines also retain rejection. Original CI logs and
these native red/green receipts remain preserved.
