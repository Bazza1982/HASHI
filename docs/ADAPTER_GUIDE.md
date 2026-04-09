# Nagare Adapter Guide

Use adapters when a host application needs to plug its own worker dispatch, notification, or evaluation behavior into `nagare-core` without forking the engine.

## Boundary

Core package responsibilities:

- workflow loading and validation
- DAG execution
- run state persistence
- artifact registration
- stable event stream
- read-only runtime inspection API

Host adapter responsibilities:

- backend-specific step execution
- external notifications
- optional run evaluation
- host-only dependencies and auth

## HASHI Reference Implementation

HASHI’s adapter layer lives in `flow/adapters/hashi.py`:

- `HASHIStepHandler`
- `HChatNotifier`
- `HASHIEvaluator`

The compatibility entrypoint is `flow.engine.flow_runner.FlowRunner`, which wraps `nagare.engine.runner.FlowRunner` and binds adapter runtime context.

## Integration Rules

- Keep adapter imports out of `nagare/`.
- Preserve `run_id`, `trace_id`, `workflow_id`, and `request_id` across boundaries.
- Emit adapter-scoped events so host failures are visible without changing core event names.
- Prefer composition over inheritance: wrap core protocols rather than modifying engine internals.

## Recommended Bind Pattern

If an adapter needs runtime metadata, expose:

```python
def bind_runtime_context(self, *, run_id: str, workflow_id: str | None, trace_id: str, event_logger) -> None:
    ...
```

The HASHI wrapper calls this once after the workflow is loaded.

## Verification

- Run `pytest -q tests/contract/test_hashi_adapter_contract.py tests/contract/test_nagare_core_contract.py`
- Confirm adapter events appear in `flow/runs/<run_id>/events.jsonl`
- Confirm the same run is inspectable through `nagare.api`
