# Auto Vibe Coding Superloop Template

## Purpose

Run a bounded implementation loop where one orchestrator coordinates one worker
and one independent reviewer until the requested code change is implemented,
verified, reviewed, fixed, and accepted.

This template is for feature work, refactors, bug fixes, or cleanup tasks that
benefit from parallel agent roles. It is not a remote install template and does
not close on "code written" alone. It closes only when the exit condition is
met and evidence has been recorded.

## Standard Roles

- `orchestrator`: Zelda or the active controller. Owns task framing, scope,
  integration, risk calls, commits, and final acceptance.
- `worker`: Lulu or another implementation agent. Owns the assigned write
  scope and edits files directly in its workspace.
- `reviewer`: Akane or another independent verifier. Reviews the diff,
  checks evidence, challenges weak assumptions, and confirms blockers.
- `consultant`: Optional. Used only for repeated failures, architecture risk,
  security risk, or unclear product intent.

The worker and reviewer must not have the same responsibility. The worker
implements; the reviewer verifies and tries to break the result.

## Inputs

- User request and concrete exit condition.
- Target repo, branch, and known dirty-worktree constraints.
- Files/modules in scope and files/modules out of scope.
- Worker ownership boundaries.
- Reviewer checklist.
- Required lightweight checks.
- Live checks, if the task affects running services.
- Commit policy for the checkpoint.

## Non-Negotiable Gates

Before starting, read the shared balanced orchestration guidance:

```text
superloops/config/orchestration_guidance.json
```

Use it as a compass, not a rigid rule engine. Preserve agentic flexibility, but
keep the hard stops for safety.

### G1 Scope And Baseline

Before editing:

- record the current branch and dirty status
- identify user-owned dirty files
- define in-scope files
- define out-of-scope files
- define exact exit condition
- create a taskboard from this template

Do not revert unrelated user changes.

### G2 Role Split

The orchestrator must explicitly assign:

- worker write scope
- reviewer verification scope
- orchestrator local responsibilities

Workers must be told they are not alone in the codebase and must not revert
edits made by others.

### G3 Evidence-First Implementation

Every implementation report must include:

- files changed
- behavior changed
- tests/checks run
- failures observed
- residual risks

Do not accept "done" without command outputs or concrete inspection evidence.

### G4 Independent Review

The reviewer must inspect the actual resulting diff or files. The reviewer
should prioritize:

- behavioral regressions
- missing tests
- broken integration boundaries
- unsafe assumptions
- stale or incomplete evidence

Reviewer findings must be classified as blocker, non-blocker, or follow-up.

### G5 Fix And Retest Loop

For every blocker:

- record the issue
- assign the fix owner
- apply the fix
- rerun the relevant check
- ask reviewer to confirm if the fix changes their review surface

Do not close while blockers remain.

### G5.5 Orchestration Hygiene

The orchestrator should keep the loop moving without becoming a busy wait:

- check worker/reviewer replies and state before running new probes
- preserve worker ownership unless reassignment is intentional
- classify stale or contradictory reports before acting on them
- turn blockers into the smallest safe next action
- record resume evidence in the loop state or evidence log

Reviewer and consultant agents may challenge the plan, but they should not take
over the worker's write scope unless the orchestrator explicitly reassigns it.

### G6 Runtime Or Live Verification

If the change affects a running service, remote peer, startup script, UI, or
cross-machine flow, static tests are not enough. Record the live command or
probe that proves the target behavior.

If live verification cannot be performed, record the reason as residual risk.

### G7 Commit And Close

Commit when the change is coherent, scoped, and verified enough for its
purpose. Stage only files that belong to the checkpoint.

Before final closeout, the orchestrator must perform an inbox drain barrier:

- check queued worker/reviewer replies for the loop id
- check recent hchat replies, message logs, taskboard updates, and loop events
- classify every pending reply as current evidence, superseded evidence,
  already-accounted-for evidence, contradiction, or new blocker
- record stale or superseded replies in the loop events/evidence log without
  surfacing them as new user-facing status unless they change the outcome
- reopen or pause the loop if a late reply contains a new blocker,
  contradictory evidence, or a missing acceptance requirement

Do not treat a loop as closed merely because commits exist. Close only after
the exit evidence is recorded and pending same-loop replies have been drained
or classified.

The final closeout must include:

- commit SHA, if committed
- changed files
- checks run
- live verification, if applicable
- reviewer result
- inbox drain/classification result
- remaining risks
- exit condition status

## Standard Loop

1. Capture request, scope, baseline, and exit condition.
2. Create the taskboard and evidence log.
3. Assign worker write scope and reviewer verification scope.
4. Worker implements bounded change.
5. Orchestrator inspects and integrates worker output.
6. Run lightweight checks.
7. Reviewer verifies diff and evidence.
8. Fix blockers and rerun checks.
9. Run live verification when required.
10. Commit scoped checkpoint.
11. Drain and classify pending worker/reviewer replies for the loop id.
12. Final acceptance: exit condition met, no blockers, evidence recorded, no
    unclassified pending same-loop replies.

## Anti-Patterns

- Closing because a worker says "done".
- Letting reviewer only read a summary instead of the diff.
- Running broad refactors outside the user's requested scope.
- Mixing unrelated dirty files into the checkpoint.
- Skipping live verification for startup, service, remote, or UI work.
- Creating excessive harnesses while the critical path remains unverified.
- Continuing to add infrastructure after a working state appears before
  freezing the working checkpoint.
- Closing while worker/reviewer hchat replies for the same loop remain queued,
  unclassified, or unsurfaced.
- Responding one-by-one to stale post-close hchat replies instead of recording
  them as superseded/late evidence unless they introduce a new blocker.
