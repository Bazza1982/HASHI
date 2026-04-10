# Nagare Workflow Creation Guide

**Version:** 1.0.0
**Last updated:** 2026-04-06
**Applies to:** Nagare engine (hashi/nagare/), nagare-core package, HASHI v3.0-alpha

| Version | Date | Changes |
|---------|------|---------|
| 1.0.0 | 2026-04-06 | Initial version. Covers full YAML spec, all backends, callables design, variable substitution, artifacts, error handling, quality gates, evaluation loop. |

> **Complete reference for writing Nagare workflow YAML files — for humans and AI agents.**
>
> This guide covers every field, every backend option, the callables system, variable injection,
> artifact flow, error handling, quality gates, and how everything connects at runtime.

---

## Table of Contents

1. [What is a Nagare Workflow?](#1-what-is-a-nagare-workflow)
2. [Complete YAML Structure Overview](#2-complete-yaml-structure-overview)
3. [Top-level: `workflow` block](#3-top-level-workflow-block)
4. [Pre-flight: Collecting Human Input](#4-pre-flight-collecting-human-input)
5. [Agents: Orchestrator and Workers](#5-agents-orchestrator-and-workers)
6. [LLM Configuration: Backends and Models](#6-llm-configuration-backends-and-models)
7. [Steps: The DAG](#7-steps-the-dag)
8. [Variable Substitution](#8-variable-substitution)
9. [Artifacts: Data Between Steps](#9-artifacts-data-between-steps)
10. [Error Handling](#10-error-handling)
11. [Quality Gates](#11-quality-gates)
12. [Success Criteria](#12-success-criteria)
13. [Output Block](#13-output-block)
14. [Evaluation Block](#14-evaluation-block)
15. [Callables: In-Process Python Steps](#15-callables-in-process-python-steps)
16. [Runtime Control](#16-runtime-control)
17. [Complete Annotated Example](#17-complete-annotated-example)
18. [Design Checklist](#18-design-checklist)

---

## 1. What is a Nagare Workflow?

A Nagare workflow is a **YAML file** that declaratively describes a multi-agent task pipeline. The engine (`FlowRunner`) reads it and:

1. Runs pre-flight: asks the human the configured questions once upfront
2. Builds a DAG (Directed Acyclic Graph) from step dependencies
3. Executes steps in topological order, calling the appropriate LLM or Python callable per step
4. Passes artifacts (files) between steps via the artifact store
5. Handles failures automatically via the debug agent (up to N retries)
6. Evaluates the run and records lessons for continuous improvement

Once pre-flight completes, the workflow runs **fully automatically** — no mid-run human input.

---

## 2. Complete YAML Structure Overview

```
workflow/          ← metadata (id, name, version, description, tags)
meta/              ← authorship metadata (created_by, created_at, improvement_count)
changelog/         ← version history
pre_flight/        ← human input collection (questions, defaults, scope_notice)
agents/            ← orchestrator + worker definitions
  orchestrator/    ← flow-runner identity and human interface
  workers[]        ← each worker: id, role, agent_md, backend, model, workspace
steps[]            ← pipeline steps (id, name, agent, depends, prompt, input, output, ...)
error_handling/    ← debug agent, retry strategies
success_criteria/  ← tiered pass/fail definitions
output/            ← final deliverable configuration
evaluation/        ← metrics collection and improvement loop
```

Every block except `workflow` and `steps` is optional (but most are strongly recommended).

---

## 3. Top-level: `workflow` Block

```yaml
workflow:
  id: my-workflow-id            # Required. Unique identifier. Use kebab-case.
  name: "Human Readable Name"   # Required. Short display name.
  version: "1.0.0"              # Required. Semantic version.
  description: |                # Required. What this workflow does.
    Multi-line description of
    the workflow's purpose.
  tags: [writing, academic]     # Optional. Free-form tags for discovery.
```

**Rules:**
- `id` must be unique across all workflows in the library. It is used to name run directories and in evaluation KB keys.
- `version` follows semver. The engine uses this to track candidate promotions.
- `tags` are cosmetic — not used by the engine.

---

## 4. Pre-flight: Collecting Human Input

The pre-flight system collects all human decisions **once, before the workflow starts**. After pre-flight, no human input is needed or accepted until the workflow finishes (or fails and escalates).

### 4.1 Full Structure

```yaml
pre_flight:
  analyst_agent: writer_01      # Optional. Agent used for auto-scan analysis.
  auto_scan: false              # Optional. Default: false. If true, engine pre-analyzes
                                # the task to suggest which questions to ask.

  collect_from_human:           # List of questions to ask the user
    - key: topic                # Required. Variable name for {pre_flight.topic}
      question: "What topic?"   # Required. Displayed to the user.
      required: true            # Optional. Default: false. Blocks start if unanswered.
      type: text                # Required. See question types below.

    - key: output_format
      question: "Output format?"
      required: false
      type: choice
      choices: [markdown, pdf, docx]   # Required when type: choice
      default: markdown                # Optional. Used if user skips.

  scope_notice: |               # Optional. Displayed before questions. Use to set
    This workflow produces X.   # expectations (scope, what is/isn't included).
    For Y, use a different one.

  defaults:                     # Optional. Named defaults injected as {pre_flight.*}
    quality_threshold: "total >= 20"   # Available in step prompts like variables
    max_retries: 3
```

### 4.2 Question Types

| `type` | Description | Extra fields |
|--------|-------------|--------------|
| `text` | Free-text input | — |
| `choice` | User picks from a list | `choices: [a, b, c]`, `default: a` |

> **Note:** Only `text` and `choice` are currently implemented in the interactive CLI. File uploads and multiselect are not yet supported.

### 4.3 Pre-flight Timeout

If the user doesn't respond within **300 seconds**, the engine uses defaults for `can_assume: true` questions and continues automatically. Questions without a default that remain unanswered will fail the pre-flight step.

### 4.4 Prefill (Automation / CI)

For automated runs, pre-flight answers can be injected via JSON:

```bash
python flow/flow_trigger.py start my-workflow \
  '{"topic": "climate change", "output_format": "markdown"}'
```

The JSON keys must match the `key` fields in `collect_from_human`.

---

## 5. Agents: Orchestrator and Workers

### 5.1 Orchestrator

```yaml
agents:
  orchestrator:
    id: flow-runner             # Required. Use "flow-runner" (the engine itself).
    human_interface: akane      # Optional. Which agent receives HChat notifications.
```

The `human_interface` value is the agent ID that receives push notifications from the engine (start, completion, failures). Set to `akane` for HASHI integration.

### 5.2 Workers

Workers are the agents that execute steps. Each worker maps to an LLM backend + model + system prompt.

```yaml
agents:
  workers:
    - id: analyst_01              # Required. Unique worker ID within this workflow.
      role: "Senior Analyst"      # Required. Human-readable role description.
      agent_md: "flow/agents/analyst/AGENT.md"  # Required. Path to system prompt file.
      workspace: "flow/runs/{run_id}/workers/analyst_01"  # Optional. Working directory.
      backend: claude-cli         # Required. See Section 6.
      model: claude-opus-4-6      # Required. Model identifier.
      controllable_by: [orchestrator, human]  # Optional. Who can interrupt this worker.
```

**Key fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `id` | Yes | Must be unique. Used in `steps[].agent` to assign steps. |
| `role` | Yes | Displayed in logs and notifications. Describes what this agent does. |
| `agent_md` | Yes | Path to the AGENT.md system prompt file (relative to repo root). |
| `workspace` | No | Working directory for the agent subprocess. `{run_id}` is substituted automatically. |
| `backend` | Yes | Which execution backend to use. See Section 6. |
| `model` | Yes | Model identifier (backend-specific format). See Section 6. |
| `controllable_by` | No | Who can pause/stop this worker. Default: `[orchestrator]`. |

---

## 6. LLM Configuration: Backends and Models

This is one of the most important design decisions. Each worker is independently configurable.

### 6.1 Supported Backends

#### `claude-cli`

Invokes the Claude CLI (`claude`) as a subprocess. This is the primary backend for Claude models.

```yaml
- id: writer_01
  backend: claude-cli
  model: claude-opus-4-6
```

**How it works internally:**
```
subprocess: claude --print --model claude-opus-4-6 --system-prompt <AGENT.md contents>
```

The engine strips the `CLAUDECODE` environment variable before spawning to prevent session interference.

**Available models (Claude):**

| Model | Use when |
|-------|----------|
| `claude-opus-4-6` | Deep reasoning, architecture design, complex writing, analysis |
| `claude-sonnet-4-6` | Structured output, format checking, simple writing, notifications |
| `claude-haiku-4-5` | Fast, cheap tasks; simple classification; short summaries |

**Design rule:** Use Opus for steps where judgment quality directly affects output quality. Use Sonnet/Haiku for steps that are primarily structural (formatting, merging, notifying).

---

#### `openrouter-api`

Calls any model via OpenRouter. Required for non-Claude models (GPT, Gemini, Mistral, etc.).

```yaml
- id: evaluator_01
  backend: openrouter-api
  model: openai/gpt-4.5-preview
```

**Why use this?** Claude cannot evaluate its own outputs objectively. For critique, independent review, and evaluation steps, using a different vendor (via OpenRouter) enforces true independence. This is architecturally enforced, not a convention.

**Available models (via OpenRouter):**

| Model | Use when |
|-------|----------|
| `openai/gpt-4.5-preview` | Independent evaluation, devil's advocate critique |
| `openai/o4-mini` | Code execution, tool use, mathematical reasoning |
| `google/gemini-2.5-pro` | Long-context tasks, multimodal |
| `anthropic/claude-opus-4-6` | Claude via OpenRouter (alternative routing) |

---

#### `codex-cli`

Invokes the Codex CLI (`codex`) as a subprocess. Best for code-heavy steps with tool access.

```yaml
- id: coder_01
  backend: codex-cli
  model: o4-mini
```

**How it works:**
```
subprocess: codex exec --model o4-mini --full-auto
```

Use when the step needs to read/write files, run shell commands, or do code execution rather than just text generation.

---

#### `callable`

Executes an in-process Python function instead of calling any LLM. No subprocess, no API calls.

```yaml
- id: pdf_extractor
  backend: callable
  model: ""     # Unused — leave empty or omit.
```

See **Section 15** for the full callables design.

---

### 6.2 Per-Step Model Override

Workers define a default model, but individual steps can override it:

```yaml
steps:
  - id: step_01
    agent: analyst_01       # normally uses claude-opus-4-6
    model: claude-sonnet-4-6  # override for this step only
    ...
```

This allows one worker definition to cover a range of tasks at different cost/quality trade-offs.

---

### 6.3 Backend Extra Options

```yaml
workers:
  - id: worker_01
    backend: claude-cli
    model: claude-opus-4-6
    backend_extra:
      timeout_seconds: 300    # Override default 600s timeout for this worker
      access_scope: local_only  # Restrict file system access
```

`backend_extra` is passed through to the handler and interpreted backend-specifically. Not all backends support all options.

---

### 6.4 Multi-Model Strategy

The recommended pattern for high-quality workflows:

```yaml
workers:
  # Heavy reasoning → Opus
  - id: analyst_01
    backend: claude-cli
    model: claude-opus-4-6

  # Structured formatting → Sonnet
  - id: formatter_01
    backend: claude-cli
    model: claude-sonnet-4-6

  # Independent evaluation → different vendor (GPT)
  - id: evaluator_01
    backend: openrouter-api
    model: openai/gpt-4.5-preview

  # Debug recovery → Sonnet (fast and capable enough)
  - id: debug_01
    backend: claude-cli
    model: claude-sonnet-4-6
```

---

## 7. Steps: The DAG

Steps are the core of a workflow. They define what gets done, in what order, by whom.

### 7.1 Full Step Schema

```yaml
steps:
  - id: analyze_input           # Required. Unique step ID within this workflow.
    name: "Analyze Input Data"  # Required. Human-readable name for display/logs.
    agent: analyst_01           # Required. Must match a worker id in agents.workers.
    depends: []                 # Required (can be empty). List of step IDs that must
                                # complete before this step runs.
    model: claude-opus-4-6      # Optional. Override the worker's default model.

    input:                      # Optional. Explicit input wiring.
      from_artifacts: [outline] # Pull these artifact keys into the step's working dir.
      params:                   # Named parameter substitution (see Section 8).
        topic: "{pre_flight.topic}"
        language: "{pre_flight.language}"

    prompt: |                   # Required. The task instruction sent to the agent.
      Analyze the topic: {pre_flight.topic}
      Read the file: paragraph_outline.json
      Output analysis.json with your findings.

    output:                     # Required if the step produces artifacts.
      artifacts:
        - key: analysis         # Artifact key for downstream reference.
          path: analysis.json   # File path (relative to worker workspace).
          type: json            # Artifact type hint: json | text | file | binary

    quality_gate:               # Optional. Blocks workflow if criteria not met.
      type: auto
      criteria:
        - "analysis.status == 'ok'"

    skip_if: ""                 # Optional. Skip this step if condition is true.
                                # Uses artifact and pre_flight variable access.

    timeout_seconds: 300        # Optional. Default: 600. Override per step.
```

### 7.2 Step Dependency DAG

The `depends` field controls execution order:

```yaml
steps:
  - id: step_a
    depends: []               # No dependencies — runs first (or in parallel with others)

  - id: step_b
    depends: []               # Also runs first — parallel with step_a

  - id: step_c
    depends: [step_a]         # Runs after step_a completes

  - id: step_d
    depends: [step_a, step_b] # Runs after BOTH step_a AND step_b complete

  - id: step_e
    depends: [step_c, step_d] # Runs last — after the full preceding chain
```

The engine topologically sorts steps and runs independent groups in parallel automatically.

**Rules:**
- No cycles. If A depends on B and B depends on A, the engine will reject the workflow.
- An empty `depends: []` means the step starts as soon as pre-flight completes.
- All dependency IDs must reference valid step `id` values in the same workflow.

### 7.3 Conditional Skip

```yaml
- id: citation_check
  depends: [draft_writing]
  skip_if: "pre_flight.citation_style == 'none'"
  ...
```

If the `skip_if` condition evaluates to `true` at runtime, the step is marked `skipped` and downstream steps that depend on it can still run (they receive no artifact from the skipped step).

Currently supported `skip_if` expressions:
- `pre_flight.<key> == '<value>'`
- `artifacts.<key> == null` (artifact was not produced)
- String equality comparisons

---

## 8. Variable Substitution

Variables in prompts and `input.params` are substituted at runtime by the engine.

### 8.1 Pre-flight Variables

Access answers to pre-flight questions:

```
{pre_flight.key}
```

Example:
```yaml
pre_flight:
  collect_from_human:
    - key: topic
      question: "Topic?"
      type: text

steps:
  - id: step_01
    prompt: |
      Write about: {pre_flight.topic}
```

### 8.2 Pre-flight Defaults

Pre-flight `defaults` are also accessible as variables:

```yaml
pre_flight:
  defaults:
    quality_threshold: "total >= 20"

steps:
  - id: step_final
    prompt: |
      Pass criteria: {pre_flight.quality_threshold}
```

### 8.3 Artifact Variables

Reference the content or path of a produced artifact:

```
{artifacts.key}
```

The engine resolves this to the absolute file path of the artifact. The agent can then read the file.

Example:
```yaml
steps:
  - id: step_01
    output:
      artifacts:
        - key: outline
          path: outline.json
          type: json

  - id: step_02
    depends: [step_01]
    prompt: |
      Read outline.json (available as {artifacts.outline}).
      Write the full draft based on the outline.
```

### 8.4 Built-in Variables

| Variable | Value |
|----------|-------|
| `{run_id}` | Current run identifier (e.g. `run-my-workflow-2026-04-06-143022`) |
| `{workflow_id}` | The workflow's `id` field |

These are always available without declaration.

### 8.5 Substitution in `input.params`

`input.params` creates named variables that are passed to the agent alongside the prompt:

```yaml
input:
  params:
    language: "{pre_flight.output_language}"
    source: "{pre_flight.source_materials}"
```

The agent receives these as key-value pairs in its task message payload. They do NOT automatically appear in the prompt — you must reference them in the `prompt` text as `{pre_flight.output_language}` directly.

---

## 9. Artifacts: Data Between Steps

Artifacts are the mechanism for passing data between steps. Every piece of output that a downstream step needs must be declared as an artifact.

### 9.1 Producing Artifacts

```yaml
steps:
  - id: step_01
    ...
    output:
      artifacts:
        - key: analysis_report    # Artifact key (referenced downstream as {artifacts.analysis_report})
          path: analysis.json     # Relative path within the worker's workspace
          type: json              # Type hint: json | text | file | binary
```

The engine registers this artifact in the ArtifactStore after the step completes. Absolute path is stored for downstream access.

### 9.2 Consuming Artifacts

```yaml
steps:
  - id: step_02
    depends: [step_01]
    input:
      from_artifacts: [analysis_report]   # Pull artifact into this step's working dir
    prompt: |
      Read analysis.json and use it to write the report.
      ({artifacts.analysis_report} is the file path)
```

When `from_artifacts` is declared, the engine makes that artifact available to the worker before the step runs.

### 9.3 Artifact Types

| `type` | Description |
|--------|-------------|
| `json` | JSON file — engine can parse it for quality gate evaluation |
| `text` | Plain text file |
| `file` | Any file type (binary or text) — engine treats as opaque |
| `binary` | Binary data (images, PDFs, etc.) |

### 9.4 Artifact Key Rules

- Keys must be unique within a workflow (collision = last writer wins)
- Use `snake_case` for keys
- Parallel steps must use **different artifact keys** — two parallel steps writing to the same key will have undefined behavior

### 9.5 Artifact Versioning

If a step is re-run (e.g., after a failure and retry), the engine creates a new version of the artifact without deleting the old one. The artifact store maintains a history. Downstream steps always receive the latest version.

---

## 10. Error Handling

### 10.1 Basic Configuration

```yaml
error_handling:
  debug_agent: debug_01         # Worker ID of the debug agent.
  max_attempts: 3               # Max retry attempts per step (default: 1).
  retry_strategy: prompt_adjustment  # Simple mode: engine adjusts prompt on retry.
```

### 10.2 Per-Attempt Retry Strategies

For fine-grained control over each retry:

```yaml
error_handling:
  debug_agent: debug_01
  max_attempts: 3
  retry_strategy:
    attempt_1: >
      Analyze error via debug_01. If prompt interpretation issue,
      clarify ambiguous instructions and retry.
    attempt_2: >
      If step timed out, simplify prompt (remove optional requirements).
      Reduce target length. Switch to fallback mode.
    attempt_3: >
      Minimal viable attempt: relaxed quality threshold, notify user of
      degraded output.
  on_max_exceeded:
    action: notify_human_interface
    target: akane
    message: "Workflow failed after 3 attempts. Step: {failed_step_id}, Error: {error}"
```

### 10.3 Failure Flow

```
Step fails
    ↓
debug_01 agent analyzes the failure
    ↓
Engine applies retry_strategy for this attempt
    ↓
Step retried (attempt 2 of 3)
    ↓
If still failing after max_attempts:
    ↓
on_max_exceeded.action = notify_human_interface
    → HChat message sent to the human interface agent
    → Workflow status set to FAILED
```

### 10.4 What Counts as a Failure

- Step subprocess exits with non-zero status
- Step times out (exceeds `timeout_seconds`)
- Step output doesn't parse as valid JSON when JSON is expected
- Step output doesn't include `"status": "completed"`
- Quality gate criteria not met (see Section 11)

---

## 11. Quality Gates

Quality gates block a step from being considered "completed" until specific criteria are met.

### 11.1 Configuration

```yaml
steps:
  - id: final_output
    ...
    quality_gate:
      type: auto
      criteria:
        - "final_package.quality_passed == true"
        - "final_package.quality_score >= 0.7"
```

If criteria are not met, the step is treated as failed and enters the error-handling retry loop.

### 11.2 Criteria Syntax

Criteria are evaluated against the step's produced JSON artifacts:

| Pattern | Example |
|---------|---------|
| `artifact_key.field == value` | `final_package.status == 'ok'` |
| `artifact_key.field >= number` | `final_package.score >= 0.7` |
| `artifact_key.field == true/false` | `result.passed == true` |

**Note:** Quality gates only work when the artifact `type` is `json` — the engine parses the JSON to evaluate field comparisons.

### 11.3 Non-Blocking Review Pattern

For advisory review steps (where output always flows forward regardless of score):

```yaml
# NOTE: No quality_gate here — review is advisory, not blocking.
# The reviewer still produces its output, downstream steps use it.
- id: academic_review
  agent: reviewer_01
  ...
  # No quality_gate block
```

Add the quality gate only on the **final step** where the combined output is evaluated.

---

## 12. Success Criteria

Success criteria define what "done" means for the whole workflow. Unlike quality gates (which block individual steps), success criteria are evaluated at the end of the run.

### 12.1 Simple Form

```yaml
success_criteria:
  - "step_final status is completed"
  - "final_package.quality_passed == true"
```

### 12.2 Tiered Form (Recommended)

```yaml
success_criteria:
  full_success:
    - "Step final_output status is completed"
    - "final_package.quality_score >= 0.8"
    - "output_file.txt exists"
  acceptable:
    - "Step final_output status is completed"
    - "final_package.quality_score >= 0.6"
  degraded:
    - "Step final_output status is completed"
    - "final_package.quality_score >= 0.4"
    - "User notified of quality gaps"
```

The engine reports which tier was achieved. The `full_success` tier drives the evaluation score.

---

## 13. Output Block

Configures the final deliverable and post-completion notification.

```yaml
output:
  type: file                          # "file" or "message"
  source_artifact: final_paragraph    # Artifact key of the final output
  destination: "artifacts/output/final_paragraph.txt"  # Where to copy it
  notify_agent: akane                 # Send completion notification to this agent
  message_template: |
    Workflow complete!
    Quality score: {final_package.quality_score}
    Word count: {final_package.word_count}
    Output: {final_paragraph}
```

`message_template` supports artifact variable substitution. The notification is sent via HChat to the `notify_agent`.

---

## 14. Evaluation Block

Controls whether this workflow participates in the continuous improvement loop.

```yaml
evaluation:
  enabled: true               # Default: true. Set false to disable for test workflows.
  metrics:
    - total_duration          # How long the run took (seconds)
    - design_quality_score    # Quality of the output (from quality gate scores)
    - model_cost_estimate     # Estimated token cost
    - error_retries           # Number of debug retries needed
    - human_interventions     # How many times a human was needed
  improvement_threshold: 5   # Minimum runs before KB generates improvement suggestions
  auto_apply: false           # If true, Class A improvements are auto-applied
```

Evaluation data is stored in `flow/evaluation_kb/`. After `improvement_threshold` runs, the evaluator generates improvement proposals classified as:

| Class | Risk | Auto-applied? |
|-------|------|---------------|
| **A** | Low | Yes (if `auto_apply: true`) |
| **B** | Medium | No — requires human approval |
| **C** | High | No — requires human approval |

---

## 15. Callables: In-Process Python Steps

The callables system allows workflow steps to execute Python functions directly — no LLM, no subprocess, no API call. This is used for deterministic processing tasks: PDF extraction, database queries, format conversion, calling internal APIs, etc.

### 15.1 Why Callables?

Not every step needs an LLM. For example:
- Extract text from a PDF file → Python function, deterministic, fast
- Query a database for recent records → Python function
- Call an internal API → Python function
- Resize an image → Python function

Mixing LLM steps and callable steps in the same workflow is fully supported.

### 15.2 Declaring a Callable Worker

```yaml
agents:
  workers:
    - id: pdf_extractor
      role: "PDF Text Extractor"
      agent_md: ""              # Unused for callables — leave empty or omit.
      backend: callable         # This is what makes it a callable.
      model: ""                 # Unused — leave empty.
      workspace: "flow/runs/{run_id}/workers/pdf_extractor"
```

### 15.3 Declaring a Callable Step

```yaml
steps:
  - id: extract_pdf
    name: "Extract PDF Text"
    agent: pdf_extractor        # Must match a worker with backend: callable
    depends: []
    prompt: |
      Extract all text from the PDF at {pre_flight.input_file}.
      Save the extracted text to extracted_text.txt.
    output:
      artifacts:
        - key: extracted_text
          path: extracted_text.txt
          type: text
    timeout_seconds: 120
```

### 15.4 Registering the Callable in Python

Before starting the workflow, register the Python function with the handler:

```python
from nagare.handlers import RoutingStepHandler, SubprocessStepHandler
from nagare.engine.runner import FlowRunner

def extract_pdf(task_message: dict) -> dict:
    """
    Receives the full task_message dict.
    Must return {"status": "completed", ...} or {"status": "failed", ...}
    """
    payload = task_message["payload"]
    params = payload.get("params", {})
    input_file = params.get("input_file", "")
    workspace = task_message.get("worker_workspace", "/tmp")

    # Do the actual work
    import pdfminer.high_level
    text = pdfminer.high_level.extract_text(input_file)

    output_path = f"{workspace}/extracted_text.txt"
    with open(output_path, "w") as f:
        f.write(text)

    return {
        "status": "completed",
        "artifacts_produced": {"extracted_text": output_path},
        "summary": f"Extracted {len(text)} characters from {input_file}"
    }

# Wire everything together
run_id = "run-my-workflow-..."
subprocess_handler = SubprocessStepHandler(run_id=run_id, ...)
router = RoutingStepHandler(
    run_id=run_id,
    fallback_handler=subprocess_handler,
)
router.register_callable("pdf_extractor", extract_pdf)

runner = FlowRunner("path/to/workflow.yaml", run_id=run_id, step_handler=router)
runner.start()
```

### 15.5 Callable Function Contract

Every callable must:

```python
def my_function(task_message: dict) -> dict:
    ...
```

**Input — `task_message` structure:**

```python
{
    "run_id": "run-my-workflow-...",
    "workflow_id": "my-workflow",
    "task_id": "task-pdf_extractor-1712345678",
    "worker_workspace": "/abs/path/to/flow/runs/{run_id}/workers/pdf_extractor",
    "payload": {
        "step_id": "extract_pdf",
        "prompt": "Extract all text from ...",
        "params": {
            "input_file": "/path/to/file.pdf",
            ...
        },
        "input_artifacts": {
            "previous_artifact_key": "/abs/path/to/file"
        },
        "output_spec": [
            {"key": "extracted_text", "path": "extracted_text.txt", "type": "text"}
        ]
    }
}
```

**Output — success:**

```python
{
    "status": "completed",
    "artifacts_produced": {
        "extracted_text": "/abs/path/to/extracted_text.txt"   # absolute paths
    },
    "summary": "Brief human-readable result"
}
```

**Output — failure:**

```python
{
    "status": "failed",
    "error": "Human-readable error message",
    "error_type": "optional_error_type_tag"
}
```

### 15.6 Callable Auto-Setup (Advanced)

When a workflow step targets `backend: callable` but no function is registered at runtime, the **CallableSetupManager** can automatically request an AI agent to implement it:

```python
from nagare.engine.callable_setup_manager import CallableSetupManager

setup_manager = CallableSetupManager(
    run_id=run_id,
    notifier=hchat_notifier,    # How to contact the AI agent
    ai_agent_id="akane",        # Which AI agent to ask
    api_base_url="http://127.0.0.1:8787",  # Where the AI should POST the code
)

callable_handler = CallableStepHandler(
    run_id=run_id,
    setup_manager=setup_manager,
)
```

**Auto-setup flow:**

```
Step executes → no callable registered for agent_id
    ↓
CallableSetupManager.request_setup()
    → Sends HChat message to akane:
      "Step X needs a callable. Please implement run(task_message) → dict
       and POST it to /runs/{run_id}/callables/{agent_id}"
    ↓
Runner thread blocks (up to 300 seconds per attempt)
    ↓
AI agent POSTs code to the API endpoint
    → Engine exec()s the code
    → Validates that `run` function is defined
    → Persists to flow/callables/{agent_id}.py (auto-loaded on future runs)
    ↓
Gate event fires → runner thread wakes and executes the callable
    ↓
If callable fails → retry with refined request (up to MAX_RETRIES = 3)
    ↓
If all retries fail → escalate to human
```

**Persisted callables** in `flow/callables/{agent_id}.py` are auto-loaded on subsequent runs — the AI only needs to implement each callable once.

---

## 16. Runtime Control

### 16.1 Signal Files

The engine checks for signal files between steps (not during a step):

```bash
# Pause after current step completes
touch flow/runs/<run_id>/_pause

# Resume from pause
rm flow/runs/<run_id>/_pause

# Stop cleanly (after current step)
touch flow/runs/<run_id>/_stop
```

### 16.2 State Machine

```
CREATED
    │
PRE_FLIGHT     ← human Q&A happens here
    │
CONFIRMED      ← all inputs locked
    │
RUNNING        ← steps execute
    ├── step fails → DEBUG → retry (up to max_attempts)
    │                      → FAILED (if max exceeded)
    ├── _pause signal → PAUSED → (delete _pause) → RUNNING
    └── _stop signal → ABORTED
    │
COMPLETED / FAILED / ABORTED
```

### 16.3 Run Directory Structure

```
flow/runs/<run_id>/
├── state.json               ← Current workflow state (all steps, statuses)
├── artifacts/               ← All produced artifacts
│   ├── analysis.json
│   └── final_output.txt
├── workers/                 ← Per-agent working directories
│   ├── analyst_01/
│   │   ├── inbox/           ← Task messages delivered to the agent
│   │   └── outbox/          ← Results from the agent
│   └── writer_01/
├── callable_setup.json      ← Callable auto-setup retry tracking
├── evaluation_events.jsonl  ← Full event stream for the evaluator
├── _pause                   ← Create to pause (delete to resume)
└── _stop                    ← Create to stop cleanly
```

### 16.4 Starting and Monitoring Workflows

```bash
# Start a workflow
python flow/flow_trigger.py start <workflow_id> '<json_prefill>'

# Check status
python flow/flow_trigger.py status <run_id>

# List recent runs
python flow/flow_trigger.py list
```

---

## 17. Complete Annotated Example

A minimal but complete 3-step workflow that demonstrates all key concepts:

```yaml
# ================================================================
# workflow: metadata
# ================================================================
workflow:
  id: translate-article
  name: "Article Translation"
  version: "1.0.0"
  description: |
    Translates an article from one language to another through a
    3-step pipeline: pre-process → translate → quality review.
  tags: [translation, writing]

meta:
  created_by: akane
  created_at: "2026-04-06T00:00:00Z"

# ================================================================
# pre_flight: ask the user before starting
# ================================================================
pre_flight:
  collect_from_human:
    - key: source_file
      question: "Path to the article file to translate?"
      required: true
      type: text

    - key: target_language
      question: "Target language?"
      required: true
      type: choice
      choices: [Chinese, English, Japanese, French]

    - key: style
      question: "Translation style?"
      required: false
      type: choice
      choices: [formal, natural, literary]
      default: natural

  scope_notice: |
    This workflow translates one article file.
    For batch translation of multiple files, run multiple times.

# ================================================================
# agents: define workers
# ================================================================
agents:
  orchestrator:
    id: flow-runner
    human_interface: akane

  workers:
    - id: translator_01
      role: "Expert Translator"
      agent_md: "flow/agents/analyst/AGENT.md"
      workspace: "flow/runs/{run_id}/workers/translator_01"
      backend: claude-cli
      model: claude-opus-4-6

    - id: reviewer_01
      role: "Translation Reviewer"
      agent_md: "flow/agents/analyst/AGENT.md"
      workspace: "flow/runs/{run_id}/workers/reviewer_01"
      backend: openrouter-api          # Independent reviewer = different vendor
      model: openai/gpt-4.5-preview

    - id: debug_01
      role: "Debug Agent"
      agent_md: "flow/agents/analyst/AGENT.md"
      workspace: "flow/runs/{run_id}/workers/debug_01"
      backend: claude-cli
      model: claude-sonnet-4-6

# ================================================================
# steps: the pipeline
# ================================================================
steps:
  # Step 1: Pre-process the source article
  - id: preprocess
    name: "Pre-process Source Article"
    agent: translator_01
    depends: []                        # Runs first — no dependencies
    prompt: |
      Read the source article at: {pre_flight.source_file}
      Target language: {pre_flight.target_language}
      Style: {pre_flight.style}

      Analyze the article:
      1. Count the words
      2. Identify the domain/topic
      3. Note any specialized terminology

      Output article_analysis.json:
      {
        "word_count": 0,
        "domain": "...",
        "terminology": ["term1", "term2"],
        "article_text": "full article text here",
        "notes": "any special considerations"
      }
    output:
      artifacts:
        - key: article_analysis
          path: article_analysis.json
          type: json
    timeout_seconds: 120

  # Step 2: Translate
  - id: translate
    name: "Translate Article"
    agent: translator_01
    depends: [preprocess]              # Runs after preprocess
    input:
      from_artifacts: [article_analysis]  # Makes article_analysis.json available
    prompt: |
      Read article_analysis.json to get the source text and context.
      Target language: {pre_flight.target_language}
      Style: {pre_flight.style}

      Translate the article. Follow these rules:
      - Preserve all formatting (headers, paragraphs, lists)
      - Use consistent terminology (check the terminology list)
      - Match the requested style ({pre_flight.style})
      - Do NOT translate proper nouns (names, places, brands)

      Output TWO files:
      1. translation.txt  — the full translated text
      2. translation_notes.json:
         {
           "word_count_source": 0,
           "word_count_translation": 0,
           "terminology_decisions": {"original": "translated"},
           "confidence": 0.0    # 0.0 to 1.0
         }
    output:
      artifacts:
        - key: translation
          path: translation.txt
          type: text
        - key: translation_notes
          path: translation_notes.json
          type: json
    timeout_seconds: 600

  # Step 3: Quality review (independent reviewer — different vendor)
  - id: review
    name: "Quality Review"
    agent: reviewer_01               # GPT — independent perspective
    depends: [translate]
    input:
      from_artifacts: [article_analysis, translation, translation_notes]
    prompt: |
      Review the translation quality.

      Files to read:
      - article_analysis.json (source text and context)
      - translation.txt (the translation)
      - translation_notes.json (translator's notes)

      Target language: {pre_flight.target_language}
      Style requested: {pre_flight.style}

      Score on 5 dimensions (1-5 each):
      1. Accuracy — does it faithfully represent the source?
      2. Fluency — does it read naturally in the target language?
      3. Style — does it match the requested style?
      4. Terminology — are specialized terms handled correctly?
      5. Formatting — is the structure preserved?

      Output review_result.json:
      {
        "scores": {
          "accuracy": 0, "fluency": 0, "style": 0,
          "terminology": 0, "formatting": 0, "total": 0
        },
        "passed": false,
        "issues": [{"dimension": "...", "issue": "...", "suggestion": "..."}],
        "final_translation": "corrected translation text (apply all fixes)",
        "summary": "brief overall assessment"
      }

      Pass criteria: total >= 20 AND no dimension below 3.
    output:
      artifacts:
        - key: review_result
          path: review_result.json
          type: json
    quality_gate:                     # Block if quality is insufficient
      type: auto
      criteria:
        - "review_result.passed == true"
    timeout_seconds: 300

# ================================================================
# error_handling
# ================================================================
error_handling:
  debug_agent: debug_01
  max_attempts: 3
  retry_strategy:
    attempt_1: "Analyze failure. If quality gate failed, pass reviewer feedback to translator and retry translation step."
    attempt_2: "Simplify: reduce style requirements, focus on accuracy only."
    attempt_3: "Minimum viable: accuracy only, no style matching. Notify user."
  on_max_exceeded:
    action: notify_human_interface
    target: akane
    message: "Translation workflow failed after 3 attempts. Step: {failed_step_id}. Error: {error}"

# ================================================================
# success_criteria
# ================================================================
success_criteria:
  full_success:
    - "Step review status is completed"
    - "review_result.passed == true"
    - "review_result.scores.total >= 23"
  acceptable:
    - "Step review status is completed"
    - "review_result.passed == true"
  degraded:
    - "Step review status is completed"
    - "review_result.scores.total >= 15"
    - "User notified of quality limitations"

# ================================================================
# output
# ================================================================
output:
  type: file
  source_artifact: review_result
  destination: "artifacts/output/final_translation.txt"
  notify_agent: akane
  message_template: |
    Translation complete!
    Quality: {review_result.scores.total}/25
    Passed: {review_result.passed}
    Summary: {review_result.summary}

# ================================================================
# evaluation
# ================================================================
evaluation:
  enabled: true
  metrics:
    - total_duration
    - design_quality_score
    - error_retries
    - human_interventions
  improvement_threshold: 5
  auto_apply: false
```

---

## 18. Design Checklist

Before saving a new workflow, verify:

**Structure**
- [ ] `workflow.id` is unique (kebab-case)
- [ ] All step `id` values are unique within the workflow
- [ ] All `depends` references point to valid step IDs
- [ ] No circular dependencies in the DAG

**Workers**
- [ ] Every `agent` in steps matches a worker `id`
- [ ] Complex reasoning steps use Opus (not Sonnet/Haiku)
- [ ] Evaluation / critique steps use a different vendor than the primary workers
- [ ] A `debug_01` worker is defined and referenced in `error_handling`

**Pre-flight**
- [ ] All questions that are truly required are marked `required: true`
- [ ] Choice questions have sensible `default` values
- [ ] All pre-flight keys used in prompts are declared in `collect_from_human`

**Artifacts**
- [ ] Every artifact consumed via `from_artifacts` or `{artifacts.key}` is produced by a preceding step
- [ ] Parallel steps use unique artifact keys (no collisions)
- [ ] The final step produces an artifact referenced in `output.source_artifact`

**Error Handling**
- [ ] `error_handling.debug_agent` references a valid worker ID
- [ ] `max_attempts` is 2-3 for production workflows
- [ ] `on_max_exceeded` sends a notification so the failure doesn't go unnoticed

**Quality Gates**
- [ ] Quality gates use `type: json` artifacts only
- [ ] Gate criteria expressions match actual JSON field names in the artifact
- [ ] Advisory steps (non-blocking reviews) do NOT have quality gates

**Callables (if used)**
- [ ] Callable workers declare `backend: callable`
- [ ] The Python function is registered before `runner.start()` is called
- [ ] The function returns `{"status": "completed"|"failed", ...}`
- [ ] Artifact paths in the return value are absolute

---

*This guide reflects the Nagare engine as of v3.0-alpha. For the underlying architecture and design philosophy, see `NAGARE_FLOW_SYSTEM.md`. For the round-trip YAML contract and compatibility classes, see `ROUND_TRIP_CONTRACT.md`. For implementing custom step handlers, see `HANDLER_GUIDE.md`.*
