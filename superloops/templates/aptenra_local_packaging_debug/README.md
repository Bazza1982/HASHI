# Aptenra Local Packaging Debug Superloop

## Purpose

Drive Aptenra Windows packaging from an observed installation failure to one
real, user-view lifecycle acceptance. This is a supervised engineering loop,
not a build-only loop and not permission to repeat blind installs.

The loop keeps the Aptenra Packaging Failure Journal as an executable
regression memory:

```text
What mistake did I make last time?
How will I avoid it in the most straightforward way this round?
```

The orchestrator must answer both questions at the start of every round and
record the answer before changing source, building media, or installing.

## Fixed Loop Policy

```text
max_rounds = 30
scheduler_auto_advance = false
known_failure_recurrence = immediate_block
candidate_validation = actual GUI install + actual standard-user launch
failed_candidate_cleanup = uninstall before the next round
success = installed Aptenra launch + installed Workbench launch
provider_credentials_required = false
```

Thirty rounds means at most thirty evidence-led
`diagnose -> repair -> build -> install -> validate` engineering rounds. It
does not authorise thirty blind installation attempts. A round that cannot
explain what new information or fix distinguishes it from the prior round is
blocked before build.

## Source Of Truth

- Product source: the exact recorded Aptenra `main` commit.
- Packaging source: the packaging-only worktree and exact recorded commit.
- Failure memory:
  `docs/aptenra/APTENRA_PACKAGING_FAILURE_JOURNAL.md` in the packaging
  worktree.
- Runtime controller state:
  `superloops/loops/<loop_id>/`.
- Candidate media: immutable candidate-specific directory containing the MSI,
  every CAB, manifest, proof and hashes.

The installer must consume a proven payload from the recorded product commit.
It must not repair missing product behaviour by copying credentials, inventing
runtime configuration, selecting ports, provisioning HASHI, or hiding startup
failures.

## Required Roles

- `orchestrator`: owns the round question, journal review, scope decision,
  evidence review, explicit task advancement and final acceptance.
- `executor`: performs bounded source changes, builds, `/usecomputer`
  operations and lifecycle tests assigned by the orchestrator.
- `reviewer`: checks that the previous-error gates, diff, media and lifecycle
  evidence support the claimed outcome.
- `operator`: the user. Approval is required only when the loop needs authority
  outside the standing local packaging SOP.

One runtime may temporarily fill more than one role, but the evidence and
decision fields remain separate. A worker's statement that something passed is
never the acceptance decision.

## Round State Machine

Each round follows this order:

1. **Last-mistake gate**
   - Answer the two mandatory questions.
   - Read every active Journal record.
   - Confirm all `PFJ-001` through the latest PFJ entry have an armed prevention
     gate.
   - Any known signature already present in the proposed approach blocks the
     round immediately.
2. **Freeze and diagnose**
   - Preserve MSI, CAB, logs, process identity, state identity, timestamps and
     exact user-visible symptom.
   - Locate the failing responsibility layer: product, packaging, media,
     target state, proof system or GUI/lifecycle.
3. **Journal update**
   - Append the new failure before building replacement media.
   - Record source, media and regression-coverage status separately.
4. **Uninstall the failed candidate**
   - Stop only processes proved to belong to that candidate.
   - Uninstall the exact failed product.
   - Prove zero candidate registration, shortcuts, services, Program Files
     residue, processes, listeners and pending-delete entries.
   - Prove the original Debug Runtime was not stopped, modified or removed.
5. **Minimal repair plan**
   - Re-read the Journal and choose the smallest correct-layer repair.
   - State what differs from the prior failed round.
   - Do not build if this distinction is absent.
6. **Implement**
   - Modify the recorded correct product `main` and/or packaging layer only
     where the diagnosed ownership requires it.
   - Keep failed media immutable.
7. **Prebuild regression gate**
   - Run applicable previous-error checks before expensive payload/CAB work.
   - Run the unpacked payload from a new state root with provider credentials
     absent.
   - Proof must be derived from observations, not authored pass literals.
8. **Build one new candidate**
   - Use a new candidate ID, ProductCode and immutable media directory.
   - Freeze product commit, packaging commit, file inventory and hashes.
9. **Preinstall media gate**
   - Validate MSI/CAB atomicity, ICE, decompiled tables, shortcut quoting,
     icon, paths, target preconditions and verbose logging.
10. **Actual user-view validation**
    - Use `/usecomputer` to run the visible interactive installer.
    - Launch both installed shortcuts as an ordinary user.
    - Record whether the real Aptenra and Workbench windows appear or which
      real error appears.
    - Provider credentials are outside this installation-and-dual-launch
      acceptance scope and must not become a wait or prerequisite.
    - A theory, source check, unit test, extracted payload, process, port or
      health response never substitutes for this installed test.
11. **Failed-candidate uninstall**
    - If installation or either installed launch fails, perform Uninstall
      before the next round.
    - Prove the declared state-retention policy and zero installer-owned
      residue.
12. **Round decision**
    - `pass`: only when the exact installed media reaches
      `INSTALL-DUAL-LAUNCH-ACCEPTED`.
    - `fail-new`: freeze, diagnose, append Journal entry, uninstall, then
      create the next round.
    - `fail-known`: block the candidate immediately and repair the failed
      regression gate before any next build.
    - `await_human`: use only for a genuinely missing authority or external
      state change.

## Actual Validation Rule

Every candidate-level validation must include a real installation. Prebuild
checks are necessary rejection gates, but they are not candidate validation.

The exact accepted sequence is:

```text
new candidate media
-> visible interactive install via /usecomputer
-> installed Aptenra shortcut launch as standard user
-> installed Workbench shortcut launch as standard user
-> success: leave the accepted installation in place
-> failure: Uninstall and residue audit before the next round
```

If installation, launch or a basic function fails, record the actual failure
and proceed to candidate-specific Uninstall/cleanup. Do not manually repair the
installed files and then accept the original media.

## Known-Failure Recurrence Rule

One historical occurrence is enough to create a permanent gate. There is no
"two consecutive occurrences" grace period.

```text
known signature observed
-> candidate status INVALID
-> stop build/install progression
-> record which regression gate failed
-> repair that gate
-> use a new candidate identity
```

`known_failure_registry.template.json` is the machine-readable minimum set. The
Journal remains authoritative and may contain newer records; the first task in
every round must fail if the Journal contains a PFJ ID missing from the
registry.

## Liveness Without Automatic Task Starts

`scheduler_auto_advance=false` means the background Superloop scheduler must not
change a pending card to `in_progress` merely because the loop is idle.

Liveness is provided separately by an idle nudge:

1. When the orchestrator is busy, the nudge does nothing.
2. When the orchestrator becomes idle, the nudge queues a continuation prompt.
3. The prompt makes the orchestrator read `state.json`, `taskboard.json`,
   `issues.json`, `waits.json`, recent events and the active round record.
4. If a task is already in progress, the orchestrator takes a concrete
   continuation action; it must not answer with status alone.
5. If no task is in progress, the orchestrator checks dependency and evidence
   gates, then explicitly starts the next permitted card in the same turn.
6. The nudge itself never edits task status, never clicks the GUI and never
   waives a gate.
7. A failed internal gate is work for the loop to diagnose and repair. It must
   not be converted into an invented secrets or user-input wait.
8. If no evidence changes within the declared stale interval, the orchestrator
   opens a `loop_stalled` issue and immediately takes the smallest safe
   recovery action.
9. Waiting and status reporting are non-terminal. The nudge remains enabled
   until installed dual-launch succeeds or round 30 is formally blocked.

This separates two powers:

```text
nudge = wake and inspect
orchestrator = decide and explicitly advance
```

## Standing Authority And Human Waits

This template records the user's standing authority for:

- bounded diagnosis;
- correct-layer product/packaging fixes;
- new candidate build;
- actual local interactive install via `/usecomputer`;
- installed launch and basic-function checks;
- candidate-specific Repair and Uninstall; and
- identity-safe cleanup of the failed candidate.

Create `await_human` before:

- rebooting Windows or WSL;
- stopping, modifying or uninstalling the original Debug Runtime;
- deleting user data or credential material;
- acting on a different Aptenra instance;
- publishing/signing/deploying externally; or
- expanding beyond Aptenra local packaging acceptance.

## Closeout

The loop closes successfully only when:

- every historical error gate is recorded and passed or evidenced
  non-applicable;
- no known signature recurred;
- one exact candidate completed a real GUI install;
- installed Aptenra launched from its shortcut from the user perspective;
- installed Workbench launched from its shortcut from the user perspective;
- the original Debug Runtime remained unchanged;
- the Failure Journal and candidate ledger are current;
- no blocker issue or open wait remains; and
- the exact status is `INSTALL-DUAL-LAUNCH-ACCEPTED`.

At round 30 without closeout, set the loop to `blocked`, preserve all evidence,
disable the liveness nudge and report the unresolved technical boundary. Never
convert the round limit into a success claim.
