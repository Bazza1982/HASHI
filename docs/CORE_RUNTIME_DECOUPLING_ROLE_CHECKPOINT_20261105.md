# Core v5 role-contract checkpoint

Status: tested source checkpoint preserved for independent review; NOT a completed migration, merged implementation or running adoption.

Functional owner: PAO release governance. Engineering scope: explicitly authorized Core v5 role/runtime contract, validation and version projections.

## Preserved implementation

The candidate was prepared against branch `feature/core-runtime-decoupling-20261105`, base HEAD `35f5e7831b95084ba6cb23c3f999e9cf372d271e`, upstream main `6f78ac7f45b25111e68a737a5e6588152341b6a2`.

Eight changed/new files are preserved as a review patch and exact source snapshot in ignored Agent diagnostics:

- Protected runtime contract: explicit Core-host/Functions roles, exact same-role comparisons and distinct cross-role admission.
- Empty, explicitly checked Core-host dependency lock; Functions retains every original applicable standard dependency.
- Independent Python patch/build policy by role, with runtime policy identity separated from protected code/protocol identity.
- Trusted-installation manifest verification: exact environment and base file closure, executable paths/hashes, package metadata, strict schema/types, forbidden path/startup injection, site isolation and source/ABI checks.
- Derived immutable installation identity excludes Core code/protocol qualification. New Core qualification does not renumber unchanged Function-runtime bytes.
- Regression tests and consistent unpublished `5.0.0a1` / `5.0.0-alpha.1` candidate metadata.

This checkpoint only adds the contract/admission primitives. Trusted installation selection and child-observed nonce/PID/FG binding, the shared cross-interpreter launcher and real split-runtime operation are still future implementation. The existing launcher was not migrated. It is NOT a claim that full Acceptance Gate 1 or Gate 2 has passed.

## Exact review identity and validation

- Tested source-tree SHA256: `4e3580fd6ce16b9c42ea01b2caeff951dcc91b0b50605b2eef5ca4d5e88e695b`.
- Canonical candidate protected-Core review digest: `sha256:c791e148b711e3a8916e9b2fc304575806dba609122eedf1ce58ec9125111516`.
- Review patch SHA256: `d17aa182d47f880d9f8d5af46427406af51d292e090f9af0f9814f1c8225e611`.

The exact candidate passes **985 cases, 1 skipped** across preserved JUnit/command receipts. The unchanged 39-module curated Core gate remains **881 passed, 1 skipped**; extra cases cover the owning runtime/Portable/guard selectors and 48 new role/installation cases. Counts overlap by scope and must not be added again. Four separate test processes have identical before/after source-tree identities and distinct temporary roots.

A real clean Windows CPython environment imports the protected Core modules with zero application distributions. Native existing process/IPC/adoption/durable-progress/rollback preparation tests still pass. Manifest admission fixtures use synthetic, never-executed executable files; they are NOT installed-runtime acceptance.

Failure receipts are retained: missing role/admission APIs; the clean-source fixture initially exposed unrelated build metadata; startup-injection and duplicate JSON members were accepted before their fixes; a Core-only Python build date initially changed Functions identity. The latter three behavioral failures were reproduced before correction. Three separate temporary guard mutations each make the relevant real negative assertion fail; product source is unchanged by the mutation proof.

Original dependency versions remain intact. No #137 security-version upgrade, provider call, real frontend/device acceptance, L2/WAC qualification or production adoption is claimed.

## Safety correction and restored active checkout

The initial environment check found a clean idle HASHI5 source checkout and healthy sealed running Functions. A later check identified that the active Core's cold/crash recovery still executes protected bootstrap files from that checkout. The new role contract rejects the old Core's bootstrap fingerprint because it lacks a role. Leaving an incomplete Core-major candidate there would undermine its recovery path even while current PIDs remained healthy.

The initial safety assessment was therefore incomplete. Before waiting for review, every owned edit was preserved and verified, then the active checkout was restored. All original protected-Core bytes, running Core/Shared/Agent/Remote PIDs and generations, installed dependencies and standard-lock bytes match the starting baseline. No existing service was restarted; durable user/task state and active environments were not changed.

Only documentation checkpoints are left/published on this branch in HASHI5. The eight-file implementation patch remains uncommitted and is not described as delivered executable source. The old general HASHI checkout has unknown changes and was not reused or cleaned; other development/qualification instances remain running or reserved.

## Unresolved gates and next action

1. An independently isolated, non-running development checkout is required before resuming protected Core edits. The current instruction forbids creating additional worktrees without explicit authorization; none was created to bypass that constraint.
2. The legitimate Core-major source gate rejects this checkpoint until a different reviewer adds a matching independent record. No approval record, actor, label or hook bypass was manufactured.
3. After these prerequisites, apply the preserved candidate to the same approved branch, repeat appropriate native checks for that actual checkout, obtain its exact-tree review and make the tested gate commit.
4. Continue the documented launcher/isolation/parity/security/packaging phases. Gate 3 original-dependency full Function parity, same-Core-PID FR upgrades/rollback, complete release CI and operator-approved adoption remain unverified. Do not merge #137 as-is.
