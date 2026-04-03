# Fixture Runtime Inventory

This file records the runtime behavior and artifact expectations that Phase 1+
must preserve or verify.

## `smoke_test.yaml`

- Execution shape: linear two-step DAG
- Human interaction: none
- Declared artifacts:
  - `quote` -> `output.txt`
  - `review` -> `review.txt`
- Important runtime expectations:
  - `step_check` depends on `step_write`
  - output JSON includes `artifacts_produced`
  - simple success path should remain executable with minimal setup

## `book_translation.yaml`

- Execution shape: analyze -> two parallel translation branches -> merge/review -> package
- Human interaction: pre-flight collection only
- Declared artifacts:
  - `document_analysis` -> `document_analysis.json`
  - `translation_first_half` -> `translation_first_half.md`
  - `translation_second_half` -> `translation_second_half.md`
  - downstream merge/package artifacts declared later in file
- Important runtime expectations:
  - `translate_first_half` and `translate_second_half` remain parallel-capable
  - pre-flight defaults and choices remain intact
  - fan-in step semantics remain unchanged

## `academic_writing_paragraph.yaml`

- Execution shape: linear authored pipeline with nested prompt/config content
- Human interaction: pre-flight collection only
- Declared artifacts:
  - `paragraph_outline` -> `paragraph_outline.json`
  - additional draft/review/polish artifacts declared later in file
- Important runtime expectations:
  - evidence mode branching remains driven by prompt/runtime logic, not canvas layout
  - scope notice/defaults remain preserved
  - long prompt blocks and quality threshold text survive round-trip

## `meta_workflow_creation.yaml`

- Execution shape: high-complexity real workflow with multiple review/evaluation stages
- Human interaction:
  - pre-flight questions
  - `wait_for_human` step with timeout recovery
- Declared artifacts:
  - `task_analysis`
  - `question_set`
  - `preflight_context`
  - `design_package`
  - `critique_report`
  - additional validation/evaluation/publish artifacts declared later in file
- Important runtime expectations:
  - `wait_for_human` and timeout semantics remain explicit
  - evaluator, reviewer, critic, and debug roles remain distinguishable in logs and runtime state
  - large prompts and changelog/history blocks remain loadable

## `legacy_english_news_to_chinese_markdown.yaml`

- Execution shape: legacy sequential task list, not current canonical schema
- Human interaction: none declared in current file shape
- Declared outputs:
  - `markdown_file`
  - `translation_report`
- Important runtime expectations:
  - importer must detect noncanonical dialect safely
  - visual editor must not pretend this is fully form-editable without a compatibility layer
  - unsupported legacy keys remain inspectable and recoverable

## `unknown_fields_workflow.yaml`

- Execution shape: minimal single-step workflow
- Human interaction: none
- Declared artifacts: none beyond prompt-directed `output.txt`
- Important runtime expectations:
  - `x-team-note`, `x-worker-extension`, `x-step-note`, and `x-nagare-viz` survive round-trip
  - comment-loss risk remains visible in tests until a preserving codec exists
