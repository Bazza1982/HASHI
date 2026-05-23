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
python -m pytest tests/test_superloop_store.py tests/test_superloop_taskboard.py tests/test_superloop_waits.py tests/test_superloop_runner.py tests/test_superloop_scheduler.py tests/test_superloop_compiler.py tests/test_superloop_issues.py tests/test_superloop_commands.py tests/test_superloop_recording.py tests/test_superloop_nagare_adapter.py -q
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
