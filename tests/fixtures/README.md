# Nagare Fixture Inventory

This directory freezes the workflow shapes that `nagare-core` and `nagare-viz`
must preserve during extraction.

Fixture selection rules:

- prefer real workflows over toy examples
- include at least one legacy dialect that does not match the current schema
- include at least one HITL workflow
- include at least one workflow with parallel structure
- include at least one workflow with nested config and evaluator semantics
- include one synthetic file with unknown fields and editor-owned metadata

Current fixture set:

- `smoke_test.yaml`
  Source: `flow/workflows/examples/smoke_test.yaml`
  Role: minimal happy path; two-step linear DAG; no pre-flight questions.
- `book_translation.yaml`
  Source: `flow/workflows/library/book_translation.yaml`
  Role: multi-step workflow with parallel branches, defaults, and artifact fan-in.
- `academic_writing_paragraph.yaml`
  Source: `flow/minato/ai-consulting/shimanto/workflow-work/nagare/academic-writing-paragraph/workflow.yaml`
  Role: nested step config, richer pre-flight, scope notice/defaults, and quality-gate style semantics.
- `meta_workflow_creation.yaml`
  Source: `flow/workflows/examples/meta_workflow_creation.yaml`
  Role: high-complexity real workflow; HITL wait flow; evaluator/reviewer/critic/debug semantics; large prompt payloads.
- `legacy_english_news_to_chinese_markdown.yaml`
  Source: frozen compatibility fixture; its former library workflow is retired
  from the published runtime catalogue.
  Role: legacy hand-authored dialect that does not match the current schema; guards against overfitting to the ideal model.
- `unknown_fields_workflow.yaml`
  Source: synthetic preservation fixture
  Role: explicit preservation test for comments, unknown fields, ordering-sensitive areas, and `x-nagare-viz` metadata.

Runtime expectations tracked for each fixture include:

- state machine transitions
- produced artifacts
- emitted JSONL events
- human pause/resume behavior where relevant
- failure and retry behavior where relevant

See also:

- `tests/fixtures/manifest.json`
- `tests/fixtures/runtime_inventory.md`
