# Migration From HASHI

Phase 3 establishes HASHI as a host application around the extracted `nagare` core instead of keeping HASHI-specific behavior inside the engine package.

## Current boundary

- `nagare.engine.runner.FlowRunner` owns workflow loading, DAG execution, state persistence, artifact registration, and the stable event stream.
- `flow.adapters.hashi.HASHIStepHandler` owns HASHI worker dispatch and emits adapter-scoped events before and after each worker invocation.
- `flow.adapters.hashi.HChatNotifier` owns HChat delivery and logs notification attempts with the same run-scoped correlation metadata.
- `flow.adapters.hashi.HASHIEvaluator` owns the optional HASHI evaluator hook and keeps it outside `nagare-core`.
- `flow.engine.flow_runner.FlowRunner` is now a compatibility wrapper that binds those adapters to the core runner.

## Maintainer expectations

- New HASHI-specific integrations should be added in `flow.adapters`, not inside `nagare/`.
- Changes to worker dispatch, notification semantics, or evaluator behavior should preserve the Phase 1 event contract in [`docs/LOGGING.md`](/home/lily/projects/hashi/docs/LOGGING.md).
- Compatibility work should be verified through the HASHI wrapper path, not only by instantiating `nagare.engine.runner.FlowRunner` directly.
- When adding host-specific dependencies, keep the protocol boundary narrow enough that `nagare-core` can still run without importing HASHI modules.

## Verification target

Phase 3 is considered healthy when representative workflows still execute through `flow.engine.flow_runner.FlowRunner`, the adapter events appear in `flow/runs/{run_id}/events.jsonl`, and the run can still be inspected through the existing HASHI CLI.

## Install and development note

Phase 8 keeps Nagare in the HASHI monorepo while making the extracted package path usable:

- Python package entrypoint: `nagare`
- Editor app: [`nagare-viz/`](/home/lily/projects/hashi/nagare-viz)
- Core install path: `pip install .`
- Core smoke command: `python -m nagare.cli --help`

This is release-ready inside the monorepo, but it is not yet a separately published `nagare-core` distribution with its own repository lifecycle.
