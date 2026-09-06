# Nagare Flow System — Technical Reference

Nagare is HASHI's YAML-driven workflow engine. It executes a directed acyclic graph (DAG) of
specialist workers, persists run evidence, moves declared artifacts between steps, and exposes a
small host-adapter boundary for notifications and evaluation.

This document describes the behavior implemented in the current source tree. The more detailed
authoring reference is [NAGARE_WORKFLOW_GUIDE.md](NAGARE_WORKFLOW_GUIDE.md); current limitations
are summarized in [KNOWN_LIMITATIONS_NAGARE.md](KNOWN_LIMITATIONS_NAGARE.md).

## 1. Scope and Layers

- **Nagare** owns one bounded workflow: its inputs, steps, artifacts, and terminal result.
- **Shimanto** groups related Nagare workflows into a larger deliverable stream.
- **Minato** is the human-owned project/decision layer.

These labels describe ownership. Only Nagare's runner is an execution engine in this package;
Shimanto and Minato are organized through HASHI's flow registry and project metadata.

## 2. Runtime Components

| Component | Implemented responsibility |
|---|---|
| `nagare.engine.runner.FlowRunner` | Loads YAML, executes ready DAG steps, handles signals and recovery |
| `nagare.handlers.SubprocessStepHandler` | Runs `claude-cli` or `codex-cli` workers |
| `nagare.handlers.CallableStepHandler` | Runs explicitly registered in-process Python callables |
| `nagare.engine.preflight.PreFlightCollector` | Collects `text` and `choice` inputs or applies silent defaults |
| `nagare.engine.ArtifactStore` | Copies verified single- or multi-file outputs into the run directory |
| `nagare.engine.TaskState` | Persists workflow and step state atomically |
| `nagare.logging.RunEventLogger` | Writes canonical events plus evaluator-compatible legacy events |
| HASHI adapter layer | Supplies HChat notification, provider routing, and post-run evaluation |

The compatibility imports under `flow/engine/` delegate to the extracted `nagare/` core.

## 3. Execution Lifecycle

```text
CREATED
   ↓
pre-flight validation and optional CLI confirmation
   ↓
RUNNING ── step succeeds ──→ next ready step
   │
   ├── correctable task failure → DEBUG contract → revised retry
   │                                  ↖───────────────┘
   ├── explicit wait_for_human → PAUSED → response + resume
   ├── unrecoverable/infrastructure failure → FAILED
   └── authorized _stop signal → ABORTED
   ↓
COMPLETED
```

Recovery has no fixed attempt or wall-clock ceiling. Attempt count alone has no authority to end
a task. The runner sends the failure evidence and prior recovery history to the Debug Agent,
requires diagnosis/evidence/fix fields, and rejects an exact repeated recovery action. Semantic
equivalence is not mechanically provable, so the Debug Agent must return an explicit
`unrecoverable` result when the path cannot safely continue.

Sequential and `strategy: parallel` task failures use the same Debug recovery semantics. Parallel
steps finish their current dispatch group before recovery is processed.

## 4. Workflow Contract

A current workflow has `workflow`, `agents`, and `steps`. Common optional blocks are
`pre_flight`, `error_handling`, `success_criteria`, `output`, and `evaluation`.

```yaml
workflow:
  id: example
  name: Example
  version: 1.0.0

agents:
  orchestrator:
    id: flow-runner
    human_interface: akane
  workers:
    - id: writer
      role: Draft writer
      agent_md: flow/agents/example/AGENT.md
      backend: claude-cli
      model: configured-model-id
    - id: debug
      role: Recovery specialist
      agent_md: flow/agents/debug/AGENT.md
      backend: claude-cli

steps:
  - id: draft
    name: Draft
    agent: writer
    depends: []
    prompt: Write the requested draft.
    output:
      artifacts:
        - key: draft_report
          path: draft_report.json
          type: json

error_handling:
  debug_agent: debug
  on_unrecoverable:
    action: notify_human_interface
    message: "Cannot recover {failed_step_id}: {error}"
```

Every executable workflow has at least one named step and declares worker roles. Published
workflows are contract-tested for a valid graph, supported backends, resolvable `agent_md` files,
and absence of retired timeout/retry fields.

### Supported backends

- `claude-cli`: invokes the installed Claude CLI.
- `codex-cli`: invokes the installed Codex CLI.
- `callable`: invokes a Python function registered through `RoutingStepHandler`.

Unknown subprocess backend names fail closed. They are not silently routed through Claude.
Declared model IDs are passed to the selected CLI; when omitted, that CLI's configured default is
used. Availability is owned by the CLI and account.

## 5. Output and Artifact Integrity

A worker must end with a structured JSON result whose status is one of `completed`, `failed`,
`recovered`, or `unrecoverable`. Unstructured prose is not completion evidence.

For a completed step, the runner verifies that:

- `artifacts_produced` is a mapping;
- every declared artifact is present unless it has `required: false`;
- every reported path exists;
- each verified artifact can be copied into the run's artifact store.

Subprocess workers report paths relative to their run-scoped worker workspace; absolute paths and
`..` escapes are rejected. Trusted host-registered callables may return absolute paths because the
host already controls both their code and filesystem authority.

Missing or fabricated output becomes an `artifact_contract` failure and follows normal Debug
recovery. A list of files is stored as a directory artifact under its declared key.

## 6. Automatic Quality Gates

`quality_gate.type: auto` evaluates dotted fields in produced JSON artifacts. Supported operators
are `==`, `!=`, `>=`, `<=`, `>`, `<`, and `in`.

```yaml
quality_gate:
  type: auto
  criteria:
    - validation_report.valid == true
    - validation_report.grade in ['A', 'B']
```

Unknown paths, malformed JSON, unsupported expressions, and false comparisons all fail closed as
`quality_gate_fail`. Arbitrary Python expressions and function calls are not evaluated.

`success_criteria` remains descriptive metadata for authors and host integrations; the core does
not attempt to interpret free-form or tiered prose. Any condition that must block execution must
be expressed as a step quality gate and/or a required artifact.

The `output` block is also host-facing metadata. The core does not copy its `destination` or render
its message template; the producing worker must create the artifact, and a host may deliver it.

## 7. Human Input and Runtime Control

Pre-flight supports:

- interactive `text` and `choice` questions;
- JSON prefill;
- silent mode using declared defaults.

A step with `wait_for_human: true` may emit `clarification_questions`. The runner writes a query,
creates `_pause`, records an intervention event, and waits without an implicit timeout. After the
response is written and the pause signal is removed, answers are merged into pre-flight context.
`{pre_flight}` passes the complete context as a structured mapping to a later step;
`{pre_flight.key}` substitutes one value.

Control files live under the run directory:

```text
_pause   pause between steps; delete it to resume the still-running process
_stop    terminate the workflow path
```

Pause is checked between steps, not inside a running worker. Stop is checked between steps, during
subprocess dispatch, and while callable auto-setup is waiting. A currently executing in-process
callable cannot be pre-empted safely by Python and must return before the runner observes stop.

`nagare resume` only clears `_pause` for a live workflow. It does not restart a dead process or
reconstruct an interrupted worker from `state.json`.

## 8. Events and Evaluation

Each run writes:

```text
flow/runs/<run_id>/
├── state.json
├── events.jsonl
├── evaluation_events.jsonl
├── artifacts/
├── workers/
└── logs/
```

When `evaluation.enabled` is not false, the configured host Evaluator runs after the terminal run
event. The bundled HASHI evaluator measures only evidence available in the event stream:

- total and per-step duration when timestamps exist;
- unique completed and failed steps;
- Debug invocations, escalations, and human interventions;
- rule-based stability and intervention scores.

Efficiency and quality remain `null` without an appropriate baseline or downstream evidence.
`overall` averages only measured dimensions and is accompanied by coverage metadata.

The passive Evaluator writes reports and recommendations; it does not edit canonical workflows.
An explicitly authorized authoring workflow may create a sibling `_candidate.yaml`. The next run
trials that exact file, promotes it on success, or removes it on failure while preserving the
canonical workflow.

## 9. Local API and Editor

Start the API with:

```bash
nagare api --host 127.0.0.1 --port 8787
```

The server exposes inspection endpoints for snapshots, events, and artifact metadata. It also has
local control endpoints used by Nagare Viz to submit a run and by callable auto-setup to deliver
code.

Because callable delivery executes and persists Python, the API refuses non-loopback bind
addresses. Browser CORS access is limited to the fixed loopback Nagare Viz origin on port 5380.
Treat the API and `flow/callables/` as a trusted-local boundary; never proxy them to an untrusted
network.

Callable code is compiled and executed in the Nagare process. It is not sandboxed. Prefer
pre-registered, reviewed callables; enable AI-delivered callable setup only in an explicitly
trusted local environment.

## 10. Known Boundaries

- `skip_if` supports only the small equality/empty-condition grammar implemented by the runner.
- Pause/resume is live-process signaling, not crash recovery.
- In-process callable execution is not pre-emptible.
- The bundled API does not expose pause/stop endpoints; control remains file-based.
- The standalone deterministic handler is for packaging/CI smoke tests, not model-quality proof.
- Model/provider credentials, routing, and notification delivery are host responsibilities.
- YAML comments and unknown fields require the preservation-aware codec/editor path documented in
  [ROUND_TRIP_CONTRACT.md](ROUND_TRIP_CONTRACT.md).

## 11. Quick Reference

```bash
# Validate packaging behavior without a model call
nagare run tests/fixtures/smoke_test.yaml --silent --yes --smoke-handler

# Inspect runs
nagare list
nagare status <run_id>

# Clear a live pause signal
nagare resume <run_id>

# Start the loopback API
nagare api --host 127.0.0.1 --port 8787
```

Built-in workflow locations:

- `flow/workflows/examples/smoke_test.yaml`
- `flow/workflows/examples/meta_workflow_creation.yaml`
- `flow/workflows/library/book_translation.yaml`
