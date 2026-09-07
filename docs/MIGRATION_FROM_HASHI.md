# Migrating Between HASHI Flow Imports and Nagare

Nagare is the reusable workflow core. HASHI remains the host that supplies HChat delivery, worker
routing, and post-run evaluation. Compatibility imports are retained so existing HASHI code can
migrate without a flag day.

## Current Boundary

| Responsibility | Current owner |
|---|---|
| YAML loading and validation | `nagare.yaml` and `nagare.engine.FlowRunner` |
| DAG execution, state, artifacts, signals | `nagare.engine` |
| Subprocess and callable execution | `nagare.handlers` |
| Stable run events | `nagare.logging` |
| HChat, HASHI routing, HASHI evaluator | `flow.adapters.hashi` |
| Legacy import compatibility | `flow.engine` |

`flow.engine.flow_runner.FlowRunner` wraps the core runner with HASHI adapters.
`flow.engine.task_state` and `flow.engine.worker_dispatcher` remain compatibility imports.
New reusable engine behavior belongs under `nagare/`; new HASHI-specific integration belongs under
`flow/adapters/`.

## Import Migration

Reusable code should prefer:

```python
from nagare.engine.runner import FlowRunner
from nagare.engine.state import TaskState
from nagare.handlers import RoutingStepHandler
```

HASHI-hosted callers that require HChat or the HASHI evaluator should continue to use:

```python
from flow.engine.flow_runner import FlowRunner
```

Do not import HASHI modules from inside `nagare`. A publication contract scans the package AST and
rejects dependencies on `flow`, `hashi`, or `tools`.

## Behavioral Differences to Account For

- Workflow definitions are validated at load time and reject duplicate YAML keys, unsafe IDs,
  invalid DAGs, unsupported backends, artifact-contract errors, and retired runtime fields.
- CLI worker `model` is optional. Omitting it delegates model selection to the installed CLI.
- Subprocess artifacts must be relative to the run-scoped worker workspace.
- Free-form top-level `success_criteria` and `output` are host-facing metadata. Required artifacts
  and supported automatic quality gates enforce core completion.
- `nagare resume` clears a pause signal for a live process. It does not reconstruct a dead run.
- Evaluation is an injected protocol; standalone Nagare does not advertise an evaluator CLI.

## Package and Data Boundary

The Python wheel/sdist contains the Nagare package and required Flow assets only. Run directories,
callable deliveries, evaluator scores, improvement proposals, candidate history, private skills,
operator configuration, and Workbench data are excluded.

The editor is a separate npm workspace. Its source is published; generated TypeScript/Vite output,
dependencies, coverage, and local caches are not.

## Verification

Before removing a compatibility import, verify both paths:

```bash
python -m pytest -q tests/contract/test_nagare_core_contract.py
python -m pytest -q tests/contract/test_hashi_adapter_contract.py
python -m nagare.cli --help
```

The migration is complete for a caller only when it does not rely on HASHI notification or
evaluation side effects. Compatibility modules remain supported for host callers.
