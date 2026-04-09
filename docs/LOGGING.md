# Nagare Logging and Diagnostics Contract

## Purpose

Logging is part of the product contract for `nagare-core` and `nagare-viz`.
Phase 0 defines the minimum event model now so extraction and GUI work do not
invent incompatible observability later.

## Goals

- make a broken run diagnosable after the fact
- correlate CLI, engine, handlers, API, and GUI activity
- preserve enough structure for later live status views
- keep the event names stable enough to test against fixtures

## Correlation Identifiers

Every event should include these identifiers where applicable:

- `run_id`: immutable workflow run identifier
- `trace_id`: correlation identifier spanning a run or API request chain
- `request_id`: per-request identifier for API/GUI initiated operations
- `step_id`: workflow step identifier for step-scoped events
- `workflow_id`: workflow definition identifier when available
- `workflow_path`: source file path or logical workflow reference

## Required Event Envelope

All structured events should be serializable to JSONL with this envelope:

```json
{
  "timestamp": "2026-04-03T12:00:00Z",
  "level": "INFO",
  "component": "engine.runner",
  "event": "step.started",
  "message": "Executing step",
  "run_id": "run-123",
  "trace_id": "trace-123",
  "request_id": "req-456",
  "workflow_id": "smoke-test",
  "workflow_path": "tests/fixtures/smoke_test.yaml",
  "step_id": "step_write",
  "duration_ms": 0,
  "error_code": null,
  "error_message": null,
  "data": {}
}
```

## Event Names

### Run lifecycle

- `run.created`
- `run.preflight.started`
- `run.preflight.completed`
- `run.confirmed`
- `run.started`
- `run.paused`
- `run.resumed`
- `run.completed`
- `run.failed`
- `run.cancelled`

### Step lifecycle

- `step.ready`
- `step.started`
- `step.waiting_human`
- `step.resumed`
- `step.retrying`
- `step.completed`
- `step.failed`
- `step.cancelled`
- `step.skipped`

### Validation and codec

- `workflow.load.started`
- `workflow.load.completed`
- `workflow.load.failed`
- `workflow.validate.started`
- `workflow.validate.completed`
- `workflow.validate.failed`
- `workflow.export.started`
- `workflow.export.completed`
- `workflow.export.blocked`
- `workflow.fidelity.warning`

### Adapter and integration

- `handler.invoke.started`
- `handler.invoke.completed`
- `handler.invoke.failed`
- `notifier.send.started`
- `notifier.send.completed`
- `notifier.send.failed`
- `evaluator.run.started`
- `evaluator.run.completed`
- `evaluator.run.failed`

### API and GUI

- `api.request.started`
- `api.request.completed`
- `api.request.failed`
- `gui.import.started`
- `gui.import.completed`
- `gui.import.failed`
- `gui.export.started`
- `gui.export.completed`
- `gui.export.blocked`
- `gui.poll.failed`

## Persistence

Required persistence strategy:

- per-run JSONL event stream stored alongside run artifacts
- process log for long-lived CLI/API services
- browser console logging in development for `nagare-viz`

Recommended file locations later:

- `flow/runs/{run_id}/events.jsonl`
- `flow/runs/{run_id}/state.json`
- `flow/runs/{run_id}/artifacts/`
- service-level rotating log for API processes

Compatibility note:

- existing `evaluation_events.jsonl` should remain readable or be mapped forward

## Runtime Snapshot Shape

The API and GUI will need a stable read model separate from the editor draft.

Minimum runtime snapshot shape:

```json
{
  "run_id": "run-123",
  "workflow_id": "smoke-test",
  "workflow_version": "1.0.0",
  "status": "RUNNING",
  "created_at": "2026-04-03T12:00:00Z",
  "updated_at": "2026-04-03T12:05:00Z",
  "current_steps": ["step_write"],
  "completed_steps": [],
  "failed_steps": [],
  "waiting_human_steps": [],
  "step_status": {
    "step_write": {
      "status": "RUNNING",
      "attempt": 1,
      "started_at": "2026-04-03T12:04:55Z",
      "ended_at": null,
      "artifacts": {},
      "error": null
    }
  }
}
```

Rules:

- snapshots are immutable views of a real run
- editor drafts must not overwrite or masquerade as snapshots
- GUI overlays must use run snapshot ids, not mutable node objects

## Layer Coverage

- CLI: command name, workflow path, parsed flags, created run id, fatal exit
- Engine: state transitions, scheduler decisions, retries, pause/resume, artifact registration
- Handler: backend name, latency, exit code, stderr/stdout summary, timeout
- Notifier/Evaluator: downstream target, latency, success/failure, retry context
- API: request/response status, validation failures, serialization failures, latency
- GUI: import/export path, validation findings, fidelity warnings, polling errors

## Phase 1 Exit Criteria Derived From This Doc

- event envelope implemented in code
- event names emitted for run and step lifecycle
- JSONL persistence working for at least one fixture workflow
- runtime snapshot available in a stable shape for tests
