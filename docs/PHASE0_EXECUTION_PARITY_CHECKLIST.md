# Phase 0 Execution Parity Checklist

This checklist freezes what "parity" means before extraction work starts.

## Workflow Loading

- current HASHI workflow files still load without importing from `hashi/`
- fixture corpus remains readable:
  - `smoke_test.yaml`
  - `book_translation.yaml`
  - `academic_writing_paragraph.yaml`
  - `meta_workflow_creation.yaml`
  - `legacy_english_news_to_chinese_markdown.yaml`
  - `unknown_fields_workflow.yaml`

## Runtime Semantics

- `depends` continues to define execution ordering
- parallel strategy fields remain meaningful
- `wait_for_human` remains distinct from failure
- timeout and retry semantics remain observable in logs/state
- artifact references remain resolvable by the same keys

## CLI and Persistence

- recognizable commands remain: `run`, `status`, `list`, `resume`
- per-run state remains inspectable
- event stream remains readable, including compatibility with `evaluation_events.jsonl`

## GUI Safety

- visual layout never overrides execution semantics
- unsupported fields are surfaced instead of dropped
- raw YAML remains available
- export blocks or warns when fidelity is at risk

## Verification Sequence For Later Phases

1. Load each fixture in parser/validator tests.
2. Execute the executable fixtures through `nagare run`.
3. Compare step ordering, artifact keys, and terminal run status.
4. Compare emitted event names and required correlation ids.
5. Import/export in `nagare-viz` and re-run semantics checks.
