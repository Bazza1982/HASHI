# Nagare Flow System — Complete Technical Reference

> **Nagare (流れ)**: The HASHI multi-agent workflow orchestration engine. Designed to accomplish what no single prompt — or hundred prompts — can: coordinated, multi-perspective, self-improving work at scale.

---

## Table of Contents

1. [Why Nagare Exists — The Core Problem](#1-why-nagare-exists)
2. [Architecture Overview](#2-architecture-overview)
3. [Why Single-Prompt / Chain-of-Thought Cannot Compete](#3-why-single-prompt-cannot-compete)
4. [System Components](#4-system-components)
5. [Human-in-the-Loop: The Pre-Flight System](#5-human-in-the-loop-the-pre-flight-system)
6. [Multi-Model Strategy](#6-multi-model-strategy)
7. [How to Use: The Meta-Workflow](#7-how-to-use-the-meta-workflow)
8. [Workflow YAML Schema](#8-workflow-yaml-schema)
9. [Evaluation KB & Continuous Improvement](#9-evaluation-kb--continuous-improvement)
10. [Operational Notes & Caveats](#10-operational-notes--caveats)
11. [Quick Reference](#11-quick-reference)

---

## 1. Why Nagare Exists

### The Fundamental Limit of All AI Models

Every AI model — regardless of capability — operates inside a **single reasoning session**. Within that session, it:

- Cannot maintain more than 2 million tokens of working context without degradation
- Cannot run parallel sub-tasks with true separation of concerns
- Cannot call itself with a fresh perspective to critique its own output
- Cannot remember lessons from previous runs and apply them to new runs
- Cannot escalate only when necessary without pausing the whole conversation

The result: for any task requiring more than 2-3 coherent reasoning steps, quality collapses. A brilliant translation model becomes inconsistent across chapters. A capable code writer misses cross-file implications. A thorough analyst ignores its own contradictions.

**Nagare solves this at the architecture level** — not by making a bigger model, but by coordinating many focused agents, each excellent at their narrow role, together producing work that no single agent could.

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    NAGARE FLOW SYSTEM                    │
│                                                         │
│  Human (you)                                            │
│      │                                                  │
│      ▼  (one-time pre-flight Q&A)                       │
│  ┌─────────────────────────────────────────────────┐    │
│  │              FlowRunner (Orchestrator)          │    │
│  │  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐       │    │
│  │  │Step 1│→ │Step 2│→ │Step 3│→ │Step N│       │    │
│  │  │Agent │  │Agent │  │Agent │  │Agent │       │    │
│  │  └──────┘  └──────┘  └──────┘  └──────┘       │    │
│  │       ↘         ↓         ↙                    │    │
│  │        [Artifact Store]                         │    │
│  │              │                                  │    │
│  │    [Debug Agent] ← on failure                  │    │
│  └─────────────────────────────────────────────────┘    │
│              │                                          │
│       [Evaluation KB]                                   │
│       (improves next run)                               │
└─────────────────────────────────────────────────────────┘
```

**Core design principles:**

| Principle | Implementation |
|-----------|---------------|
| **Pre-flight, not mid-run** | All human decisions collected once upfront; workflow runs uninterrupted |
| **Declarative, not imperative** | Workflows defined in YAML; engine handles execution |
| **Artifact-driven data flow** | Outputs from each step are versioned files; downstream steps pull by reference |
| **Multi-vendor by design** | Claude, GPT, Codex, Gemini — any model for any step |
| **Event-driven, not polling** | HChat push notifications; no busy-wait |
| **Self-improving** | Every run feeds lessons back into the Evaluation KB |

---

## 3. Why Single-Prompt Cannot Compete

This is not marketing. These are concrete, architectural reasons:

### 3.1 Context Contamination

When a single model writes a document and then reviews it in the same session, it has already "committed" to its choices. Its review is biased — it finds what it expects to find. Nagare sends the output to a **separate agent with a fresh context and a Devil's Advocate role**. That agent has never seen the original prompt and has no ego investment in the design.

### 3.2 Role Specialization vs. Role Conflict

In a single prompt: "Analyze requirements. Then design a solution. Then critique your own design. Then validate." Every transition is a context switch — the model is fighting its own previous output. Nagare assigns each role to a dedicated agent with a **dedicated AGENT.md system prompt** written for exactly that role. The analyst never becomes the validator. The validator never becomes the designer.

### 3.3 Parallel Execution

A single model processes tokens sequentially. Nagare can run multiple independent steps in parallel — for example, translating ten chapters simultaneously, or running three independent validations at once. Wall-clock time shrinks by an order of magnitude for multi-part tasks.

### 3.4 Persistent State Across Sessions

A single prompt loses everything when the session ends. Nagare writes atomic `state.json` and artifact files after every step. A workflow can be paused, resumed days later, or recovered from failure at the exact step that failed — without re-running completed work.

### 3.5 Cross-Vendor Independence

A model cannot evaluate itself without bias. Nagare's meta-workflow explicitly uses GPT-5.4 as the independent evaluator, critic, and reviewer — precisely because Claude cannot be objective about its own outputs. This is architecturally enforced, not a convention.

### 3.6 Continuous Learning

A single prompt knows nothing about the 200 runs before it. Nagare maintains an **Evaluation Knowledge Base** where every run deposits:
- What patterns worked
- What failures occurred
- Improvement proposals (auto-applied or queued for approval)
- Model performance benchmarks per task type

The 201st workflow is genuinely better than the 1st because of what the previous 200 taught the system.

---

## 4. System Components

### 4.1 FlowRunner — The Orchestration Engine

`flow/engine/flow_runner.py` — the central brain (~1,100 lines).

**Responsibilities:**
- Parse workflow YAML into an executable DAG
- Manage state machine: `CREATED → PRE_FLIGHT → CONFIRMED → RUNNING → COMPLETED/FAILED`
- Topologically sort steps by `depends[]` declarations
- Dispatch steps to workers (via `WorkerDispatcher`)
- Substitute variables: `{pre_flight.key}` and `{artifacts.key}` in prompts
- Evaluate `skip_if` conditions at runtime
- Invoke debug agents on failure (up to 3 attempts, then escalate)
- Send HChat push notifications at each milestone

**State machine:**
```
CREATED
    │
    ▼
PRE_FLIGHT  ← human Q&A happens here
    │
    ▼
CONFIRMED   ← all inputs locked; no more human interaction
    │
    ▼
RUNNING     ← steps execute in DAG order
    │
    ├── step fails → DEBUG (auto) → retry up to 3x
    │                              → ESCALATE to human on max_exceeded
    ▼
COMPLETED / FAILED / ABORTED
```

**Control signals (file-based, no polling required):**
- Create `{run_dir}/_pause` → workflow suspends after current step
- Create `{run_dir}/_stop` → workflow aborts cleanly

### 4.2 WorkerDispatcher — The Execution Layer

Spawns actual AI subprocess calls:

```python
# Claude CLI worker
cmd = ["claude", "--print", "--model", model, "--system-prompt", agent_md_content]

# Codex CLI worker
cmd = ["codex", "exec", "--model", model, "--full-auto"]
```

Key detail: removes `CLAUDECODE` environment variable before spawning — this allows nested Claude Code sessions without interference from the parent session.

### 4.3 PreFlightCollector — Human Input

Handles the one-time human interaction phase. Supports:
- **Prefill**: JSON file with pre-answered questions (for automation/testing)
- **Silent mode**: auto-use defaults (for CI/batch runs)
- **Interactive mode**: typed prompts with type-specific UI (text, choice menus)
- **Required validation**: refuses to continue if required questions have no answer

### 4.4 TaskState — Persistence

Thread-safe atomic writes to `state.json`:
```
Write to state.json.tmp → fsync → rename to state.json
```
Survives crashes. Resume always reads consistent state.

### 4.5 ArtifactStore — Data Between Steps

All step outputs are registered as named artifacts:
- `task_analysis.json`, `design_package.json`, `validation_report.json`, etc.
- Referenced in downstream step prompts via `{artifacts.key}`
- Versioned — re-running a step creates a new artifact version without losing history

### 4.6 FlowTrigger — Non-Blocking Launcher

`flow/flow_trigger.py` — starts workflows in the background:

```bash
python flow/flow_trigger.py start meta '{"task_description": "..."}'
# Returns: {"ok": true, "pid": 12345, "expected_run_prefix": "run-meta-2026-03-28-..."}

python flow/flow_trigger.py status run-meta-2026-03-28-...
# Returns: {"status": "running", "steps": {...}, "scores": {...}}
```

`start_new_session=True` truly detaches the workflow from the calling process. The workflow continues running even if the terminal/session that launched it closes.

---

## 5. Human-in-the-Loop: The Pre-Flight System

### 5.1 Core Philosophy

> "Question everything upfront. Execute clean."

Once a workflow enters `RUNNING`, no human interaction is needed or allowed (except escalation on unrecoverable errors). This prevents:
- Mid-task context switches that degrade output quality
- Blocking the workflow on an unread message
- Inconsistent decisions (question answered differently at step 3 vs step 7)

### 5.2 Smart Preflight Module (v1.6.0)

The meta-workflow uses a 4-step intelligent pre-flight pipeline:

**Step 1: Analyst analyzes information gaps**

The analyst categorizes every unknown into one of three `parameter_layer` types:

| Layer | Meaning | Action |
|-------|---------|--------|
| `design_time` | Affects architecture (e.g., "should steps be parallelized?") | **Ask the human** |
| `runtime` | Will be collected when the *generated* workflow runs (e.g., "what file to translate?") | Skip at design time |
| `implementation_detail` | Has a reasonable default (e.g., "chunk size: 2000 tokens") | Auto-use default |

**Step 2: Questioner scores and filters**

3-dimensional quality scoring per question:
```
score = necessity × 0.5 + impact × 0.3 + clarity × 0.2
```
Questions scoring below 0.6 are auto-resolved with smart defaults. Maximum 5 questions sent to human. This prevents question fatigue.

**Step 3: Human answers (with timeout)**

If the human doesn't respond within 300 seconds, the system uses `can_assume=true` defaults and continues automatically. No stalling. No workflow hanging indefinitely.

**Step 4: Validation**

A separate pass checks that the collected answers are internally consistent and sufficient before workflow execution begins.

---

## 6. Multi-Model Strategy

### 6.1 Why Multiple Models?

No single model is optimal for every step:
- **Opus**: Best for deep reasoning — requirements analysis, system design, complex critique
- **Sonnet**: Best for structured formatting — validation, integration, notifications
- **GPT-5.4**: Independent perspective — evaluation, review, critique (avoids Claude self-evaluation bias)
- **Codex/o4-mini**: Best for code execution, tool use, file manipulation

Using the wrong model is both wasteful (Opus for formatting) and harmful (Sonnet for architecture design).

### 6.2 Worker Configuration in YAML

```yaml
workers:
  - id: analyst_01
    backend: claude-cli
    model: claude-opus-4-6          # Complex reasoning → Opus

  - id: validator_01
    backend: claude-cli
    model: claude-sonnet-4-6        # Format checking → Sonnet

  - id: evaluator_01
    backend: openrouter-api
    model: openai/gpt-4.5-preview   # Independent evaluation → GPT

  - id: critic_01
    backend: openrouter-api
    model: openai/gpt-4.5-preview   # Devil's Advocate → different vendor
```

### 6.3 Runtime Backend Override

The `flexible_backend_manager` supports runtime model switching without rewriting YAML:

```python
# Programmatically override for a run
backend_manager.set_model_override(worker_id="analyst_01", model="claude-opus-4-6")

# Per-run backend extra options
backend_extra:
  timeout_seconds: 300
  access_scope: local_only
  tools: [read_file, write_file, bash]
```

### 6.4 Meta-Workflow Model Assignment

| Step | Agent | Model | Rationale |
|------|-------|-------|-----------|
| analyze_requirements | analyst_01 | Opus | Deep requirement analysis |
| generate_preflight_questions | questioner_01 | Opus | Quality judgment calls |
| integrate_preflight_responses | integrator_01 | Sonnet | Structured merge |
| validate_preflight | analyst_01 | Opus | Completeness reasoning |
| design_workflow | designer_01 | Opus | Architecture design |
| critique_design | critic_01 | GPT-5.4 | Independent challenge |
| create_workflow_files | designer_01 | Opus | Complex generation |
| validate_workflow | validator_01 | Sonnet | Schema/format validation |
| independent_review | reviewer_01 | GPT-5.4 | Cross-vendor audit |
| evaluate_and_improve | evaluator_01 | GPT-5.4 | Bias-free evaluation |
| apply_improvement | evaluator_01 | GPT-5.4 | Improvement synthesis |
| notify_completion | evaluator_01 | Sonnet | Simple notification |

---

## 7. How to Use: The Meta-Workflow

The meta-workflow is the system's most powerful capability: **describe a task in natural language, and Nagare designs and creates a complete, validated workflow for it**.

### 7.1 What It Does

Given: "I want a workflow that takes academic papers as input, extracts key claims, searches for contradicting evidence, and writes a critical analysis report"

The meta-workflow will:
1. Analyze what sub-tasks are involved and what decisions need to be made
2. Ask you 1-5 targeted questions (only what truly matters for design)
3. Design a multi-agent workflow with appropriate roles and models
4. Have a Devil's Advocate critique the design
5. Generate the complete YAML file(s)
6. Validate the YAML for correctness and design quality
7. Have an independent reviewer audit the validation
8. Evaluate quality and generate improvement suggestions
9. Apply low-risk improvements automatically
10. Notify you with the result and quality score

### 7.2 How to Start

**Via Akane (recommended):**

Simply describe what you want:
> "帮我创建一个工作流，用来..."

Akane will trigger the meta-workflow and manage the process.

**Via CLI:**

```bash
cd /home/lily/projects/hashi

python flow/flow_trigger.py start meta \
  '{"task_description": "Describe the task here in detail"}'
```

This returns immediately with a `run_id`. The workflow runs in the background.

**Check status:**

```bash
python flow/flow_trigger.py status run-meta-2026-03-28-XXXX
```

**Or list recent runs:**

```bash
python flow/flow_trigger.py list
```

### 7.3 The 12-Step Pipeline

```
Pre-flight
│
├─── [1] analyze_requirements        ← Opus; deep task analysis
│         ↓ task_analysis.json
├─── [2] generate_preflight_questions ← Opus; 3-dim quality scoring
│         ↓ question_set.json
├─── [3] integrate_preflight_responses ← Sonnet; merge answers + defaults
│         ↓ preflight_context.json
├─── [4] validate_preflight          ← Opus; completeness check
│         ↓ preflight_validation.json
│
Design
│
├─── [5] design_workflow             ← Opus; full YAML + rationale + DAG
│         ↓ design_package.json
├─── [6] critique_design             ← GPT-5.4; Devil's Advocate (5-point)
│         ↓ critique_report.json
├─── [7] create_workflow_files       ← Opus; materialize YAML files
│         ↓ creation_report.json
│
Validation
│
├─── [8] validate_workflow           ← Sonnet; format + design check
│         ↓ validation_report.json
├─── [9] independent_review          ← GPT-5.4; cross-vendor audit
│         ↓ review_report.json
│
Improvement
│
├─── [10] evaluate_and_improve       ← GPT-5.4; quality + KB update
│          ↓ evaluation_report.json
├─── [11] apply_improvement          ← GPT-5.4; apply A-class, queue B/C
│          ↓ improvement_package.json + candidate_workflow.yaml
│
Notification
│
└─── [12] notify_completion          ← Sonnet; HChat push to Akane
```

### 7.4 What You Receive

After completion:
- **Generated workflow YAML** in `flow/workflows/`
- **Quality score** (0-10) from the evaluator
- **Design rationale** explaining each architectural choice
- **Pending improvements** recorded in `evaluation_kb/improvements/pending.yaml` for your review
- **HChat notification** with summary

### 7.5 Running the Generated Workflow

Once created, run any workflow the same way:

```bash
python flow/flow_trigger.py start your_new_workflow_name \
  '{"key": "value"}'
```

---

## 8. Workflow YAML Schema

A minimal valid workflow:

```yaml
workflow:
  id: my_workflow
  version: "1.0.0"
  description: "What this workflow does"

pre_flight:
  collect_from_human:
    - key: source_path
      question: "Where is the source file?"
      type: text
      required: true
    - key: output_format
      question: "Output format?"
      type: choice
      choices: [markdown, pdf, docx]
      default: markdown
      required: false

agents:
  orchestrator:
    id: flow-runner
    human_interface: akane

workers:
  - id: worker_01
    role: "Analyst"
    backend: claude-cli
    model: claude-opus-4-6
    workspace: "flow/runs/{run_id}/workers/worker_01"

steps:
  - id: step_01
    name: "Analyze input"
    worker: worker_01
    prompt: |
      Analyze this file: {pre_flight.source_path}
      Output format requested: {pre_flight.output_format}
    artifacts_produced:
      - key: analysis
        filename: analysis.json
    timeout_seconds: 300

  - id: step_02
    name: "Generate output"
    worker: worker_01
    depends: [step_01]
    prompt: |
      Based on the analysis: {artifacts.analysis}
      Generate the final output.
    artifacts_produced:
      - key: final_output
        filename: output.md
    timeout_seconds: 600

error_handling:
  debug_agent: worker_01
  max_attempts: 3
  retry_strategy: prompt_adjustment

success_criteria:
  - "step_02 completed with artifacts_produced"
```

**Key YAML features:**

| Feature | Syntax | Purpose |
|---------|--------|---------|
| Pre-flight input | `{pre_flight.key}` | Inject human answers into prompts |
| Artifact reference | `{artifacts.key}` | Inject upstream step outputs |
| Step dependency | `depends: [step_id]` | DAG ordering |
| Conditional skip | `skip_if: "artifacts.key == null"` | Dynamic branching |
| Model per step | `model: claude-opus-4-6` | Step-level model override |
| Debug recovery | `error_handling.debug_agent` | Auto-recovery agent |

---

## 9. Evaluation KB & Continuous Improvement

### 9.1 Structure

```
flow/evaluation_kb/
├── patterns/
│   ├── successful.yaml     ← What works: model choices, timeouts, agent combos
│   └── failure.yaml        ← What fails: common pitfalls, risky patterns
├── model_performance/
│   └── benchmarks.yaml     ← Opus vs Sonnet vs GPT per task type
├── improvements/
│   ├── pending.yaml        ← New proposals awaiting your review
│   ├── accepted.yaml       ← You approved — waiting implementation
│   ├── applied.yaml        ← Auto-applied (A-class)
│   └── implemented.yaml    ← Archived history
└── workflow_versions/
    └── {workflow_id}/
        ├── v1.0.0.yaml     ← Historical versions
        └── v1.1.0.yaml
```

### 9.2 Improvement Classes

| Class | Risk | Requires approval | Examples |
|-------|------|------------------|---------|
| **A** | Low | No — auto-applied | Prompt text rewording, timeout tweaks, default value changes |
| **B** | Medium | Yes | Agent role changes, step restructuring, model substitution |
| **C** | High | Yes | New agents, DAG restructuring, core logic changes |

### 9.3 The Learning Loop

```
Run N completes
    ↓
Evaluator reads evaluation_events.jsonl (full event stream)
    ↓
Extracts patterns + identifies failures + benchmarks model performance
    ↓
Generates improvement proposals with confidence scores
    ↓
A-class: auto-applied → improvement_package.json
B/C-class: written to pending.yaml → wait for approval
    ↓
Creates candidate_workflow.yaml (vNext)
    ↓
Run N+1 uses candidate if it exists
    ↓
If N+1 succeeds → promote candidate to new version
If N+1 fails → revert, delete candidate
```

### 9.4 Metrics Tracked Per Run

- **efficiency_score** (0-10): Duration vs task complexity
- **quality_score** (0-10): Output quality (downstream feedback)
- **stability_score** (0-10): Success rate, debug attempts needed
- **intervention_score** (0-10): Human interventions vs expected
- **improvement_adoption_rate** (0.0-1.0): What % of KB suggestions the designer adopted

---

## 10. Operational Notes & Caveats

### ✅ Do

- **Describe tasks in detail** during pre-flight. Vague descriptions produce vague workflows.
- **Review `pending.yaml`** periodically. B/C-class improvements accumulate there and won't auto-apply.
- **Check quality scores** after runs. A score below 6 usually means the design needs review.
- **Use the meta-workflow** for new tasks rather than writing YAML by hand. It applies KB patterns automatically.
- **Let debug agents handle first-level failures** — they often self-recover. Only intervene if escalated.

### ⚠️ Be Aware

- **Pre-flight timeout is 300 seconds.** If you don't answer design questions within 5 minutes, smart defaults are used. Check `preflight_context.json` if you're unsure what assumptions were made.
- **Candidate workflows are not promoted automatically.** They run once as candidates; if successful, they replace the previous version. You can inspect candidates in `flow/workflows/` before the next run.
- **GPT-based steps require OpenRouter API access.** If the `openrouter-api` backend is unavailable, independent review and evaluation steps will fail. Use Claude fallback if needed.
- **Parallel steps share the artifact store.** Two parallel steps producing an artifact with the same key will collide. Always use unique artifact keys.
- **The `_pause` signal is checked between steps**, not within a running step. A paused signal will take effect only after the current step finishes.

### ❌ Don't

- Don't manually edit `state.json` during a run — use signal files (`_pause`, `_stop`) for control.
- Don't delete `evaluation_kb/` — it contains the system's accumulated knowledge from all previous runs.
- Don't run multiple instances of the same workflow simultaneously — artifact store collisions will corrupt results.
- Don't rely on polling to check workflow status — use HChat notifications or `flow_trigger.py status`.

---

## 11. Quick Reference

### Start a workflow

```bash
python flow/flow_trigger.py start <workflow_alias> '<json_prefill>'
```

**Aliases:**

| Alias | Workflow | Use when |
|-------|----------|---------|
| `meta` | meta_workflow_creation | Creating new workflows |
| `book_translation` | book_translation | Translating documents |
| `smoke_test` | smoke_test | Testing the system |

### Check status

```bash
python flow/flow_trigger.py status <run_id_prefix>
python flow/flow_trigger.py list
```

### Control a running workflow

```bash
# Pause after current step
touch flow/runs/<run_id>/_pause

# Stop cleanly
touch flow/runs/<run_id>/_stop

# Resume from pause
rm flow/runs/<run_id>/_pause
```

### Review pending improvements

```bash
cat flow/evaluation_kb/improvements/pending.yaml
```

### Workflow run directory structure

```
flow/runs/<run_id>/
├── state.json               ← Current workflow state (all steps)
├── artifacts/               ← All produced artifacts
│   ├── task_analysis.json
│   ├── design_package.json
│   └── ...
├── workers/                 ← Per-agent workspaces
│   ├── analyst_01/
│   └── designer_01/
├── evaluation_events.jsonl  ← Full event stream for evaluator
├── _pause                   ← Create to pause (delete to resume)
└── _stop                    ← Create to stop cleanly
```

---

*Nagare — because the most capable model and the cleverest prompt are still just one voice. Orchestration is the difference between a monologue and a symphony.*
