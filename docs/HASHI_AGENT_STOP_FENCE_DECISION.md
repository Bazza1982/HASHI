# HASHI Agent Stop Fence Decision

Status: approved implementation.

Approval: the user approved this Agent-level stop design on 2026-10-14.

Implementation: HASHI3 branch `debug-stop`; PAO and its Function consumers.

Live verification: HASHI3 adopted implementation commit `6c12e927` through
`/reboot max` on 2026-10-14. The shared Function and all eleven Workers moved
to the new generation while protected Core stayed live. A real Backend API
canary created one enabled heartbeat and one FUTURE delayed message for an
otherwise idle test Agent. `/stop` removed the delayed message, retained the
heartbeat definition as enabled, and advanced the Agent-level stop. The
temporary heartbeat was then deleted and the test Agent returned to zero
scheduler jobs.

Owner: PAO runtime control with Frontend Connector, Scheduler, and
BackgroundJobManager consumers. HER v2 continues to own its Turn internals;
protected Core is not changed.

## Decision

`/stop` means an immediate Agent-level stop, regardless of which supported
frontend or Session issued it. It does not mean “clear only this chat”. One
operation:

- advances a durable, per-Agent stop epoch;
- interrupts all known foreground and detached requests for that Agent;
- clears its READY and FUTURE request items;
- cancels its retry/detached helper tasks and non-terminal managed OS jobs;
- leaves cron and nudge definitions intact; and
- records one receipt identifying the epoch, source, reason, and active request.

Every managed background job and nudge invocation captures the epoch at which
it starts. A completion, retry, or callback from an older epoch is retained as
audit evidence but cannot enqueue a new Agent request, deliver a completion
notification, or disable a current nudge definition.

Cancelling an active Session execution must pass through the canonical terminal
writer before its in-memory metadata is removed. The Run becomes `stopped`,
the `run.stopped` event and projection are committed, and a Workbench refresh
or reopen reads the terminal state. On Worker adoption, the latest durable
user-stop receipt also repairs legacy queued/running Runs admitted before its
stop time; the receipt's named request remains authoritative when timestamps
share the same clock tick. Requests admitted after the stop retain normal
restart reconciliation.

Stop success also requires canonical readback of affected Session Runs. If a
terminal write fails before commit, the Run cannot be found, or readback fails,
the command reports that stop is unconfirmed instead of claiming success. A
write that reports an error after commit is read back without retrying it;
only a confirmed `stopped` record permits the normal success receipt.

The stop-fence store closes each SQLite connection after its transaction,
including initialization, reads and failed writes. Transaction rollback alone
does not release a connection; cleanup cannot rely on Python garbage collection
to release a Windows database handle.

The stop operation removes only work admitted before the new epoch. A genuine
new request or background job admitted after the fence is not consumed by the
still-finishing cancellation pass. Interrupted-task snapshots remain scoped to
their original Sessions so each affected Session can be resumed explicitly.

Nudges use one persisted invocation lease per nudge. Admission reads the live
Worker metadata, detached request count, queue depth, and managed background
jobs. A second invocation is not created while the first lease is active. An
orphaned lease may be released only when its request is no longer outstanding
and the runtime is idle; a changed stop epoch always invalidates the old lease.

Legacy WIP remains bounded recovery evidence and may still be projected into
the internal recovery context. It is quiet by default. A user-visible recovery
warning requires an explicit typed request flag; ordinary later messages do not
receive a recovery card merely because old WIP exists.

## Non-goals

This decision adds no token, time, Tool, context, or loop limit; no automatic
checkpoint or compression; no shell reclassification; no Provider retry
policy; and no broad HER v2 execution rewrite. It does not delete scheduler
definitions and never stops another Agent.

## Required evidence

Focused tests cover cross-Session stop, READY/FUTURE removal, detached request
markers, managed-job cancellation, stale completion suppression, nudge lease
mutual exclusion, stop-epoch persistence/recovery, terminal state after
refresh/reopen, legacy non-terminal repair, quiet WIP, and existing HER Turn
ownership conflicts. Protected Core checks remain mandatory.

## Independent pre-merge review (2026-10-14)

The review on HASHI3 `debug-stop` found two release blockers in the original
implementation:

- Stop could finish while a managed process was being created; the late
  process handle then overwrote the cancelled record with `running`. Cancelling
  the start caller during creation could also leave a child process unowned.
  Launch ownership now survives caller cancellation, the stop waits for the
  handle and reaps the child, and the launch rechecks the persisted stop epoch.
- The stop pass used a 1,000-item display limit. Through the real Worker service
  facade, a fixture with 1,001 older owned jobs plus newer/other-Agent jobs
  cancelled only 999. Lifecycle callers now request a complete snapshot;
  the epoch and Agent filters still protect new work and other Agents.

Three real-process regression cases failed before the launch fix and passed
after it. The large-job-set case failed before the list fix. Existing success
and failure event ownership/idempotency assertions have also been restored.
The FYI summary was shortened with a link to the complete Call decision so
the added stop guidance does not truncate unrelated engineering guidance.

Initial curated Core-gate run: 811 passed, 1 skipped, 3 failed because the
Function qualification checks correctly rejected the uncommitted review fix.
Committed implementation `48d1764d` then passed the curated Core gate:
814 passed, 1 skipped (the POSIX permission-mode case is inapplicable on
Windows). This includes Function generation, Worker supervision, shared
handoff and real isolated product preparation. Lint also passed. The three
uncommitted-source failures were cleared without weakening the gate.
The final focused run passed 118 cases, including four new regressions and
the restored event assertions. Protected Core and whitespace checks also
passed.
These are offline checks. The HASHI3 live canary recorded above exercised
`6c12e927`, not the subsequent review fixes; it is not evidence of HASHI4
adoption. Source deployment and a separately authorized Function adoption
remain distinct.
