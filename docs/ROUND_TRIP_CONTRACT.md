# Nagare YAML Round-Trip Contract

## Purpose

This document defines what `nagare-viz` and future codec code are allowed to
change when loading and exporting Nagare workflow YAML.

The standard is not "exports valid YAML." The standard is "preserves workflow
meaning and does not silently discard user-authored data."

## Scope

This contract applies to:

- `nagare-core` YAML parsing and serialization
- `nagare-viz` visual editing
- raw YAML edit mode
- future migration/import tools

It applies to the Phase 0 fixture corpus in
`tests/fixtures/manifest.json`.

## Non-Negotiable Preservation Rules

1. A workflow file that loads successfully must be exportable again without
   dropping known fields.
2. Unknown top-level fields must be preserved unless the user explicitly deletes
   them.
3. Unknown nested fields under `agents`, `steps`, `pre_flight`, `meta`,
   `error_handling`, `evaluation`, and `output` must be preserved unless the
   user explicitly deletes them.
4. `depends` semantics are authoritative for execution order. Visual layout is
   never authoritative.
5. Editor-owned layout metadata must live under `x-nagare-viz`.
6. Export must not silently convert a workflow into a different execution model.
7. Runtime overlays must bind to immutable run snapshots, not the mutable draft
   currently open in the editor.
8. When the editor cannot preserve a property with confidence, export must be
   blocked or require an explicit warning/override path.

## Comments and Ordering

Comments and field ordering matter for hand-maintained workflow files, but they
may not always be preservable through every edit path.

Rules:

- comment preservation is the target for no-op loads, raw-YAML edits, and
  metadata-only edits
- if comments will be lost on export, the UI must warn before export
- existing key ordering should be preserved where practical
- if ordering is normalized, the export path must warn before export

The Phase 0 fixture `tests/fixtures/unknown_fields_workflow.yaml` exists
specifically to keep this risk visible.

## Unknown Field Policy

Unknown fields fall into three buckets:

- editor-owned extension fields
  Example: `x-nagare-viz`
- engine-owned future extension fields
  Example: future `x-nagare-*` blocks
- foreign or legacy fields
  Example: `workflow_id`, `tasks`, `input_binding`, custom vendor metadata

Policy:

- preserve all three buckets on load/export
- surface unknown fields in the UI, even if they are not form-editable
- do not silently remap foreign fields into canonical fields
- do not silently delete legacy dialect content because it does not fit the new
  schema

## Compatibility Classes

### Class A: Full-fidelity editable

The file can be safely edited visually and exported without a fidelity warning.

### Class B: Editable with warning

The file can be rendered and partially edited, but some comments, ordering, or
unsupported sections may not survive export. The user must see a warning.

### Class C: Inspect and raw-edit only

The file can be opened, validated, and edited in raw YAML mode, but the visual
editor must not claim safe round-trip support.

Current expectation by fixture:

- `smoke_test.yaml`: Class A
- `book_translation.yaml`: Class A/B depending on codec maturity
- `academic_writing_paragraph.yaml`: Class A/B depending on codec maturity
- `meta_workflow_creation.yaml`: Class B
- `legacy_english_news_to_chinese_markdown.yaml`: Class C
- `unknown_fields_workflow.yaml`: Class B until comment-preserving export exists

## Canonical Editor Metadata

Editor layout metadata is allowed only in a dedicated extension block:

```yaml
x-nagare-viz:
  version: 1
  nodes:
    draft:
      position:
        x: 120
        y: 80
```

Rules:

- this block must not affect execution
- unknown fields inside this block must also be preserved
- deleting this block must only remove editor metadata, not execution data

## Export Blocking Conditions

The editor must block export or require explicit confirmation when:

- duplicate step ids exist
- references point to missing agents or steps
- the graph cannot be mapped back to the YAML execution semantics
- unsupported legacy constructs would be discarded
- comment/order preservation is known to be broken for the chosen edit path
- raw YAML contains parse errors

## Contract Tests Required

At minimum, the automated suite must cover:

- fixture inventory exists and remains readable
- unknown top-level and nested fields survive round-trip
- editor metadata survives round-trip
- no-op load/export does not change execution semantics
- legacy workflow fixtures are detected and downgraded safely instead of being
  "normalized" destructively

## Current Gaps

Phase 0 records the contract before the codec exists. The initial contract test
is allowed to fail or xfail until a real round-trip implementation is present.
