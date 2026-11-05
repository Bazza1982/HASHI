# PR #133 Windows CI contention and watchdogs

## Final native read: remove unchanged-state durable writes

At `0484226bbf757d550730deef2a4b2e25a62d55a9`, PR run `42024751560` passed on both platforms, but the independent Windows push run `42024742848` failed the original bound-read case at its unchanged ten-second contract watchdog (1249 passed, 17 skipped, one failure). Its preserved trace waited for the synchronous file supervisor to settle after cancellation. It has no inner-thread transaction timing; the exact hosted storage/scheduling event remains unmeasured.

The source rewrote both identical trusted-time floors and the continuity head during status-only reads. A controlled run delays each real ledger commit by 900 ms and keeps the original native read function, all its assertions and the same ten-second watchdog. Before correction it makes 28 commits and times out. After correction it makes nine commits and passes; that probe and the complete Windows supervisor module pass together (16 checks). These are controlled measurements, not reconstructed timings of the failed hosted runner.

A read now skips floor writes only when the exact durable value is unchanged. Every call still acquires the same FIFO transaction, checks the independent witness and live service/Run/permission facts, and reads current database state. If neither authority data nor trusted time changes, it releases that transaction without another commit or witness rotation. Any mutation or exact time advance still requires the original atomic FULL dual-database/witness commit. There is no time rounding, cache, batching interval, rate limit, retry or deadline extension.

Schema initialization explicitly commits because SQLite row-change counts do not include DDL or user_version updates. Rejected transactions compare row changes after their business rollback before persisting new time. A real witness-abort regression rejects the old read-only flush, while also proving that revocation or a new clock floor cannot succeed when their witness write fails. Successful and rejected reads with new time both persist the floor and reject a later rollback/restart.

The existing FIFO test now arranges a real new clock floor before holding its first durable commit; the later unchanged-time denied poll needs no write. The stale-maintenance test likewise creates a real persisted time revision, retaining its original conflict and atomicity assertions. No original failure is overwritten or called a pass.

## Final integration: fingerprint receipt verification

At integrated source `80ac21876b7011fc7dca5a2c5afe9dd804f21a2b`, GitHub run `42019903648` failed the Windows fingerprint receipt-only integration at its two-second test watchdog. The retained native runner trace reached `PermissionExecutionGate._verified_receipt` and was cancelled while reading back the committed operation; it did not report an additional file effect or a permission rejection. Its exact host storage/scheduling event is not reconstructable from that log.

A controlled local arrangement delayed each real ledger commit by 250 ms, retaining the actual SQLite transactions, witness commits, file operation and all original assertions. Both enabled-policy and invalid-schema variants failed at the same receipt readback with the original two-second watchdog; 50 delayed commits across the two cases took about 14.18 seconds in total. This reproduces the mechanism without claiming to measure the historical runner's disk latency. Reusing the existing shared permission-contract deadlock watchdog passed both variants and the entire fingerprint integration module (10 cases); 52 real delayed commits took about 14.50 seconds. No product expiry, cancellation ordering, SQLite durability, policy, rate limit, effect, scope or fingerprint assertion changes.

Owner: PAO. Engineering layer: replaceable Functions and their contract tests.
Source integration remains separate from L2 release qualification.

## Evidence and scope

The original run 41993486993 at 9310217515f1197cc8523355a5334f9d24afdef5 is retained:
attempt 1 failed the bound-file read's three-second wait; attempt 2 failed the
concurrent presentation at SQLite BEGIN IMMEDIATE and the encrypted-result
recovery's one-second wait. Their assertions, logs and job identities were not
discarded or reclassified as passes.

A temporary, parameter-free SQLite observer was pushed in 84dc7168d75e1dc28fdec9d5a58ea93633079629.
It retained every original test and wait, added no retries or sleeps, and logged
only connection roles, transaction phases, caller names, counts and elapsed time.
It did not log SQL, arguments, results or credentials. Run 42000170919 passed
both jobs: Windows 924 passed / 3 skipped; Ubuntu 874 passed / 53 skipped.
This successful diagnostic run is evidence about timing, not proof that the
intermittent failures were fixed.

On that Windows runner, the read scenario used 29 authority commits, totalling
1.727 seconds; individual commits reached 86 milliseconds. The recovery scenario
used 38 authority commits, totalling 1.897 seconds. Eight concurrent presentation
calls spent up to 432 milliseconds acquiring a SQLite transaction. The Ubuntu
recovery used about 49 milliseconds for all authority commits. Whole-scenario
times include setup and replay and must not be compared directly to the
post-approval watchdog.

The earlier failing jobs did not retain per-transaction timing. We cannot
retrospectively identify their exact host storage stall or scheduling event.
The two reproducible failure mechanisms below, rather than a guessed host
fault or an unexplained green rerun, are the basis for the fix.

## Service-owned writer queue

Previously every thread in the same PermissionAuthority opened a connection and
competed for SQLite's ten-second writer budget. A deterministic regression holds
a real first transaction before its commit and issues a second identical
presentation, with SQLite's external-writer wait disabled for the test.
The old implementation raises SQLITE_BUSY at BEGIN IMMEDIATE, matching the
reported failure boundary, although no independent writer exists.

The authority now queues its own transactions before opening a connection.
The first commit, rejection rollback and durable trusted-time recovery all
remain inside that queue. The second caller waits and returns the same
committed action; neither presentation creates a grant.

SQLite BEGIN IMMEDIATE, its external-writer timeout, independent service
generation fencing, FULL synchronization of both rollback-journal databases,
continuity checks and witness advancement are unchanged. This is not a global
lock across unrelated authorities or a retry of a failed transaction. The
existing PAO Run / independent policy-reader lock ordering is preserved.

The new contention test fails before this change and passes afterward.
Related action and policy-lock checks pass as well.

## Integration watchdogs and authority deadlines

The one- and three-second waits were test deadlock watchdogs, not permission
expiry or cancellation requirements. They could cancel a correct operation
while Windows was still persisting its result and cleanup proof.

The two affected modules now use the existing ten-second contract watchdog,
as the other ledger/dispatch integration tests already do. Controlled delays
of 350 milliseconds per real ledger commit for the read and 150 milliseconds
for result recovery reproduce both original TimeoutErrors. With the corrected
watchdogs, both original test functions pass with all assertions unchanged.
The combined slow-storage, writer-queue, installed-policy and lock-order
validation passes 23 cases.

Product lease/action expiry, clock rollback detection, revocation checks,
cancellation ordering, stop evidence, encrypted receipt validation and
single-effect replay assertions were not extended or removed. Existing native
cancellation cases still require the operation to settle before returning.

A local diagnostic initially put its test business directories inside this
checkout. The protected-source policy correctly rejected those writes.
That arrangement is unsuitable for the installed-policy positive cases; the
same cases pass with an operating-system temporary root outside protected
source. The original eight refusals are preserved, and no protected path was
removed to make them pass.

## Continuing checks and release boundary

The temporary observer is removed after its raw results were retained. CI keeps
the standard slow-test summary and now explicitly includes the writer-queue,
policy/Run lock-order and remote-presentation regression modules in lint and
test selection.

Final committed-HEAD results and the exact SHA belong to the PR review receipt.
Earlier Intel receipts remain scoped to their recorded 93102175 capsule; they
must not be relabelled as a deployment or native acceptance of a later commit.
The Draft PR and L2 HOLD remain until the outstanding governed frontend and
formal WAC release cases are qualified.
