# Remote Install Superloop Template

## Purpose

Install a prepared HASHI package onto a remote machine, verify it in place, configure any required triggers, and close only after the remote package is operational.

This template is for cross-machine deployments such as INTEL/MSI package rollout. It must keep HASHI core slim: prefer package files, `private/` config, remote sidecar scripts, Hashi Remote transfer, and existing scheduler/task systems.

## Required Roles

- `orchestrator`: Lily or another controller on HASHI1.
- `remote_worker`: the target machine agent that applies the install, e.g. `agent1@INTEL`.
- `remote_reviewer`: an independent verifier on the same target when available, e.g. `agent2@INTEL`.
- `consultant`: optional external reviewer for repeated failures, security risk, or design uncertainty.

If a same-machine reviewer exists, do not skip it. The reviewer should verify paths, hashes, task status, and smoke outputs independently from the worker.

## Inputs

- Target instance id: `INTEL`, `MSI`, etc.
- Remote worker and reviewer agents.
- Package artifact list and expected SHA256.
- Remote staging path.
- Remote install root.
- Rollback policy and backup path.
- Required local config paths under `private/`.
- Required triggers: logon/startup, daily, interval, HASHI cron, Windows Task Scheduler, or manual.
- Exit condition in concrete command terms.

## Non-Negotiable Gates

### G1 Route And Identity

Verify both messaging and file transfer routes before transfer:

- HChat/protocol route to worker.
- Hashi Remote capabilities.
- File transfer `stat` or equivalent.
- Remote repo root exists.

Record the live host/route used. If cached HChat and protocol disagree, prefer the route that passes live capability checks.

### G2 Versioned Artifact Gate

Every artifact transferred must have:

- file path
- byte size
- SHA256
- timestamp
- expected remote destination

Every remote report must include the SHA it tested. If the reply references an old SHA, stale command name, or old error signature, mark it stale and request a hash-first retest.

### G3 Staging Before Apply

Transfer to remote staging first. Do not write final install locations until:

- remote worker verifies package contents
- `py_compile` or equivalent syntax check passes
- package `--help`, `--check`, and `--dry-run` pass where applicable
- rollback location is chosen

### G4 Apply With Rollback

Before applying:

- backup any existing target files
- backup relevant local state only when explicitly part of rollback
- never copy secrets or memory DBs into committed package locations
- keep machine-specific config under `private/`

### G5 Live Operational Gate

Do not close on install alone. Close only after the package is usable in its target role.

Required evidence:

- installed files SHA match expected values
- runtime command passes from final install path
- target role smoke passes, not only isolated smoke
- configured private paths are used when defaults are not portable
- schedule/trigger layer passes if the package is meant to run automatically
- delivery/import side is verified when the workflow crosses machines

### G6 Active Follow-Up

The orchestrator must not passively wait after a partial result.

For every remote handoff:

- define exact expected fields
- set the next action if no reply arrives
- set a concrete follow-up deadline before entering the wait
- run a controller-side probe when the deadline is reached
- follow up when a report is incomplete
- record stale replies as stale, not as new failures
- use short non-blocking ticks instead of long blocking sleeps

Required wait record fields:

```text
wait_id:
target:
command_id:
entered_at:
follow_up_after_minutes:
deadline_at:
controller_probe:
follow_up_message:
escalation_if_no_reply:
status:
```

Short tick rule:

- Keep each orchestrator wait tick to 30-60 seconds.
- Each tick must run at most one quick message/route check, one endpoint probe,
  and one focused file/stat probe when useful.
- Do not run long `sleep` loops that prevent the orchestrator from processing
  incoming reports or changing course.
- If the worker owns the task, do not switch execution agents during a tick.
  Use other agents only as read-only reviewers unless the operator explicitly
  reassigns ownership.
- If a tick finds no progress, record the observation and send a narrowly scoped
  follow-up asking for the current command, exit code, and blocker.

Default deadlines:

- preflight/check-only command: follow up after 5 minutes
- package transfer/stat command: follow up after 3 minutes
- install/apply command: follow up after 10 minutes
- service/startup command: follow up after 5 minutes
- live smoke command: follow up after 3 minutes

When a deadline is reached, the orchestrator must do all of the following:

1. run the controller-side probe defined in the wait record
2. send the follow-up message
3. append an event noting the missed deadline and probe result
4. keep the loop in an explicit waiting or blocked state, never implicit idle

### G7 Independent Review

Use the remote reviewer before final close when:

- a remote reviewer exists
- scheduler/trigger behavior is involved
- secrets/token handling is involved
- there were repeated route failures or stale reports
- package writes to durable state

Reviewer checks should include hashes, task state, logs, smoke results, and rollback path.

## Standard Taskboard

1. Collect target, agents, package, install root, staging root, rollback policy.
2. Preflight routes: HChat/protocol, Hashi Remote capabilities, file transfer stat.
3. Build package manifest with SHA256, byte sizes, and privacy check.
4. Transfer package to staging and verify remote SHA.
5. Worker runs staging check/dry-run and reports command outputs.
6. Apply install with rollback backup.
7. Verify installed files by remote stat/hash.
8. Run target-role smoke from final install path.
9. Configure triggers/schedule if required.
10. Run scheduled/trigger wrapper manually once.
11. Verify cross-machine delivery/import if relevant.
12. Remote reviewer independently verifies evidence.
13. Fix/retest loop for any blocker.
14. Final acceptance: all gates green, evidence written, no open blockers.

## Remote Report Schema

Remote replies should include:

```text
report_type:
target_instance:
agent:
artifact_sha256:
command_id:
command:
exit_code:
stdout_summary:
stderr_summary:
paths_checked:
tokens_or_secrets_status: present|missing|not_required
rollback_path:
go_no_go:
blockers:
next_recommended_action:
```

## Stale Reply Detection

Treat a remote reply as stale if it:

- reports a SHA that does not match the latest pushed artifact
- mentions code/function names removed in the latest artifact
- omits command id or timestamp when a later command has already been sent
- repeats a previously resolved failure without hash evidence

Required response: ask for `Get-FileHash` first, then rerun only the latest command set.

## Trigger/Schedule Checklist

If the install includes automation:

- install trigger scripts under remote `private/` or package `scripts/` as appropriate
- verify installer is idempotent
- verify task exists after install
- run trigger wrapper manually in `diagnose` mode
- run the actual scheduled mode manually once
- verify logs include mode, config path, source/destination, token source, success/failure
- verify token/secret is loaded from existing secure machine config, not embedded in task command text
- verify central receiving side processes one delivered batch

## Exit Condition

The loop can close only when:

- install succeeded
- rollback path is documented
- final installed command passes
- configured smoke passes
- schedule/trigger passes if applicable
- cross-machine delivery/import passes if applicable
- worker and reviewer reports are recorded
- evidence artifact is updated
- no blockers remain

## Anti-Patterns

- Closing after only package transfer.
- Closing after only isolated smoke.
- Treating delayed stale HChat replies as current results.
- Skipping same-machine reviewer.
- Waiting without a deadline and controller-side follow-up probe.
- Leaving schedule/trigger setup outside the acceptance gate.
- Embedding shared tokens in scripts, task commands, docs, or committed config.
- Modifying HASHI core runtime to complete an install.
