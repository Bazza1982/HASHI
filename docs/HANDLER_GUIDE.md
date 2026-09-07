# Nagare Handler Guide

A `StepHandler` executes one workflow step. The engine owns scheduling, retries, state, artifacts,
quality gates, and terminal status.

## Protocol

```python
def execute(
    agent_id: str,
    task_message: dict,
    agent_md_path: str,
    backend: str = "claude-cli",
    model: str = "",
) -> dict:
    ...
```

Success:

```json
{
  "status": "completed",
  "artifacts_produced": {"report": "report.json"},
  "summary": "report created and verified"
}
```

Failure:

```json
{
  "status": "failed",
  "error_type": "invalid_input",
  "error_message": "required source is unreadable",
  "suggested_fix": "supply a readable source"
}
```

The explicit statuses are `completed`, `failed`, `recovered`, and `unrecoverable`. A missing or
unknown status fails closed.

## Task Message

Handlers receive:

- `task_id`: unique dispatch request ID;
- `run_id` and `workflow_id`;
- `worker_workspace`: run-scoped output directory;
- `payload.step_id`;
- rendered `payload.prompt`;
- resolved `payload.input_artifacts`;
- declared `payload.output_spec`;
- rendered `payload.params`.

Treat the message as immutable. Do not modify `state.json`, `events.jsonl`, or the artifact index.

## Artifact Paths

Path authority differs by handler:

- `SubprocessStepHandler` requires relative paths inside `worker_workspace`. Absolute paths,
  traversal, and symlink escapes are rejected before completion.
- `DeterministicStepHandler` resolves declared relative paths inside its own run workspace.
- A host-registered callable is already trusted in-process code and may return an absolute path.
  It should normally use `task_message["worker_workspace"]` so outputs remain run-scoped.

The runner verifies that reported paths exist, that required declared keys are present, and that no
undeclared keys are reported. Verified files/directories are copied into the artifact store.

## Callable Example

```python
import json
from pathlib import Path


def write_report(task_message: dict) -> dict:
    workspace = Path(task_message["worker_workspace"])
    workspace.mkdir(parents=True, exist_ok=True)
    output = workspace / "report.json"
    output.write_text(json.dumps({"ok": True}) + "\\n", encoding="utf-8")
    return {
        "status": "completed",
        "artifacts_produced": {"report": str(output)},
        "summary": "wrote and parsed report.json",
    }
```

Register it through `CallableStepHandler` or `RoutingStepHandler` and declare the worker backend as
`callable`. Callable code executes inside the Nagare process and is not sandboxed or pre-emptible.

## Subprocess Backends

The built-in subprocess handler supports only `claude-cli` and `codex-cli`. `model` is passed
through when supplied; an empty value uses the CLI's configured default. A worker must physically
create each output and place the final structured JSON in its response.

The `_stop` signal terminates a running subprocess. There is no implicit execution timeout or fixed
retry ceiling.

## Events

Handlers emit `handler.invoke.started|completed|failed`. The worker request belongs in `request_id`;
the DAG step belongs in `step_id`. Callable setup has separate `handler.callable.setup_*` events.

## Verification

```bash
python -m pytest -q tests/contract/test_subprocess_handler_contract.py
python -m pytest -q tests/contract/test_nagare_core_contract.py
python -m nagare.cli run tests/fixtures/smoke_test.yaml --yes --silent --smoke-handler
```
