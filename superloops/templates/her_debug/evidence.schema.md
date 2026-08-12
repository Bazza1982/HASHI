# HER Debug Superloop Evidence Contract

## Campaign ledger

The campaign ledger expands every required combination and never relies on a
summary count alone. Each work item records:

```text
work_item_id
stage
provider and exact base-route family
model slug
mode
effort
wave
scenario
presentation policy
candidate SHA-256
status
current attempt ID
final verified attempt ID
stale reason, when invalidated
```

The ledger must reconcile to 24 Flash core cells, 24 Pro core cells, 480 core
scenario groups, and 384 presentation runs, plus the plan's boundary,
continuity, endurance, restart, and fault work.

## Dispatch and follow-up ledger

Every Ajiao dispatch is correlated to exactly one work item and attempt:

```text
dispatch_ref
controller_agent: lin_yueru
worker_agent: ajiao
worker_status
expected_receipt
dispatched_at
last_progress_at
last_monitor_observation_at
follow_up_after
follow_up_count
follow_up_refs
terminal_receipt_ref
```

A nudge wake while `worker_status=running` records only a monitor observation
and the next check. It cannot record `/stop`, cancellation, restart,
reassignment, or another active dispatch.

A failed worker reply records the raw receipt, classification, preserved
partial evidence, and a correlated follow-up. It cannot set the campaign to a
terminal state or leave `next_action` empty.

## Test evidence

Each paid or offline test attempt links the files required by the authoritative
plan, including the run manifest, redacted provider trace, HER JSONL, HASHI
events, tool audit, delivery transcript, before/after workspace manifests,
acceptance log, and assertion-level verdict.

Live manifests must contain one of the exact stage-allowed route/model pairs.
Any other live API/model is a stop-the-line violation, not fallback evidence.

## Defect cycle

For a HER/HASHI defect, preserve:

```text
journal entry ID opened before product repair
first failing event and immutable evidence bundle
bad candidate identity
regression that fails on the bad candidate
root cause
repair refs
new candidate identity
exact retest attempts
blast-radius retest attempts
completed bug-and-fix journal fields
candidate evidence invalidated by the repair
```

The same work item remains outstanding until the required retests pass.

## Funds evidence

`BLOCKED_FUNDS` requires:

```text
required route
explicit insufficient-funds response
read-only balance result or bounded same-route confirmation probe
redacted timestamps and request IDs
completed work-item ledger
complete list of unrun work items
statement that no fallback API/model was used
```

Rate-limit, timeout, DNS, authentication, and generic server errors cannot
populate this terminal record.

## Final evidence

`PASSED` requires the final candidate identity, complete Layer A verdict, both
24-cell stage gates, every ancillary suite, full HASHI/HER regression results,
all affecting journal entries verified, Ajiao restoration evidence, and a
drained/classified reply ledger with no active dispatch, wait, or blocker.
