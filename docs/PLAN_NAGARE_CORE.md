# Nagare-Core: Independent Workflow Engine
## Product Requirements and Execution Plan
**Version:** 2.0 Execution Draft
**Author:** Ying (plan) | **Implementer:** Baymax
**Date:** 2026-04-03
**Status:** Execution-ready

---

## 1. Product Vision

Extract Nagare from HASHI into a standalone, pip-installable Python package (`nagare-core`) with a React-based visual workflow editor (`nagare-viz`) built on `xyflow/xyflow`.

Nagare is a HITL-native deterministic workflow engine. The objective is not to imitate LangChain, n8n, or BPMN tooling. The objective is:

- keep the current YAML workflow model usable as-is
- preserve existing execution behavior
- make Nagare reusable outside HASHI
- add a safe visual editor without corrupting workflow semantics
- build enough observability that failures are diagnosable after the fact

This plan is written so implementation can begin immediately, phase by phase, with explicit entry criteria, deliverables, and exit gates.

---

## 2. Product Requirements

### 2.1 Non-Negotiable

| # | Requirement |
|---|---|
| R1 | Zero HASHI dependency: `nagare-core` must run without importing from `hashi/` |
| R2 | Feature parity: existing Nagare workflows must execute with equivalent behavior after extraction |
| R3 | Abstract notification interface: hardcoded `hchat_send` replaced by `Notifier` protocol |
| R4 | Abstract execution interface: hardcoded worker dispatch replaced by `StepHandler` protocol |
| R5 | YAML workflow format remains accepted without mandatory migration |
| R6 | Visual editor uses `xyflow/xyflow` |
| R7 | Visual editor imports and exports the same YAML format the engine consumes |
| R8 | Structured logging and diagnostics exist across CLI, engine, adapters, API, and GUI |
| R9 | YAML round-trip contract is explicit and testable |
| R10 | Raw YAML editing remains available as an escape hatch |

### 2.2 Important

| # | Requirement |
|---|---|
| R11 | Evaluator becomes optional, not a core dependency |
| R12 | CLI contract remains recognizable: `run`, `status`, `list`, `resume` |
| R13 | Existing run history and `evaluation_events.jsonl` remain readable or compatible |
| R14 | Visual editor can show live run state after the core event schema stabilizes |
| R15 | Runtime state and editable graph state are separated to avoid misleading UI |
| R16 | Unsupported YAML is preserved and surfaced, not silently dropped |

### 2.3 Deferred

| # | Requirement |
|---|---|
| D1 | Workflow template marketplace |
| D2 | Multi-user collaborative editing |
| D3 | Visual workflow diff |
| D4 | Full embedded log browser in the editor |
| D5 | Mobile-first workflow editing |

---

## 3. Design Principles

### 3.1 Engine Principles

- The engine is the source of execution truth.
- YAML remains the source of workflow definition truth.
- The GUI is an editor for that YAML, not an alternative execution model.
- Public protocols must carry enough semantics to preserve real behavior, not just nominal structure.
- Logging is part of the runtime contract, not an afterthought.

### 3.2 GUI Principles

- The visual graph must never silently change workflow meaning.
- Canvas layout must never imply execution order if execution is actually defined by `depends`.
- Unsupported fields must be preserved, surfaced, and recoverable.
- The UI must degrade safely when fidelity cannot be guaranteed.
- Raw YAML editing is mandatory for advanced or edge-case workflows.

### 3.3 Delivery Principles

- Freeze contracts before large code moves.
- Verify real workflows, not toy examples only.
- Treat round-trip safety as a first-class feature.
- Delay API/server ambitions until the event model is stable.
- Prefer phased release gates over broad parallel implementation.

---

## 4. Target Architecture

### 4.1 Package Structure

```text
nagare-core/
├── pyproject.toml
├── nagare/
│   ├── engine/
│   │   ├── runner.py
│   │   ├── state.py
│   │   ├── artifacts.py
│   │   ├── preflight.py
│   │   ├── signals.py
│   │   ├── snapshot.py
│   │   └── validation.py
│   ├── logging/
│   │   ├── config.py
│   │   ├── events.py
│   │   ├── correlation.py
│   │   └── serializers.py
│   ├── protocols/
│   │   ├── step_handler.py
│   │   ├── notifier.py
│   │   └── evaluator.py
│   ├── handlers/
│   │   ├── subprocess_handler.py
│   │   └── callable_handler.py
│   ├── schema/
│   │   └── workflow.schema.yaml
│   ├── api/
│   │   ├── app.py
│   │   ├── runs.py
│   │   └── models.py
│   ├── cli.py
│   └── __init__.py
├── nagare-viz/
│   ├── package.json
│   ├── src/
│   │   ├── App.tsx
│   │   ├── components/
│   │   │   ├── WorkflowCanvas.tsx
│   │   │   ├── StepNode.tsx
│   │   │   ├── StepConfigPanel.tsx
│   │   │   ├── PreflightEditor.tsx
│   │   │   ├── YamlEditor.tsx
│   │   │   ├── UnsupportedFieldsPanel.tsx
│   │   │   ├── ValidationPanel.tsx
│   │   │   ├── DiagnosticsPanel.tsx
│   │   │   ├── RunStatusOverlay.tsx
│   │   │   └── Toolbar.tsx
│   │   ├── lib/
│   │   │   ├── yamlCodec.ts
│   │   │   ├── dagValidator.ts
│   │   │   ├── roundTrip.ts
│   │   │   ├── runtimeMapper.ts
│   │   │   ├── layout.ts
│   │   │   ├── logger.ts
│   │   │   └── workflowSchema.ts
│   │   └── api/
│   │       └── nagareApi.ts
├── tests/
│   ├── fixtures/
│   ├── contract/
│   ├── engine/
│   ├── adapters/
│   └── viz/
└── docs/
    ├── MIGRATION_FROM_HASHI.md
    ├── HANDLER_GUIDE.md
    ├── LOGGING.md
    └── ROUND_TRIP_CONTRACT.md
```

### 4.2 Protocols

The current protocol sketch is directionally right but too thin. The execution boundary must preserve semantics like timeout, retries, structured failure, cancellation, and artifacts.

Minimum direction for `StepHandler`:

```python
from typing import Any, Protocol

class StepHandler(Protocol):
    def execute(
        self,
        *,
        run_id: str,
        step_id: str,
        prompt: str,
        context: dict[str, Any],
        config: dict[str, Any],
        timeout_seconds: int | None,
        logger: Any | None = None,
    ) -> dict[str, Any]:
        """Return a structured result including status, outputs, and failure metadata."""
        ...
```

Required result shape at the protocol boundary:

- `status`: `success` | `failed` | `cancelled` | `waiting_human`
- `outputs`: structured result payload
- `artifacts`: references or metadata for stored artifacts
- `error`: structured error details when not successful
- `timing`: start/end/duration
- `meta`: backend/model/worker information if relevant

`Notifier` and `Evaluator` remain smaller interfaces, but their invocations must be logged with correlation metadata.

### 4.3 Logging and Diagnostics Strategy

Logging must support local debugging, postmortem analysis, and GUI/API correlation.

Required fields on all structured log events where applicable:

- `timestamp`
- `level`
- `component`
- `event`
- `run_id`
- `step_id`
- `trace_id`
- `request_id`
- `workflow_path`
- `duration_ms`
- `error_code`
- `error_message`

Logging coverage by layer:

- CLI: command invocation, args summary, workflow path, created `run_id`, fatal exit
- Engine: parse/validate/schedule/execute/retry/pause/resume/abort/state transition
- Handler: invocation, timeout, stderr/stdout summaries, exit code, retry context
- Notifier/Evaluator: attempt, success/failure, downstream latency
- API: endpoint, request ID, serialization failures, response status, latency
- GUI: import/export, parser errors, validation warnings, run polling failures, fidelity warnings

Persistence strategy:

- per-run JSONL event stream stored with artifacts
- rotating app logs for long-lived processes
- browser dev logs in development
- optional downloadable diagnostic bundle later

### 4.4 YAML Round-Trip Contract

The editor must follow this contract:

1. Existing YAML files must load even if they contain unknown fields.
2. Unknown top-level and step-level fields are preserved unless explicitly deleted by the user.
3. GUI layout metadata is stored in a dedicated extension block owned by the editor.
4. If comments or ordering cannot be preserved on a given edit path, the UI must warn before export.
5. Export must preserve execution semantics, not merely produce schema-valid YAML.
6. Unsupported fields must be visible in the UI, even if not editable through forms.
7. Raw YAML mode must allow direct edits and recovery from form limitations.
8. Runtime overlays bind to immutable run snapshots, never to the mutable editor draft.

Recommended layout metadata block:

```yaml
x-nagare-viz:
  version: 1
  nodes:
    draft_outline:
      position:
        x: 120
        y: 240
```

Rules for this block:

- `x-nagare-viz` must be ignored by the engine
- GUI-owned metadata must not leak into runtime semantics
- future editor metadata changes must be versioned

---

## 5. Visual Editor Design

### 5.1 Technology Choice

Decision: use `xyflow/xyflow`.

Why:

- MIT licensed
- strong React and TypeScript fit
- flexible node and edge model
- good custom editor ergonomics
- avoids coupling to another workflow engine's semantics

Additions:

- `ELKjs` for auto-layout
- schema-driven form generation where feasible
- raw YAML editor from the first usable milestone

### 5.2 YAML to Visual Mapping

| YAML | Visual Model |
|---|---|
| `step.id` | `node.id` |
| `step.prompt` | `node.data.prompt` |
| `step.depends[]` | edges |
| `step.agent` | badge or label |
| `step.backend` / `step.model` | backend indicator |
| `step.wait_for_human` | node state indicator |
| `step.quality_gate` | node state indicator |
| `step.timeout` | config field |
| unsupported fields | preserved metadata plus unsupported-fields panel |
| `x-nagare-viz` | node positions and editor metadata |

### 5.3 GUI Risk Controls

These are mandatory controls, not optional polish:

- show a persistent note that execution order is determined by `depends`
- show dependency direction clearly on edges
- separate supported editable fields from preserved unsupported fields
- block silent export when fidelity guarantees degrade
- allow switching to raw YAML at any point
- validate duplicate IDs, missing dependencies, and cycles before export
- expose import/export warnings in a dedicated diagnostics area
- treat current editor graph and runtime snapshot as different objects

### 5.4 GUI Failure Modes to Design For

The editor will fail unless these are explicitly handled:

- malformed YAML that parses partially or not at all
- duplicate step IDs
- references to missing dependencies
- cyclic workflows
- unknown nested handler-specific configuration
- comments or formatting the editor cannot preserve
- workflows that are valid YAML but not representable as a clean DAG
- runtime snapshots from older workflow versions
- step IDs changed after a run has already started

Required UX response:

- import with warnings when safe
- refuse destructive export when unsafe
- point users to raw YAML when form mode is insufficient
- never auto-delete unknown fields to “clean up” the file

---

## 6. Phased Execution Plan

Each phase has a clear objective, work products, implementation tasks, risks, and exit gate. Work should proceed in order. Later phases may begin only when the previous phase exit gate is met or consciously waived.

### Phase 0: Discovery, Contract Freeze, and Fixture Collection

**Objective**

Freeze the real-world behavior and file shapes that the extracted engine and GUI must preserve.

**Why this phase exists**

Without this phase, the team will overfit to the ideal schema and underfit to the messy workflows that actually exist.

**Deliverables**

- real workflow fixture set, including ugly and legacy examples
- contract document for YAML round-trip behavior
- structured event schema draft
- execution parity checklist for current HASHI workflows
- initial risk register

**Implementation tasks**

1. Inventory current Nagare workflow files in HASHI and group them by complexity.
2. Select fixture workflows:
   - minimal happy path
   - multi-step linear
   - branching DAG
   - HITL wait flow
   - quality-gated flow
   - legacy or hand-edited YAML
   - workflow with unknown fields
   - workflow with nested step config
3. Document current runtime outputs and artifacts for each selected fixture.
4. Write `docs/ROUND_TRIP_CONTRACT.md`.
5. Define event names for run lifecycle and step lifecycle.
6. Define the runtime snapshot shape needed later by API and GUI.

**Outputs to create immediately**

- `tests/fixtures/`
- `docs/ROUND_TRIP_CONTRACT.md`
- `docs/LOGGING.md`
- `tests/contract/test_round_trip_contract.py`

**Main risks**

- missing a weird but real workflow shape
- assuming comments and ordering are easy to preserve
- under-specifying the event model

**Exit gate**

- fixture list approved
- round-trip rules written
- event schema draft exists
- at least one contract test in place, even if failing

### Phase 1: Logging and Runtime Contract Foundations

**Objective**

Build the observability and runtime identity layer before extraction so later breakage is diagnosable.

**Deliverables**

- `nagare/logging/` package
- correlation propagation utilities
- event emitter helpers
- immutable runtime snapshot model
- engine-side structured log integration points

**Implementation tasks**

1. Add log config supporting human-readable dev output and JSON structured output.
2. Define correlation fields: `run_id`, `step_id`, `trace_id`, `request_id`.
3. Add helper functions for emitting normalized lifecycle events.
4. Add runtime snapshot data structures:
   - run status
   - step status map
   - timestamps
   - error summaries
   - artifact references
5. Ensure snapshots are immutable after emission.
6. Write tests for correlation propagation and event serialization.

**Main risks**

- logging schema churn after downstream code depends on it
- runtime snapshots coupled too tightly to internal engine objects

**Exit gate**

- logs emitted from a minimal runner path
- runtime snapshot schema documented and tested
- per-run event log can be written and read

### Phase 2: Core Extraction into `nagare-core`

**Objective**

Move the engine into a standalone package without changing workflow behavior.

**Deliverables**

- installable `nagare-core` package
- extracted engine modules
- protocol interfaces
- built-in subprocess handler
- preserved CLI commands

**Implementation tasks**

1. Create `pyproject.toml` and package skeleton.
2. Move core engine files into `nagare/engine/`.
3. Define `StepHandler`, `Notifier`, and `Evaluator` protocols.
4. Replace direct HASHI imports with protocol injection.
5. Move current dispatcher logic into `handlers/subprocess_handler.py`.
6. Preserve CLI entrypoints and output expectations where practical.
7. Integrate structured logging and event emission into:
   - workflow load
   - validation
   - step scheduling
   - step execution
   - retries
   - pause/resume/abort
   - completion and failure
8. Add engine parity tests using fixture workflows.

**Implementation order**

1. package skeleton
2. engine file move
3. protocol layer
4. handler extraction
5. CLI wiring
6. logging integration
7. parity tests

**Main risks**

- hidden HASHI dependencies reappearing through utility imports
- protocol mismatch causing behavioral regressions
- CLI compatibility drift

**Exit gate**

- `nagare run` works on fixture workflows
- no forbidden imports remain in `nagare-core`
- parity test suite passes on selected workflows

### Phase 3: HASHI Adapter and Backward Compatibility Layer

**Objective**

Reconnect HASHI to the new engine with minimal user-visible behavior change.

**Deliverables**

- `HASHIStepHandler`
- `HChatNotifier`
- optional `HASHIEvaluator`
- updated HASHI CLI integration
- adapter-level logging correlation

**Implementation tasks**

1. Create adapter module in HASHI.
2. Wrap current worker dispatch in `HASHIStepHandler`.
3. Wrap `hchat_send` in `HChatNotifier`.
4. Wrap evaluator behavior in an optional plugin adapter.
5. Ensure correlation IDs pass through adapter boundaries.
6. Re-run existing HASHI workflows through the adapter path.
7. Compare runtime outputs, artifacts, and notifications against the baseline.

**Main risks**

- silent divergence in evaluator behavior
- subtle differences in worker invocation context
- adapter swallowing structured errors

**Exit gate**

- representative HASHI workflows run end-to-end through `nagare-core`
- logs show uninterrupted correlation across HASHI and Nagare
- migration notes written for HASHI maintainers

### Phase 4: YAML Codec and Round-Trip Safety Layer

**Objective**

Build the import/export layer before the canvas UI so the hardest GUI problem is solved first.

**Why this phase comes before most UI work**

The hard part is not drawing nodes. The hard part is preserving YAML meaning, unknown fields, and editor-owned metadata without damaging workflows.

**Deliverables**

- `yamlCodec.ts`
- `roundTrip.ts`
- `dagValidator.ts`
- import/export fixture suite
- explicit fidelity warning model

**Implementation tasks**

1. Define internal editor draft model separate from runtime snapshots.
2. Implement YAML import into:
   - supported fields
   - preserved unsupported metadata
   - layout metadata
   - validation warnings
3. Implement YAML export preserving:
   - unknown fields
   - unchanged supported fields
   - GUI metadata in `x-nagare-viz`
4. Detect fidelity degradation cases:
   - comments not preserved
   - unsupported transforms
   - ambiguous field ordering changes
5. Add DAG validation:
   - cycle detection
   - missing dependency checks
   - duplicate ID checks
6. Add golden fixtures:
   - import
   - export
   - re-import
   - compare preserved metadata
7. Add engine execution checks on exported YAML.

**Main risks**

- export normalizes more than intended
- unknown nested fields get detached from their owning step
- GUI metadata pollutes engine semantics

**Exit gate**

- fixture-based round-trip suite passes
- exported YAML from supported edits still executes correctly
- fidelity warnings appear for known unsupported cases

### Phase 5: Visual Editor Foundation

**Objective**

Build the first usable editor on top of the safe YAML codec.

**Deliverables**

- `nagare-viz` app scaffold
- xyflow canvas
- custom step node
- property panel for supported fields
- unsupported-fields panel
- raw YAML editor
- validation and diagnostics panels
- local import/export workflow

**Implementation tasks**

1. Initialize React + TypeScript app.
2. Add `xyflow/xyflow` canvas and custom node rendering.
3. Implement draft state store for nodes, edges, selection, warnings, and dirty status.
4. Add toolbar actions:
   - import YAML
   - validate
   - auto-layout
   - export YAML
   - switch to raw YAML
5. Implement right-side property editor for supported fields only.
6. Implement unsupported-fields panel that shows preserved data by scope.
7. Implement raw YAML editor with:
   - parse feedback
   - schema feedback
   - diff/fidelity warning area
8. Add front-end logging for all import/export/validation failures.

**First usable milestone**

The editor can:

- open an existing workflow
- show nodes and edges
- edit supported fields
- preserve unsupported fields
- export YAML
- run validation
- warn when fidelity cannot be guaranteed

**Main risks**

- UI suggests capabilities broader than the codec safely supports
- property forms become ad hoc and inconsistent
- users mistake canvas movement for runtime order changes

**Exit gate**

- at least one real workflow can be safely edited and exported
- unsupported fields remain intact
- raw YAML mode can recover from form limitations

### Phase 6: GUI Hardening and Workflow Safety

**Objective**

Make the editor resilient enough for regular use on real workflows.

**Deliverables**

- richer validation UX
- import recovery paths
- fidelity warning UX
- diagnostics panel with correlation IDs
- layout persistence and restore
- stronger test coverage for messy workflows

**Implementation tasks**

1. Add validation grouping by severity:
   - blocking
   - warning
   - informational
2. Add import recovery flows for partially representable workflows.
3. Persist and restore layout metadata cleanly.
4. Add diagnostics panel showing:
   - validation issues
   - parser issues
   - export warnings
   - current correlation ID
5. Test workflows with hand-edited YAML and unsupported fields.
6. Add visual affordances clarifying dependency direction and non-semantic layout.

**Main risks**

- users trust the editor too much on unsupported workflows
- warnings are too weak and get ignored
- layout persistence creates noisy YAML churn

**Exit gate**

- editor behaves predictably on complex fixtures
- destructive export paths are blocked or heavily warned
- diagnostics are sufficient to file actionable bug reports

### Phase 7: API and Live Run Visualization

**Objective**

Add runtime observation after the engine event model and editor draft model are stable.

**Deliverables**

- lightweight API server in `nagare-core`
- API models based on immutable snapshots
- `nagareApi.ts`
- run status overlay in GUI
- correlated API request logging

**Implementation tasks**

1. Choose a lightweight server framework and keep scope narrow.
2. Expose read-focused endpoints first:
   - `GET /runs/{id}`
   - `GET /runs/{id}/events`
   - `GET /runs/{id}/artifacts`
3. Add write/control endpoints only if necessary later.
4. Implement GUI polling or streaming against immutable snapshots.
5. Map runtime step states onto current editor nodes by stable step ID only.
6. Handle mismatches between current draft and active run explicitly.
7. Log API latency, serialization failures, and snapshot version mismatches.

**Main risks**

- current editor graph does not match the run being viewed
- API scope expands into job control too early
- snapshot versioning is underdesigned

**Exit gate**

- active runs can be observed without mutating editor state
- API logs correlate with engine run logs
- mismatched workflow/run situations are clearly communicated

### Phase 8: Packaging, Docs, and Release Readiness

**Objective**

Make the result installable, documented, and maintainable.

**Deliverables**

- install and migration docs
- handler implementation guide
- adapter guide for downstream integrators
- release checklist
- basic smoke CI for package and editor

**Implementation tasks**

1. Write `MIGRATION_FROM_HASHI.md`.
2. Write `HANDLER_GUIDE.md`.
3. Document logging and event schemas.
4. Add installation and development instructions for both packages.
5. Add smoke checks for:
   - package install
   - CLI run
   - fixture tests
   - editor build
6. Prepare release notes and known limitations list.

**Exit gate**

- package install path works
- docs cover the adapter model and known fidelity limitations
- CI catches obvious packaging or build regressions

---

## 7. Immediate Implementation Backlog

If implementation starts now, work should begin in this order:

1. Create fixture inventory and freeze real workflow examples.
2. Write `ROUND_TRIP_CONTRACT.md` and `LOGGING.md`.
3. Define event names and runtime snapshot schema.
4. Build the `nagare/logging/` foundation.
5. Extract engine code into `nagare-core`.
6. add parity tests before moving to the GUI.
7. Build `yamlCodec.ts` and round-trip tests before the full editor UI.
8. Build the first editor with raw YAML mode included from the start.

This order is intentional. Starting with the canvas before the codec and contracts would be the fastest way to produce a broken editor.

---

## 8. File-by-File Migration Map

| HASHI Source | Nagare Target | Notes |
|---|---|---|
| `flow/engine/flow_runner.py` | `nagare/engine/runner.py` | remove HASHI-specific imports, add lifecycle logging |
| `flow/engine/task_state.py` | `nagare/engine/state.py` | preserve behavior, add event hooks only where needed |
| `flow/engine/artifact_store.py` | `nagare/engine/artifacts.py` | preserve path and artifact semantics |
| `flow/engine/preflight.py` | `nagare/engine/preflight.py` | preserve workflow question semantics |
| `flow/engine/worker_dispatcher.py` | `nagare/handlers/subprocess_handler.py` | adapt into protocol-backed handler |
| `flow/workflows/schema/workflow.schema.yaml` | `nagare/schema/workflow.schema.yaml` | schema copied unchanged initially |
| `flow/flow_cli.py` | `nagare/cli.py` | preserve user-facing commands where practical |
| HASHI evaluator modules | HASHI adapter layer | remain outside core, implement evaluator protocol |
| new | `nagare/logging/*` | new shared observability layer |
| new | `nagare/engine/snapshot.py` | immutable runtime snapshots |
| new | `nagare-viz/src/lib/yamlCodec.ts` | core GUI safety layer |

---

## 9. Acceptance Criteria

The plan is not complete until all of the following are true:

- `nagare-core` installs and imports cleanly
- no `hashi/`, `flow/`, or `tools/` imports remain inside `nagare-core`
- fixture workflows execute through `nagare run`
- representative HASHI workflows execute through the adapter path with equivalent behavior
- structured logs include correlation IDs across CLI, engine, handlers, adapters, API, and GUI
- immutable runtime snapshots exist and are consumable by downstream clients
- the visual editor imports real workflow YAML, not just demo files
- unknown fields survive import/export unless explicitly removed
- GUI-owned layout metadata stays inside `x-nagare-viz`
- exported YAML remains executable on fixture workflows with equivalent semantics
- raw YAML mode exists and is usable for recovery and advanced editing
- validation catches cycles, missing dependencies, and duplicate IDs before export
- fidelity degradation cases produce explicit warnings
- live run overlays, when added, are driven by immutable snapshots rather than the mutable draft
- tests pass across engine, contracts, adapters, and editor codec/build

---

## 10. Known Risks and Non-Goals

### High-Risk Areas

- protocol design that is too shallow to preserve real worker behavior
- YAML round-trip behavior, especially comments, ordering, and unknown nested fields
- users inferring execution order from visual layout
- mismatch between active runtime snapshots and current editable graph
- API/server scope growing too early

### Non-Goals for v1.0

- replacing YAML with a database-backed workflow definition model
- making the visual editor the only editing path
- solving every future custom step schema in the initial property panel
- multi-user or cloud orchestration concerns
- mobile-responsive workflow authoring

---

## 11. Definition of Ready for Implementation

Work on a phase is ready to begin when:

- the previous phase exit gate is met
- inputs and outputs are explicit
- fixture coverage for the relevant behavior exists
- logging expectations for that phase are defined
- tests for regressions are identified before code moves start

---

## 12. Definition of Done for v1.0

Nagare v1.0 is done when:

- the engine has been extracted into a standalone reusable package
- HASHI can consume it through adapters
- the editor can safely import, edit, and export supported workflows
- unsupported YAML survives without silent damage
- debugging information is sufficient to trace failures across layers
- the remaining limitations are documented rather than hidden

