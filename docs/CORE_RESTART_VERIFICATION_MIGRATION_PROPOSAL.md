# Proposed Core verification optimization — not implemented

> Deferred historical proposal: Core v5 is frozen by the current owner decision.
> This proposal is not authorized and is not the current implementation plan.
> Local-readiness optimisations are confined to Functions/platform code.

Owner: Core runtime / installer. This is a reviewable proposal only, independent
of the Functions, platform and Workbench candidate. No protected Core source,
version, qualification policy or production runtime is changed.

## Measured problem and accounting

The prior same-candidate cold run took 182.405 seconds; Agent startup occupied
88.378 seconds. Twelve parent launch paths independently loaded the same
qualified runtime, and child admission repeated its own independent validation.
Observed parent calls overlapped and included contention. Their inclusive wall
times cannot be summed into a saving estimate. The Windows task polling change
is measured separately and does not justify removing runtime verification.

The Functions observer sees process creation and first Functions entry, but
cannot split the earlier Core bootstrap/import/qualification interval. Report
that interval as composite until Core's own spans exist.

## Concrete change to review

1. Add bounded monotonic spans in Core around runtime descriptor loading,
   installation/identity validation, interpreter/base-file validation, Core
   fingerprint validation, Function artifact admission, spawn and child handshake.
   Record operation/process IDs and counts, never credentials or file contents.
2. Within one explicit launch batch, verify the qualified runtime once and pass
   the sealed verified runtime value to each parent launch. Reuse only the
   expensive invariant environment proof. Retain per-Agent generation selection,
   PID/nonce, contract, instance and fresh child readiness checks.
3. Retain each child's independent runtime and artifact admission. No process
   executes rejected or changed bytes. Do not substitute mtime/size alone for
   byte validation, reuse proofs across operations or globally memoize them.
4. Bind any reusable proof to canonical installation root, runtime identity,
   interpreter identity, dependency/Core contract and immutable artifact. Define
   invalidation and failure behavior explicitly before implementation. A changed
   installation or descriptor must require fresh verification, not silent reuse.
5. Publish this as a complete Core major-version migration with installer assets,
   contract/version changes, tests and docs at one verified commit. Do not copy
   a patch into a running production Core.

## Acceptance and independent review

Explicit migration authorization and independent Core review are prerequisites.
Review must cover mutation before/after admission, replaced interpreter/base
files, changed dependencies/Core source, redirected roots, old receipt reuse,
wrong instance, generation/nonce mismatch, one failed child, rollback, shutdown
and cold startup on native Windows and Linux. Admission failures remain closed.

Compare identical builds, warm/cold cache separately, fixed Agent count and idle
host. Join Core spans to Remote receipts and browser paint. Report batch hash
counts, CPU and critical-path wall intervals, with overlapping work separated.
Only measured end-to-end improvement establishes a performance gain; existing
safety gates and readiness proofs must all continue to pass.
