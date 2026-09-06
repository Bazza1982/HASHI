# Nagare Logging and Diagnostics Contract

Nagare persists structured evidence for every run. This document describes the events and snapshot
shape emitted by the current implementation; it is not a roadmap or an aspirational event list.

## Correlation

The canonical JSONL envelope contains:

```json
{
  "timestamp": "2026-09-06T04:00:00+00:00",
  "level": "INFO",
  "component": "engine.runner",
  "event": "step.started",
  "message": "Step execution started",
  "run_id": "run-example-20260906-abc12345",
  "trace_id": "trace-id",
  "request_id": "task-request-id",
  "workflow_id": "example",
  "workflow_path": "workflow.yaml",
  "step_id": "draft",
  "duration_ms": null,
  "error_code": null,
  "error_message": null,
  "data": {}
}
```

- `run_id` identifies the persisted run.
- `trace_id` correlates the run and its adapter activity.
- `request_id` identifies one API request or worker dispatch.
- `step_id` is the workflow step ID, never the worker-dispatch request ID.
- Fields that do not apply remain `null`; event-specific values belong in `data`.

## Events Emitted Today

### Runner and CLI

- `run.created`
- `workflow.load.started`
- `workflow.load.completed`
- `run.preflight.started` (CLI path)
- `run.preflight.completed`
- `run.confirmed` (CLI path)
- `run.started`
- `run.paused`
- `run.resumed`
- `run.completed`
- `run.failed`
- `run.cancelled`
- `run.escalated`

### Steps and handlers

- `step.started`
- `step.retrying`
- `step.waiting_human`
- `step.completed`
- `step.failed`
- `step.skipped`
- `handler.invoke.started`
- `handler.invoke.completed`
- `handler.invoke.failed`
- `handler.callable.setup_attempt`
- `handler.callable.setup_completed`
- `handler.callable.setup_failed`

Subprocess and callable handlers both use `handler.invoke.*`. The subprocess handler records its
unique task ID as `request_id` and the DAG step as `step_id`.

### Local API and HASHI adapters

- `api.request.completed` for successful run-inspection requests
- `adapter.step_handler.started|completed|failed`
- `adapter.notifier.started|completed|failed`
- `adapter.evaluator.started|completed|failed`

Names not listed here are not guaranteed to be emitted. Consumers must tolerate additional event
names and fields, but must not depend on a roadmap-only event.

## Persistence

Each run owns:

```text
flow/runs/<run_id>/
├── state.json
├── events.jsonl
├── evaluation_events.jsonl
├── artifacts/
├── workers/
└── logs/flow_runner.log
```

`events.jsonl` is canonical. Writes are serialized within a `RunEventLogger` instance.
`evaluation_events.jsonl` is a compatibility projection for the HASHI evaluator and contains only
events with a defined legacy mapping. It is not a second complete event stream.

Runtime data is local and may contain workflow paths, errors, artifact metadata, and model output
previews. It is excluded from release packages and should be handled as operational data.

## Runtime Snapshot

`TaskState.get_runtime_snapshot()` returns an immutable read model:

```json
{
  "run_id": "run-example",
  "workflow_id": "example",
  "workflow_version": "1.0.0",
  "status": "RUNNING",
  "created_at": "...",
  "updated_at": "...",
  "current_steps": ["draft"],
  "completed_steps": [],
  "failed_steps": [],
  "waiting_human_steps": [],
  "step_status": {
    "draft": {
      "status": "RUNNING",
      "attempt": 1,
      "started_at": "...",
      "ended_at": null,
      "artifacts": {},
      "error": null
    }
  }
}
```

`attempt` is the number of actual handler dispatches. Pending or skipped steps have attempt `0`;
the first dispatch is `1`, and every recovery re-execution increments it. Editor drafts must never
be presented as runtime snapshots.

## Diagnostic Boundaries

- A constructor failure while loading YAML can occur after `workflow.load.started` and before a
  run state exists; callers must also retain the raised validation error.
- Pause and stop are live-process controls, not crash recovery.
- Callable code is trusted in-process code and cannot be pre-empted during execution.
- Event persistence proves what Nagare observed; it does not prove semantic quality by itself.

## Verification

The logging, attempt, skip, retry, handler-correlation, API, and HASHI adapter contracts are covered
by the contract test suite under `tests/contract/`.
