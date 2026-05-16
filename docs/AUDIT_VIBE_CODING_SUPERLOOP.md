# Audit Vibe Coding Superloop

`audit_vibe_coding` is the end-to-end product delivery superloop for vibe-coded software work. It starts from a product idea and exits only after the real product is built, independently reviewed, fixed, started in its target runtime, and proven to behave correctly.

This is not a planning loop and not a unit-test loop. The final authority is the live program running in the intended environment.

## Roles

| Role | Default |
|---|---|
| Orchestrator | Zelda |
| Worker | User-selected; for the Watchtower v1 run this was Lulu |
| Independent plan reviewer | Akane |
| Independent final reviewer | Akane |
| Exit approver | Orchestrator, using the exit gates below |

The independent reviewer should be cross-vendor from the main orchestrator/worker when possible. For Zelda-led runs, use `/hchat akane` by default.

## Required Flow

```text
Idea
  -> user Q&A / scope lock
  -> plan package
  -> independent plan review
  -> address plan review comments
  -> worker implementation
  -> worker local tests
  -> orchestrator code review
  -> orchestrator focused tests
  -> real runtime smoke
  -> stop/start/restart behavior test
  -> live routing/auth/discovery verification
  -> independent final product review
  -> address final review comments
  -> re-test changed behavior
  -> exit only if all gates pass
```

Any failed check creates an internal fix loop:

```text
test/review/smoke failure
  -> diagnose
  -> assign fix
  -> implement
  -> re-test the exact failed check
  -> re-run affected downstream gates
```

## Mandatory Start Questions

The superloop must not start implementation until the orchestrator has explicit answers for:

1. Who is the worker?
2. Where is the code being written?
3. Where is the product supposed to run?
4. What is v1 in scope and out of scope?
5. What are the completion criteria?
6. Who reviews the plan?
7. Who reviews the final product?
8. What real runtime must be used for smoke testing?
9. What existing running services must not be disturbed?
10. What rollback or recovery action is allowed if smoke testing breaks something?

If any answer is missing, the loop must pause for Q&A instead of guessing.

## Plan Review Gate

Before worker implementation:

- Send the plan package to the independent reviewer.
- Treat blocker review comments as non-optional.
- Update the plan package with reviewer-resolved decisions.
- Do not dispatch implementation until the reviewer says the plan is implementable.

The plan must include concrete runtime topology. For cross-boundary products, it must state which side owns each process, port, file path, PID, and health check.

## Implementation Gate

The worker must report:

- Files changed.
- Tests run.
- Manual smoke evidence, if any.
- Known limitations.
- Dirty worktree files that were not part of the task.

The orchestrator must verify the diff and must not accept worker smoke evidence as a substitute for orchestrator-owned smoke evidence.

## Live Product Gate

The orchestrator must personally test the real product in the target runtime before exit. For sidecars, daemons, services, bridges, or network programs, this gate includes:

- Start the product in its intended runtime.
- Confirm the process is independent from services it rescues or supervises.
- Confirm the process listens on the expected or dynamically selected port.
- If the expected port is unavailable, confirm it selects a high available port and records/broadcasts that actual port.
- Confirm live discovery/routing state uses the actual runtime port, not stale static config.
- Confirm unauthenticated requests are rejected.
- Confirm the intended auth path works, including shared-token HMAC when that is the system standard.
- Confirm health/status/log/control endpoints return correct, current information.
- Confirm the view is coherent from each relevant side, such as Windows and WSL for a Windows/WSL product.
- Confirm existing services that must not be disturbed are still running and still identify as the correct instance.

If the product is meant to recover or supervise another service, the orchestrator must separately verify:

- The sidecar can be stopped without taking down the supervised service.
- The supervised service can be stopped/recovered through the intended fixed rescue path.
- The sidecar can be turned off and on again.
- After off/on, live endpoint state, advertised capabilities, auth, logs, and status are correct.

## Exit Gate

The superloop may exit only when all of these are true:

- Plan review blockers are resolved.
- Worker implementation is complete.
- Orchestrator code review is complete.
- Focused tests pass.
- Real runtime smoke passes.
- Stop/start/restart behavior passes where applicable.
- Live routing/discovery/auth views are correct.
- Independent final review has no blocker.
- Final reviewer comments have either been fixed or explicitly recorded as non-blocking.
- The orchestrator has re-run affected tests after every fix.
- No user had to manually discover a missing verification step after the loop claimed completion.

For services and sidecars, "tests passed" is never enough. The loop exits only after the live program is running and behaving correctly.

## Watchtower Lessons Captured

The Watchtower v1 run added these permanent lessons:

- A sidecar must be validated as a standalone program, not as part of the main HASHI instance.
- Dynamic port selection is only correct if the selected port is written to live endpoint state and clients use that live state.
- Rescue control must use the same authentication standard as the rest of Hashi Remote.
- The orchestrator must verify off/on behavior before exit.
- A stale or wrong-home process on the same port must be treated as a failed smoke, not as a successful service.
- The final state must identify the correct instance, root path, PID, port, and capabilities from both Windows and WSL views.
