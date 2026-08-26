# Workbench Agentic Workflow Debug Superloop

## Purpose

Build and prove the Workbench agentic-workflow product path on the Lenovo
Developer Runtime. This loop covers Canvas authoring, workflow validation and
compilation, immutable packaging, save/reload/import/export, Nagare execution,
Minato observation and control, approvals, artefact return, recovery, security,
and real Electron UI behavior.

It explicitly excludes the conforming frontend MSI, Setup, packaging, voice, chat, and general
desktop acceptance unless one of those boundaries directly prevents the
Workbench workflow path from running.

## Source and integration boundary

- The execution and repair branch is `Lenovo-debug`.
- The Lenovo checkout is
  `C:\Users\apten\projects\the conforming frontend-Lenovo-debug`.
- During the loop, all product fixes and regression tests are committed only to
  `Lenovo-debug`; the mother-machine `main` remains unchanged.
- Each repair round is reloaded and tested in the Lenovo Developer Runtime.
- Only after final closeout may the complete, Lenovo-verified commit series be
  brought back to mother `main` in one integration step.
- Never accept an uncommitted source tree or a manually patched running copy.

## Authoritative product flow

```text
Canvas Draft
  -> validation
  -> deterministic Workflow Compiler
  -> immutable Workflow Package
  -> Nagare Run
  -> Minato events and controls
  -> Canvas Run Projection
  -> typed Artefacts and Replay Bundle
```

Authority is fixed as follows:

- Canvas owns editable authoring state and layout.
- The compiler owns executable DAG semantics.
- A Workflow Package is the immutable input for one or more runs.
- Nagare owns run and step state.
- Minato observes and invokes Nagare controls; it cannot fabricate completion.
- Canvas Run Projection is read-only with respect to the source revision.
- The artefact store owns result content, provenance, hashes, and versions.

## Golden journey

The loop does not close until a user can complete this exact real journey:

```text
blank Canvas
  -> create Agent / Task / Approval / Artefact nodes
  -> connect dependencies
  -> static validation
  -> autosave
  -> close and reopen Workbench
  -> compile an immutable Workflow Package
  -> dry run
  -> submit a real run
  -> observe step/event state in Minato and Canvas
  -> approve, pause, resume, and stop controlled cases
  -> receive typed artefacts
  -> restart Workbench and recover history
  -> export, import, and rerun without old secrets or approvals
```

An exported intent brief, a mocked renderer, an API returning `run_id`, or a UI
badge saying Completed is not sufficient proof.

## Workflow Package contract

An executable package contains at least:

```text
manifest.json
workflow.yaml
canvas.snapshot.json
capabilities.json
schemas/
SHA256SUMS
```

The manifest records schema and compiler versions, workflow ID/version, source
Canvas ID/revision/hash, DAG hash, required agents/tools/capabilities, typed
inputs/outputs, approval policy, and every file SHA-256. It contains no secret,
token, absolute machine path, run state, or previous approval decision.

Required invariants:

1. The same Canvas revision compiles deterministically.
2. A semantic edit changes the package hash.
3. Editing Canvas after submission cannot change that run.
4. Any package tamper is rejected before execution.
5. Import cannot revive old credentials, approvals, or run state.
6. Canvas-to-package-to-run performs real execution.

## Gates

### G0 Baseline and executable semantics

- Freeze mappings for Task, Agent, dependency, Approval, Artefact, context-only
  Note/Message, and visual-only Group/layout/related edges.
- Executable compilation fails closed for cycles, dangling dependencies,
  duplicate IDs/outputs, missing workers, tools, or capabilities.
- Record current unit, contract, API, Electron, and Lenovo UI behavior before
  making repairs.

### G1 Canvas authoring and persistence

- Create/edit/connect/delete, undo/redo, copy/paste, selection, zoom, keyboard,
  accessibility, and compilation-error focus.
- Autosave, explicit save, immediate close, restart, primary corruption and LKG
  recovery, concurrent revision conflict, schema migration, public/encrypted
  export, import-replace, save-copy, wrong key, and corrupted authentication tag.

### G2 deterministic compiler and package

- Minimal, linear, fan-out/fan-in, conditional, quality, and approval graphs.
- Input/default/artefact substitution, agent/tool/capability binding, schema
  migration, deterministic round trip, tamper rejection, and path safety.

### G3 Nagare execution and recovery

- Deterministic workers simulate success, retry-success, permanent failure,
  timeout, malformed output, duplicate callback, and missing/corrupt artefact.
- Prove dependency ordering, attempt monotonicity, terminal-state uniqueness,
  idempotent submission, pause/resume checkpoint behavior, stop boundaries,
  crash recovery, and immutable package binding.

### G4 Agentic approvals and Minato

- Writer-to-reviewer handoff, parallel research-to-synthesis, preflight, approve,
  reject, expiry, revoke, self-approval denial, operation/input/revision binding,
  tool and model unavailability, least-context access, and consequential-action
  exactly-once behavior.
- Prove capability discovery, stable run IDs, event-sequence reconnect, multiple
  concurrent runs without cross-talk, authoritative control calls, accurate
  Canvas/Minato projection, artefact version selection, and redacted audit logs.

### G5 Real Electron golden journey

- Run the golden journey through the real Workbench server, Nagare API,
  Electron renderer, and isolated state directories on Lenovo.
- Include double-submit, offline/reconnect, server restart, narrow-window,
  keyboard, zoom, and screen-reader-relevant semantics.

### G6 Security, performance, and soak

- Cover traversal, symlink escape, zip-slip, decompression bomb, oversized and
  unknown-schema packages, hash tamper, secret/path leakage, unauthorized
  capabilities, cross-run reads, malicious artefacts/prompts, approval replay,
  and invocation-ID reuse.
- Targets: 100 nodes/180 edges save+compile p95 <= 500 ms; 500 nodes/900 edges
  compile <= 2 s; UI projection p95 <= 2 s; 10 concurrent deterministic runs
  without cross-talk; 100 sequential runs without leaked processes or corrupt
  state; four-hour event reconnect without loss or duplicate application.

### G7 final integration

- Rerun all deterministic gates and the Lenovo golden journey on a clean
  `Lenovo-debug` revision.
- Confirm all recorded bugs are fixed with regression coverage, no blocker is
  open, and replay evidence is complete.
- Bring the verified commit series to mother `main` only now, run the relevant
  mother regression suite, and confirm exact commit provenance.

## Golden fixtures

Maintain deterministic fixtures for minimal two-step, parallel fan-in,
preflight, approval, retry-success, retry-exhausted, pause/resume, mid-run stop,
missing capability, corrupt package, schema migration, and concurrent-edit
isolation. Real-model tests cover only a small safe subset and assert structure,
state, and artefact schema rather than exact prose.

## Evidence and bug loop

Every path carries `canvas_id`, `canvas_revision`, `compile_id`,
`package_sha256`, `submission_id`, `run_id`, `step_id`, `attempt`,
`approval_request_id`, `invocation_id`, `artifact_sha256`, and
`event_sequence` when applicable.

For each failure:

1. Preserve the earliest divergent event and a redacted replay bundle.
2. Classify it as Authoring, Compiler, Save, Submit, Scheduler, Worker,
   Approval, Artefact, Projection, Security, Performance, or Harness.
3. Record one deduplicated loop issue.
4. Add the smallest automated regression that reproduces it.
5. Fix only the authoritative component on `Lenovo-debug`.
6. Commit, reload the Lenovo Developer Runtime, rerun the exact path and its
   neighboring contract tests, then resolve the issue only with evidence.

A replay bundle contains the Canvas snapshot, manifest, workflow, capabilities,
state, events, artefact index, server stderr, browser trace, and screenshots,
with secrets and user content redacted.

## Exit condition

The loop ends only when all seven functional gates pass, every discovered bug
is resolved or proven to be a harness/non-product incident, the complete real
Lenovo golden journey passes, no blocker issue or wait remains, `Lenovo-debug`
is clean, the verified commit series has been integrated to mother `main`, and
the mother regression suite passes. The loop must not stop merely because a
feature is absent, a mocked test is green, or a service health endpoint is 200.
