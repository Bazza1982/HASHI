# HASHI–Frontend Essential Live Acceptance

## Purpose

This is the short, real-runtime acceptance layer between focused automated
tests and a release decision. It exercises a chosen HASHI checkout through an
actual external frontend, such as Workbench, and preserves evidence for the
failure modes that mocks do not establish:

- command delivery and natural backend completion;
- dynamic menu persistence across transcript refreshes;
- `/reboot min`, `/reboot max`, and `/restart` lifecycle boundaries;
- Remote discovery and recovery;
- client-owned unsent draft persistence; and
- desktop and compact visual layout.

The suite is data-driven. New live items belong in
`live_acceptance/suites/hashi_frontend_essential.json`; the runner itself does
not need a new command for each item.

## Ownership and boundary

- **Functional owner:** PAO, because it coordinates the run, evidence, and
  acceptance result.
- **Engineering layer:** developer/operations tooling adjacent to Functions.
  It is not loaded by the HASHI runtime.
- **Frontend boundary:** HASHI records the command/runtime evidence. The
  external frontend owns unsent draft text, layout, and screenshots.
- **Core boundary:** the protected source list is imported from
  `orchestrator.runtime_contract.CORE_SOURCE_PATHS`. The suite does not carry a
  copied list.
- **Process boundary:** PID liveness checks use the shared non-signalling
  process probe. The runner never calls `os.kill(pid, 0)` directly because on
  Windows signal zero is a console control event, not a harmless existence
  query.

The runner never sends a slash command, clicks a control, restarts a process,
or claims that a screenshot proves runtime state. Lifecycle actions remain
explicit operator actions and require current authorization for the named
instance and Agent.

## What a valid pass proves

A pass means all required interactive items have recorded evidence and the
baseline-to-final invariant holds:

1. the checked-out commit did not change;
2. the working-tree state did not change;
3. every protected Core file retained the same hash; and
4. each lifecycle step met its own PID expectation:
   - `/reboot min` and `/reboot max`: Core PID stays the same;
   - `/restart`: old Core PID is replaced by a different PID.

Source state, runtime state, frontend observations, and lifecycle receipts are
kept as separate evidence. A stable snapshot alone does not prove behavior.

## Human-facing result reporting

The operator-facing summary is a short decision note, not an evidence dump.
Write it in the user's language and lead with what is actually broken:

- if the run passed, say plainly that no tested behavior failed;
- if the run failed, name the observable broken behavior in the first sentence;
- report one root problem once, even when it causes several failed or blocked
  checklist items; and
- follow with one sentence saying whether everything else tested worked.

Keep untested scope separate from real failures. For example, a HER v2 run does
not prove or disprove a Codex-specific process-exit bug. Do not turn a dependent
blocked item into another product defect.

Do not lead with item IDs, evidence counts, PID or hash details, report paths,
or pass percentages. Include those only when they change a decision or the user
asks for supporting detail. The generated `report.md` keeps the full status and
evidence record; the normal user-facing handoff should fit in three short
paragraphs or bullets.

Preferred form:

> HASHI Exchange is offline. Everything else tested worked. The Codex-specific
> exit case was not tested because this run used HER v2.

Avoid turning the same result into a long scoreboard of passed, failed, and
blocked implementation details.

## Prerequisites

Before a run:

1. identify the exact checkout, instance, Agent, and external frontend;
2. confirm the user has authorized the live lifecycle operations in the suite;
3. make the development checkout the generation under test;
4. run focused automated checks and `scripts/check_protected_core_changes.py`;
5. ensure the frontend can select the exact target instance; and
6. prepare a folder for screenshots and exported receipts.

Do not run the suite against an ambiguous target. Stop on a failed lifecycle
receipt, unexpected Core change, lost draft, or uncertain instance identity.

## Runner commands

Run from the checkout under test with its approved Python environment.

### 1. Validate and start

```bash
.venv-wsl/bin/python3 scripts/hashi_live_acceptance.py validate

.venv-wsl/bin/python3 scripts/hashi_live_acceptance.py start \
  --instance-id HASHI2 \
  --agent zelda \
  --client-label Workbench \
  --client-url http://127.0.0.1:5176/
```

The command prints the new run directory. Keep that value as `RUN`. Run data
is stored under the ignored `state/live-tests/` tree by default.

```bash
RUN=state/live-tests/<printed-run-id>
.venv-wsl/bin/python3 scripts/hashi_live_acceptance.py status --run "$RUN"
.venv-wsl/bin/python3 scripts/hashi_live_acceptance.py guide \
  --item frontend_roundtrip
```

### 2. Record ordinary interactive evidence

The `record` command accepts repeatable screenshots/files and observations.
It refuses `pass` until the manifest's evidence minimum is satisfied.

```bash
.venv-wsl/bin/python3 scripts/hashi_live_acceptance.py record \
  --run "$RUN" \
  --item frontend_roundtrip \
  --status pass \
  --file screenshot=/path/to/status.png \
  --observation "Response appeared once and the Agent returned to idle."
```

Allowed file evidence types are `screenshot`, `transcript`,
`runtime_receipt`, `snapshot`, and `comparison`. Observations are supplied with
`--observation`. Evidence files are copied into the run and hashed.

### 3. Check `/reboot min`

```bash
.venv-wsl/bin/python3 scripts/hashi_live_acceptance.py snapshot \
  --run "$RUN" --label reboot-min-before

# Use the real frontend card to execute /reboot min and wait for online.

.venv-wsl/bin/python3 scripts/hashi_live_acceptance.py snapshot \
  --run "$RUN" --label reboot-min-after

.venv-wsl/bin/python3 scripts/hashi_live_acceptance.py compare \
  --run "$RUN" \
  --before reboot-min-before \
  --after reboot-min-after \
  --expect-core-pid same \
  --item reboot_min
```

Then record the screenshot, exported/sanitized runtime receipt, and operator
observation. The attached comparison already satisfies the comparison evidence
requirement.

### 4. Check `/reboot max`

Repeat the same pattern with labels `reboot-max-before` and
`reboot-max-after`, `--expect-core-pid same`, and `--item reboot_max`.
Confirm the terminal receipt covers shared Functions, all required Workers,
and Remote rather than only the initiating Agent.

### 5. Check `/restart` and draft persistence

Prepare the unique unsent draft before the pre-snapshot. Execute `/restart`
from a second frontend tab, then wait for the same instance to reconnect.

```bash
.venv-wsl/bin/python3 scripts/hashi_live_acceptance.py snapshot \
  --run "$RUN" --label restart-before

# Complete /restart through the real frontend and verify the draft after reconnect.

.venv-wsl/bin/python3 scripts/hashi_live_acceptance.py snapshot \
  --run "$RUN" --label restart-after

.venv-wsl/bin/python3 scripts/hashi_live_acceptance.py compare \
  --run "$RUN" \
  --before restart-before \
  --after restart-after \
  --expect-core-pid changed \
  --item restart_with_draft
```

The observations must separately state that the old PID was replaced and that
the exact draft text survived. A reconnect without draft verification is not a
pass.

### 6. Finish

```bash
.venv-wsl/bin/python3 scripts/hashi_live_acceptance.py status --run "$RUN"
.venv-wsl/bin/python3 scripts/hashi_live_acceptance.py finish --run "$RUN"
```

`finish` always captures a final snapshot and writes `report.md`. It exits
non-zero if any required item is not `pass` or if the Core/source invariant
failed.

## Failure handling

- Record a reproducible product defect as `fail` and attach the earliest useful
  screenshot, receipt, and observation.
- Record an environmental or authorization barrier as `blocked`; do not convert
  it into a product pass.
- Use `skip` only for an optional future item. Required items still prevent the
  run from passing.
- Do not use `/stop` during the backend-completion item until natural exit has
  already failed. If recovery needs `/stop`, record the item as failed first.
- Do not repair or edit the checkout during a run. End the run, fix the defect,
  and start a new run with a new baseline.

## Extending the suite

An added item must define:

- a stable ID and title;
- explicit dependencies;
- operator actions that produce observable behavior;
- concrete pass criteria; and
- minimum evidence by type.

Keep the essential suite short. Product-specific journeys and destructive or
long-duration scenarios should live in separate manifests rather than turning
this release gate into a full regression suite.
