# HASHI Agent Stop Fence Decision

Status: approved implementation for the HER Turn/WIP/`/stop` follow-up.

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
mutual exclusion, stop-epoch persistence/recovery, quiet WIP, and existing HER
Turn ownership conflicts. Protected Core checks remain mandatory.
