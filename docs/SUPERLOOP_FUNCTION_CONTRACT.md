# HASHI Superloop Function Contract

Status: v4.0.0-alpha.1 operational contract.

This document defines the minimum runtime contract for a HASHI Superloop to be
treated as a runnable controller loop. It complements `SUPERLOOP_PLAN.md`, which
remains the long-form architecture record.

Superloop alpha is not a stable unattended automation product. A loop may run
real work only when it preserves explicit state, waits, evidence, issues, and a
closeout barrier that can be inspected after interruption or restart.

## Scope

A Superloop is a controller-level orchestration run that can coordinate agents,
files, reviews, waits, and child workflows over time.

It is not:

- a single agent prompt;
- a passive HChat thread;
- a replacement for Nagare's bounded workflow runner;
- a hidden state machine stored only in one agent workspace.

The shared source of truth lives under:

```text
superloops/loops/<loop_id>/
```

## Required Files

Every runnable loop must have:

```text
state.json
taskboard.json
issues.json
waits.json
events.jsonl
README.md or operator_summary.md
```

Templates may add role files, evidence logs, manifests, child-flow definitions,
or domain-specific artifacts.

## State Contract

`state.json` must include at least:

```json
{
  "loop_id": "sl-...",
  "status": "running",
  "current_step": "step-001",
  "next_action": {
    "kind": "run_task",
    "task_id": "step-001"
  },
  "active_wait_id": null,
  "taskboard_path": "superloops/loops/<loop_id>/taskboard.json",
  "issues_path": "superloops/loops/<loop_id>/issues.json",
  "waits_path": "superloops/loops/<loop_id>/waits.json",
  "events_path": "superloops/loops/<loop_id>/events.jsonl"
}
```

Allowed `status` values:

```text
draft
running
waiting
blocked
paused
completed
aborted
failed
```

The runner must reject or block advancement when `current_step` or
`next_action.task_id` does not resolve to an existing task.

## Taskboard Contract

`taskboard.json` is an array of task objects. Every task must use `task_id`.
Do not use `id` as the task identifier.

Required fields:

```json
{
  "task_id": "step-001",
  "title": "Short task title",
  "status": "pending",
  "owner_agent": "zelda",
  "depends_on": [],
  "required_evidence": []
}
```

Allowed task `status` values:

```text
pending
in_progress
waiting
blocked
completed
skipped
failed
```

At most one task should be `in_progress` unless the loop explicitly records
parallel ownership and disjoint write scopes.

## Wait Contract

`waits.json` is an array of explicit wait records. A loop must not wait
implicitly in chat or in a silent sleep.

Required fields:

```json
{
  "wait_id": "wait-001",
  "kind": "await_hchat_reply",
  "status": "open",
  "related_task_id": "step-003",
  "target": "akane",
  "entered_at": "2026-05-23T23:00:00+10:00",
  "follow_up_after_minutes": 5,
  "deadline_at": "2026-05-23T23:15:00+10:00",
  "resume_policy": {
    "on_satisfied": "advance_task",
    "on_timeout": "raise_issue"
  }
}
```

Supported alpha wait kinds:

```text
await_human
await_hchat_reply
await_protocol_reply
await_file
await_remote_online
await_issue_resolution
await_child_run
sleep_until
```

When a wait times out, the controller must either run the configured
controller-side probe, open an issue, ask a follow-up question, or pause the
loop. It must not silently continue.

## HChat And Reply Handling

HChat is a transport, not the state layer.

When a worker, reviewer, remote peer, or subject agent replies:

1. Record the raw reply reference or excerpt in loop evidence.
2. Classify it as:
   ```text
   current_evidence
   superseded_evidence
   contradiction
   new_blocker
   stale_reply
   unrelated
   ```
3. Update the related wait, issue, and taskboard entry.
4. Advance the loop only after the task evidence requirements are satisfied.

Do not close a loop while relevant HChat replies are queued or unclassified.

## Issues Contract

`issues.json` must track blockers separately from task status.

Required fields:

```json
{
  "issue_id": "sli-...",
  "severity": "medium",
  "status": "open",
  "title": "Short issue title",
  "related_task_ids": ["step-003"],
  "created_at": "2026-05-23T23:00:00+10:00"
}
```

Allowed `status` values:

```text
open
in_progress
resolved
waived
stale
```

Open blocker issues prevent closeout.

## Dispatch Interlock And Pause Contract

Every outbound worker, model, or protocol packet must pass the shared dispatch
interlock immediately before transport acceptance. The transport must hold the
loop dispatch lock until the packet is rejected or both accepted and durably
recorded. A task changing to `in_progress` is not permission to skip this gate.

Dispatch is blocked when any of these is true:

- the loop is not `running`;
- a persisted or file-backed pause/halt signal exists;
- the current phase has an open issue marked as a dispatch blocker;
- a frozen candidate is explicitly invalid, stale, superseded, or invalidated.

Empty template candidate placeholders do not block preflight work. Once a
candidate has an identity or the loop declares `candidate_required_for_dispatch`,
an explicit invalid state fails closed.

Pause is a persisted state transition performed while holding the same dispatch
lock. It must set `state.status=paused` before returning and record:

```json
{
  "control": {
    "requested_action": "pause",
    "pause": {
      "mode": "drain",
      "requested_at": "...",
      "active_request_ids": [],
      "drain_complete": true,
      "resume_action": {"kind": "run_task", "task_id": "step-001"}
    }
  }
}
```

`drain` and `immediate` both stop new dispatches at once. They differ only in
how the executor handles a request that was already accepted. Resume is an
explicit transition and must fail closed while draining is incomplete, a pause
signal file remains, an open phase blocker exists, or the frozen candidate is
invalid.

## Events Contract

`events.jsonl` should record controller-significant transitions:

```text
loop.started
task.started
task.completed
task.blocked
wait.entered
wait.satisfied
wait.timeout
issue.opened
issue.resolved
hchat.reply_classified
loop.paused
loop.resumed
loop.completed
loop.aborted
```

Each event should include timestamp, loop id, related task/wait/issue id when
applicable, actor, and a short evidence summary.

## Closeout Barrier

Before setting `state.status=completed`, the orchestrator must:

1. Confirm every required task is `completed` or explicitly `skipped`.
2. Confirm no blocker issue is open.
3. Confirm every wait is satisfied, cancelled, or explicitly waived.
4. Drain recent HChat/protocol replies for the loop id.
5. Classify late replies as current, stale, contradiction, new blocker, or
   unrelated.
6. Reopen or pause the loop if a late reply introduces a blocker.
7. Record the checks and final evidence in the operator summary.

Closeout without inbox drain is invalid.

## Validation Gates

Before claiming Superloop functionality in a release, run:

```text
python -m pytest tests/test_superloop_store.py tests/test_superloop_taskboard.py tests/test_superloop_waits.py tests/test_superloop_runner.py tests/test_superloop_control.py tests/test_superloop_dispatch.py tests/test_superloop_validator.py tests/test_superloop_scheduler.py tests/test_superloop_compiler.py tests/test_superloop_issues.py tests/test_superloop_commands.py tests/test_superloop_recording.py tests/test_superloop_nagare_adapter.py -q
```

For live or template validation, record at least:

- loop id;
- template used;
- taskboard path;
- waits path;
- issue path;
- evidence path;
- worker/reviewer dispatch evidence;
- wait satisfaction or timeout handling;
- closeout barrier evidence.

## Liveness Without Scheduler-Owned Task Starts

A loop may require active supervision without granting the background scheduler
authority to start work.

Use these as separate controls:

```text
scheduler_auto_advance = false
idle nudge or explicit wakeup = enabled
```

With scheduler auto-advance disabled, an idle scheduler must not change a
pending task to `in_progress`. An idle nudge may enqueue a continuation prompt
to the orchestrator, but the nudge itself must not mutate the taskboard, waive
evidence, operate a GUI, or declare a gate passed.

After waking, the orchestrator must inspect state, tasks, waits, issues and
evidence. It may then explicitly continue an existing task or start the next
eligible task. This gives a long-running loop liveness without allowing
background idleness to become execution authority.

## Alpha Wording

Acceptable release wording:

```text
Superloop operational foundation
Superloop templates and function contract
Controller loops with explicit taskboard/wait/evidence state
```

Do not claim:

```text
stable unattended automation
fully autonomous superloop production release
human-free long-running execution
```

## Controller receipt follow-through (2026-09-08)

Owner: PAO; engineering placement: Functions, with Remote as the replaceable
adapter. Receipt admission must remain distinct from execution, independent
review, next-action disposition, runtime adoption and user delivery.

The existing Remote tick reconciles persisted `receipt_reviews.json` against
the owner-scoped request activity API, even after transport receipt cleanup.
It preserves the original admission status and records observed execution and
`followthrough_state` separately. Unavailable or mismatched activity is unknown,
not successful execution. Failed/cancelled runs require explicit attention and
are never automatically replayed.

A successfully completed controller run with missing review evidence or missing
whole-board dispositions receives at most one logical recovery Run. Its exact
request, original Session and suffixed idempotency key are persisted before
admission. Response-loss retries reuse that request. Recovery cannot recursively
create recovery; pause, stop and opt-out block admission. Exhaustion remains
`needs_attention` for the existing controller/maintenance review, without adding
another recurring scheduler job.

The original review row records `review_verified`, a loop-local existing
`review_evidence_ref`, and `dispositions` covering every nonterminal board task:

- `active_dispatch`: task ID, accepted nonterminal dispatch ID and a local
  execution-observation `evidence_ref`;
- `action`: task ID and local action `evidence_ref`;
- `blocked` or `deferred`: task ID, concrete reason, responsible owner and release
  trigger (including intentional priority/capacity deferral).

These checks detect omissions and absent evidence. They cannot judge whether a
human or agent's evidence is truthful, whether a blocker is justified, or whether
an accepted dispatch really executed. The controller must independently verify
those facts. A reviewed row is historical evidence for that turn. The latest queued review
continues supervising the current whole board: newly added/reopened tasks, lost
evidence and expired structured waits invalidate the old disposition. Historical
rows do not fan out recovery. Existing heartbeat supervision remains the
fallback after the single recovery budget is exhausted. Scheduler auto-advance still does not
dispatch work and must not be enabled as a substitute for controller execution.


### Explicit delivery closeout contract (2026-09-08)

An outcome task may opt in with `delivery_required: true`. The initial contract
below is historical; the scoped acceptance contract later in this document
supersedes its Boolean/file-presence completion path. Initially, when such a task was
`completed`, its taskboard row must contain `runtime_adoption_verified: true`
and `user_acceptance_verified: true`, each with a corresponding
`runtime_adoption_evidence_ref` / `user_acceptance_evidence_ref` naming an existing
file inside the loop. User acceptance here means an independently recorded
functional check of the requested behavior, not merely a passing code test.
Tasks whose outcome includes a visible message also set
`terminal_delivery_required: true` and require `terminal_delivery_verified: true`
with `terminal_delivery_evidence_ref`. Message delivery and functional acceptance
are distinct evidence. Ordinary code-only tasks and existing boards without this
explicit opt-in retain their prior completion contract. Cancelled, aborted and
failed tasks are not reopened by this check.

Receipt reconciliation reports missing outcome fields as `task_id:requirement`
gaps, including for the latest previously `reviewed` row. Historical reviewed rows do
not each fan out recovery for the same current gap. The latest row is re-observed through
the identity-checked activity API when any current whole-board gap exists; the original one-logical-recovery allowance still applies. Deleting
previous evidence cannot reset that allowance. Failed or cancelled controller
runs never auto-recover. The controller must correct a premature completion,
then record actual action or a concrete blocker; the service does not rewrite
task status or autonomously dispatch the original work.

This validates presence and scope of evidence, not its truth. The responsible
controller must independently inspect generation identity and the real user
scenario. A loop without a persisted receipt review is still handled by its
existing controller/maintenance trigger; this change creates no new scheduler.

Implementation scope: authorized PAO Functions maintenance branch
`fix/superloop-delivery-evidence-20260908`. Offline receipt-service tests use real
temporary SuperloopStore persistence, including restart, evidence loss and the
single recovery boundary. Instance adoption and live delivery acceptance remain
separate, unverified release steps for this branch.


### Continuous next-step supervision (2026-09-08)

A loop can set `continuous_supervision_required: true` to require a next step
for unfinished tasks. An `action` still records work performed, but must also
contain `next`, an `active_dispatch` or `blocked`/`deferred` disposition. It
cannot recursively point to another action. A local action file alone cannot
exempt an unfinished task indefinitely. Blocked/deferred dispositions retain
reason, owner and trigger and add `review_after`, finite absolute UTC epoch
seconds. Active dispatch dispositions also require this deadline: an accepted
ledger row and old execution file cannot certify continuing activity indefinitely.
On expiry the controller must inspect actual current activity and renew the
observation or take the next authorized action; the service never resends the
original assignment. The timestamp is a reassessment deadline, not restart permission or a
new Scheduler job; use existing receipt/deadline/maintenance opportunities.
Expiry becomes a review gap. Explicit invalid or expired timestamps are rejected
even on legacy loops; loops without the opt-in retain existing action contracts.

The single persisted recovery allowance does not reset after board changes,
wait expiry, restart or action completion. Exhaustion records `needs_attention`,
`attention_owner` and `attention_trigger=existing_controller_or_maintenance_review`.
This is inspectable state for the existing supervisor, not a delivered alert.
Failed/cancelled runs are not replayed and pause/opt-out still stop admission.

Controller reporting starts with the user's requested outcomes, remaining
impact, actual next action/owner and blocker/release condition. Execution activity
is not Connector delivery evidence: the current activity API has no canonical
`delivery_event`, and the compatibility transcript API reads the default Backend
API Session message projection, not the exact controller Session delivery audit.
Reconciliation therefore records `report_delivery_state=unverified` with an
explicit reason. It does not invent a sent Boolean, accept a manually written
report file as transport proof, or rerun work because delivery is unverified.
Exact-request canonical Connector events still require independent inspection.

Implementation/verification: the 2026-09-08 Functions patch reproduces a reviewed
row ignoring reopened/new tasks and an old action file hiding an absent next
step; both fail before the fix and pass afterward against real SuperloopStore
persistence. Source tests do not prove running Remote adoption or user delivery.
Adoption and the complete receipt-to-visible-outcome scenario remain separate
release checks.

A follow-up real Store scenario leaves the accepted dispatch ledger and old
activity file unchanged, advances past `review_after`, and verifies exactly one
controller recovery without dispatching the original work again. It fails on the
prior implementation and passes with dispatch-observation expiry enforced.

### Scoped acceptance and manager outcome review (2026-09-08)

Functional owner: PAO. Engineering layer: Functions. This repairs existing
delivery supervision; it does not authorize product/Core edits, restarts or
new recurring schedules. `superloop_taskboard` is the single acceptance owner
used by task completion, loop closeout validation and receipt reconciliation.

Every `delivery_required` task defines `user_outcome` and a nonempty
`acceptance_checks` list **before dispatch**, with a separate check for each
required scenario and target scope. Each check contains `id`, `kind`, `scope`,
`scenario`, `expected`, and optional `prerequisites` (explicit dependency IDs).
Each check also declares `subject_version`, the exact intended revision or
generation; the observation must match it. IDs must be unique. The set includes `runtime_adoption` and `user_acceptance`,
plus `terminal_delivery` when required. Implementation, test, review and merge
checks can also be recorded separately. Scope must identify the relevant
instance, platform, Agents, direction and intended version where applicable.
The delivery owner must choose a complete user path, including applicable
failure/persistence boundaries. Do not redefine that path to fit passing tests.

`acceptance_results[check_id]` contains `evidence_ref`, its `sha256`, and
`reviewed_by` (qualified reviewer identity). The loop-local JSON evidence is:

```json
{
  "task_id": "delivery-task",
  "check": {"id": "preview", "kind": "user_acceptance", "scope": "source -> target", "subject_version": "candidate-123",
    "scenario": "Preview an existing Agent", "expected": "Preview succeeds; source is unchanged",
    "prerequisites": []},
  "result": "passed",
  "observer": "worker@instance",
  "observed_at": "2026-09-08T07:00:00+00:00",
  "subject_version": "candidate-123",
  "observed": "Actual operation and result",
  "artifacts": [{"ref": "original-observation.json", "sha256": "actual artifact SHA-256"}]
}
```

The `check` must exactly match the current declared check. Changing the scenario,
scope, expectation or prerequisites invalidates old results. The named reviewer
must differ from the observer; the controller independently inspects the raw
artifacts and their scope before recording the review. Missing, changed,
out-of-loop, malformed, unreviewed, failed and mismatched evidence cannot pass.
Old `*_verified` flags remain historical notes; they cannot bypass this contract.
This validates structure, provenance and coverage, not the truth of observations,
identity authentication or the adequacy of a manager's chosen acceptance plan.

Completion through `update_task_status` raises `ValueError` with the missing
delivery checks before persisting a false completion. Direct file writers are
still caught by the same check in `validate_loop(closeout=True)` and receipt
reconciliation. Legacy delivery tasks must define their scoped plan; ordinary
code-only tasks retain their prior contract. Cancellation is not auto-replayed.

For unfinished delivery tasks the review row has `dispositions` entries with
`task_id` and `check_dispositions`: each unaccepted check has `check_id` and an
existing active/action/blocked/deferred disposition. An action still needs its
next step in the task's canonical `next_disposition`; the receipt disposition
must match that value so the report cannot retain an obsolete global wait. An action needs its
next step under continuous supervision. `blocked` also names `blocked_on`, a
nonempty subset of that check's `prerequisites`. Thus an adoption approval does
not cover an independent preview check. This does not prove the dependency
itself is justified; the controller must verify that judgment. Legitimate
unfinished work with complete current dispositions does not trigger recovery.

The controller records `outcome_report` from
`SuperloopTaskboardService.outcome_report(loop_id)` and uses that projection to
report requested outcomes, accepted scopes, remaining checks and next ownership.
A task with all checks accepted but still open requires a formal closeout action;
an empty remaining-check list cannot exempt it indefinitely. A stale/missing projection requires review; test counts and commits cannot
substitute for it. It is not a message receipt. Exact-request canonical Connector
delivery still determines whether the visible report was sent. Work review and
report delivery remain separate, and missing delivery never resends product work.
The existing single recovery budget, pause/stop boundaries and maintenance
fallback are unchanged.

Failure proof: four real-Store scenarios failed before this patch: status
completion accepted missing evidence; Runner closed a loop without receipts;
single-Agent adoption plus failed preview passed Boolean checks; and one adoption
wait hid the independent preview. The repaired scenarios also exercise persisted
reviewed observations, evidence mutation, next-check coverage and report staleness.
Source validation, running adoption and full live management acceptance are
separate facts recorded in the instance's delivery board.
