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
feature_profile (`core_off` or `habit_on`)
habit_scenario (`none`, `habit_wire`, `habit_deep`, or `habit_fault`)
wave
scenario
presentation policy
candidate SHA-256
status
current attempt ID
final verified attempt ID
stale reason, when invalidated
```

The ledger must reconcile to 12 official-DeepSeek Flash `CORE-OFF` cells, 12
`HABIT-WIRE` cells, two `HABIT-DEEP` cells, one `HABIT-FAULT` cell, 120 core
scenario groups, and 96 core presentation runs, plus the plan's
offline, boundary, continuity, endurance, restart, migration, and fault work.

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

Every `CORE-OFF` attempt also proves the raw and executed prompts are identical
and that no Habit directory, Meditation journal entry, or Meditation model call
was created. Every `HABIT-*` attempt additionally records configuration source,
raw and executed prompt hashes, selected Habit IDs, foreground and Meditation
session IDs, exact route/model, permission mode, journal timeline and attempt
count, durable actions hash, Habit/Dream before-and-after inventories, and the
count of user-visible background events.

Habit formation, retrieval, and behavioral use are three separate claims and
must never be inferred from one another:

```text
formation_observed
formation_evidence_refs
retrieval_observed
retrieval_evidence_refs
behavioral_use_observed
behavioral_use_evidence_refs
no_change_claim_limit_acknowledged
foreground_lock_wait_ms
foreground_lock_wait_limit_ms
```

`formation_observed=true` requires a durable non-empty Meditation action and a
reconciled Habit inventory change. `retrieval_observed=true` requires the exact
Habit ID in both the selection ledger and executed planning context.
`behavioral_use_observed=true` requires a predeclared observable next-request
output or tool-side-effect assertion that matches the Habit while remaining
subordinate to the current request. A selected ID, changed prompt hash, or model
assertion alone is not behavioral-use evidence.

A terminal `no_change` proves only that the Meditation wire, isolation, journal,
and silence contract completed. It is not evidence of formation, retrieval, or
behavioral use. Such attempts set the unsupported observations to `false` or
`null` and set `no_change_claim_limit_acknowledged=true`; they may satisfy
`HABIT-WIRE`, but cannot satisfy a designated `HABIT-DEEP` lifecycle packet.
Every `HABIT-DEEP` lifecycle packet must link evidence for all three observations
as `true`. `HABIT-FAULT` additionally records the measured foreground lock wait
and its predeclared upper bound.

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

`PASSED` requires the final composite candidate identity (HASHI commit/build,
HER source/package, and oracle hashes), complete joint Layer A verdict, the
12-cell core gate and Habit gate, every ancillary suite, full HASHI/HER regression results,
all affecting journal entries verified, Ajiao restoration evidence, and a
drained/classified reply ledger with no active dispatch, wait, or blocker.
