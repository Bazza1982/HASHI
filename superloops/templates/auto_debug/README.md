# Auto Debug Superloop Template

## Purpose

Run a bug investigation and repair loop from a reported issue through diagnosis,
review, implementation, live validation, and closeout.

This template is for bugs where the designed intent of a HASHI function appears
broken. It is stricter than a normal coding loop: it does not close when a worker
claims a fix. It closes only after the orchestrator proves the original reported
behavior is fixed with static checks, smoke checks, and real-world tests.

## Required Roles

- `orchestrator`: owns the intended design, scope, HASHI core boundary, and final
  acceptance. The orchestrator must personally inspect logs and live behavior.
- `debug_worker`: Lulu or another debug agent. Owns broad diagnosis and any
  bounded implementation work assigned by the orchestrator.
- `reviewer`: Akane or another independent reviewer. Reviews the diagnosis plan
  before implementation and reviews the result after implementation.
- `subject_agent`: the live agent or subsystem where the issue was reported,
  e.g. `sakura`.
- `consultant`: optional. Used for repeated failure, architecture uncertainty,
  security risk, or proposed core changes.

## Inputs

- Reported issue and user-visible symptom.
- Designed intent of the feature.
- Subject agent and exact reproduction surface.
- Exact user-visible capability that must work before closeout.
- Logs to inspect.
- In-scope files and out-of-scope core files.
- Safety boundary: HASHI core must remain slim and stable; changes should take
  effect with `/reboot` at most.
- Concrete exit condition.

## Non-Negotiable Gates

### G1 Issue Intake And Reproduction

Record:

- user-visible symptom
- screenshot or transcript evidence
- subject agent
- exact local time
- expected behavior
- observed behavior
- initial reproduction steps

Do not accept a theory until logs support it.

### G2 Design Intent Lock

The orchestrator must write the intended behavior before assigning fixes.

For dual-brain issues, examples include:

- left brain is context/FYI sidecar only
- right brain is the only user-visible executor
- left brain output must not override user prompt or `/sys`
- sidecar logs must be separate from foreground replies
- each user prompt should normally produce at most one visible assistant answer

### G3 Slim HASHI Core Boundary

Before any implementation:

- list core files that must not be modified unless approved
- prefer config, wrappers, external scripts, existing task/cron hooks, or sidecar
  state
- if core change appears necessary, record why, alternatives considered, reboot
  requirement, and rollback path

### G4 Worker Diagnosis

The debug worker must inspect every relevant angle:

- transcript and core transcript
- token audit and completion paths
- sidecar artifacts
- command routing and reset sources
- concurrency or queueing behavior
- UX timing and status messages
- tests covering the issue

The report must separate facts, hypotheses, blockers, and proposed fixes.

### G5 Pre-Implementation Review

The reviewer must review the diagnosis and proposed plan before implementation.

The reviewer should check:

- whether the plan matches design intent
- whether it touches core unnecessarily
- whether the reproduction is real
- whether the tests would actually catch the reported bug
- whether rollback and `/reboot` behavior are clear

### G6 Fix And Retest Loop

For every blocker:

- record the issue
- assign a fix owner
- apply the smallest suitable fix
- run targeted tests
- run regression tests around command routing and dual-brain behavior
- ask reviewer to confirm when the review surface changes

### G7 Supervised Runtime Environment Parity

If the bug involves a service, scheduler, watchdog, remote supervisor, cold
restart, or process launcher, the orchestrator must prove the supervised runtime
has the same required execution context as the known-good interactive runtime.

Required evidence includes:

- launch owner and launch context
- exact command used by the supervisor
- config path, home path, workspace path, and instance id
- `PATH` and required CLI discovery
- `USERPROFILE`, `HOME`, `APPDATA`, and `LOCALAPPDATA`
- app-specific homes such as `CODEX_HOME` or equivalent backend auth/cache paths
- active backend, active model, and persisted state after restart
- subject-agent identity and address after restart

Missing environment parity is a blocker. A process being alive, a port listening,
or a health endpoint returning 200 is not sufficient acceptance evidence.

### G8 Real-World Subject-Agent Capability Test

The orchestrator must personally run or coordinate a live test against the
subject agent after implementation.

Health checks are not capability tests. The live smoke must exercise the exact
user-visible path that failed, and it must show the backend or command path
actually completed the requested work.

For restart, launcher, backend, or supervisor bugs, required evidence includes:

- process restart evidence when restart is part of the fix
- workbench or service health after restart
- identity and config preserved after restart
- subject agent listed and reachable
- a real user request sent to the subject agent
- selected backend or command path reports success
- visible output returned to the user
- no new relevant errors in the subject-agent error log
- request id or timestamp tying transcript, event log, and error audit together

For a dual-brain issue, required evidence includes:

- conversation transcript showing one visible response per prompt
- left-brain event log showing sidecar-only preflight and after-action records
- token audit showing `completion_path=sidecar` for left brain and
  `completion_path=foreground` for the visible answer
- no duplicate visible answer for normal prompt
- no extra visible answer for `/new` unless explicitly designed
- `/reboot` performed if required by the changed path

### G9 Closeout

The loop can close only when:

- root cause is identified
- fix is implemented or a deliberate no-code decision is recorded
- static checks pass
- targeted tests pass
- supervised runtime environment parity passes when relevant
- live subject-agent capability smoke passes
- identity, config, backend, and model state are preserved when relevant
- relevant error logs show no new regression
- reviewer gives final no-blocker verdict
- same-loop worker/reviewer/subject-agent replies are drained and classified
- evidence artifact is updated
- no open blockers remain

Late messages after a candidate final smoke must be classified before closeout:
current blocker, contradiction, non-blocking follow-up, stale/superseded, or
already-accounted-for. If the late message contains blocker evidence, reopen the
loop instead of closing it.

## Standard Taskboard

1. Intake issue, design intent, subject agent, and reproduction evidence.
2. Orchestrator inspects baseline logs and records facts.
3. Assign debug worker to broad diagnosis.
4. Assign reviewer to pre-implementation review criteria.
5. Worker proposes root cause and fix plan.
6. Reviewer reviews diagnosis and plan.
7. Orchestrator approves bounded implementation scope.
8. Worker implements fix.
9. Orchestrator runs static and targeted tests.
10. Reviewer performs post-implementation review.
11. Orchestrator validates supervised runtime environment parity when relevant.
12. Orchestrator runs real-world subject-agent capability smoke and log audit.
13. Drain and classify same-loop worker/reviewer/subject-agent replies.
14. Fix/retest any blockers.
15. Final acceptance and close.

## Report Schema

```text
report_type:
loop_id:
role:
subject_agent:
issue:
design_intent:
facts:
hypotheses:
root_cause:
proposed_fix:
files_in_scope:
core_touch_required: yes|no
reboot_required: yes|no
environment_parity:
tests:
live_test_plan:
capability_smoke:
identity_config_preserved: yes|no|not_applicable
backend_error_audit:
inbox_drain:
blockers:
go_no_go:
next_action:
```

## Anti-Patterns

- Closing because the worker says fixed.
- Fixing before the intended behavior is written down.
- Treating screenshots as sufficient without transcript/token audit evidence.
- Accepting tests that do not reproduce the reported symptom.
- Skipping subject-agent live testing.
- Treating a green health endpoint, open port, or running process as proof that
  the user-visible capability works.
- Accepting a restart fix without proving backend auth/cache/config survived the
  supervised launch.
- Skipping environment parity checks for service/watchdog/scheduler launches.
- Hiding a core behavior change inside a large refactor.
- Leaving the loop idle after a remote or worker report.
- Closing while same-loop worker, reviewer, or subject-agent replies remain
  undrained or unclassified.
