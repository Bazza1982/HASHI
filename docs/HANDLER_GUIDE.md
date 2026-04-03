# Nagare Handler Guide

`nagare` executes workflow steps through a narrow `StepHandler` protocol. The engine owns DAG scheduling, state persistence, artifacts, and event logging. Handlers only execute one step and return a structured result.

## Protocol

Handlers must implement:

```python
def execute(
    agent_id: str,
    task_message: dict,
    agent_md_path: str,
    timeout_seconds: int = 600,
    backend: str = "claude-cli",
    model: str = "",
) -> dict:
    ...
```

Expected success shape:

```json
{
  "status": "completed",
  "artifacts_produced": {
    "quote": "/abs/path/to/output.txt"
  },
  "summary": "brief human-readable result"
}
```

Expected failure shape:

```json
{
  "status": "failed",
  "error_type": "cli_error",
  "error_message": "worker invocation failed",
  "suggested_fix": "check backend setup"
}
```

## Input Contract

`task_message["payload"]` includes:

- `step_id`: stable workflow step identifier
- `prompt`: rendered task prompt
- `input_artifacts`: resolved upstream artifact paths
- `output_spec`: declared artifact keys and relative paths from workflow YAML
- `params`: resolved step parameters

Handlers should treat `task_message` as immutable input and return all output through the result payload.

## Built-in Handlers

- `nagare.handlers.SubprocessStepHandler`: production path that calls an external CLI backend.
- `nagare.handlers.DeterministicStepHandler`: test-only path for packaging and CI smoke runs. It creates declared artifacts locally without invoking a model.

## Design Rules

- Keep handler logic side-effect scoped to step execution.
- Return absolute artifact paths when possible.
- Do not mutate `state.json` or `events.jsonl` directly; the engine owns those files.
- Preserve unknown workflow fields by ignoring them rather than failing inside the handler layer.
- Emit extra adapter-specific logs outside `nagare-core` when integrating with a host application.

## Minimal Example

```python
from pathlib import Path


class EchoHandler:
    def execute(self, agent_id, task_message, agent_md_path, timeout_seconds=600, backend="claude-cli", model=""):
        del agent_id, agent_md_path, timeout_seconds, backend, model
        step_id = task_message["payload"]["step_id"]
        output_path = Path("/tmp") / f"{step_id}.txt"
        output_path.write_text(f"completed {step_id}\n", encoding="utf-8")
        return {
            "status": "completed",
            "artifacts_produced": {"output": str(output_path)},
            "summary": f"completed {step_id}",
        }
```

## Verification

- Run `pytest -q tests/contract/test_nagare_core_contract.py tests/contract/test_nagare_cli_smoke_contract.py`
- Run `python -m nagare.cli run tests/fixtures/smoke_test.yaml --yes --silent --smoke-handler`
