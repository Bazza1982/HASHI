# Nagare YAML Round-Trip Contract

Nagare Viz must preserve workflow meaning and must not silently discard user-authored data.
Runtime validity and text fidelity are separate checks: a file can be preserved losslessly while
still being intentionally invalid under the current runtime contract.

## Preservation Rules

1. A no-op Python or editor export returns the original source unchanged.
2. Unknown top-level and nested fields remain present unless the user explicitly removes them.
3. `depends` is authoritative for execution order; canvas position is not.
4. Editor layout belongs only under `x-nagare-viz`.
5. A structural form edit must not silently convert legacy or unsupported semantics.
6. Parse failures, invalid DAGs, retired fields, and unsupported backends block form export.
7. Runtime overlays bind to a persisted run snapshot, never to a mutable editor draft.

## Current Export Paths

- **No-op export:** returns the original YAML source unchanged.
- **Metadata-only export:** replaces or appends only `x-nagare-viz`; surrounding source, comments,
  unknown fields, and ordering are preserved.
- **Structured form export:** rewrites supported fields from draft state. Unknown fields are carried
  forward, but comments and exact ordering are not guaranteed, so the editor shows a fidelity
  warning.
- **Raw YAML:** preserves full user control and is the only semantic edit mode for Class C files.

There is no override that allows a known runtime-invalid form export to masquerade as safe.

## Compatibility Classes

- **Class A:** form-editable without a known fidelity warning.
- **Class B:** inspectable and partially form-editable; comments or unsupported fields require a
  visible warning.
- **Class C:** inspect and raw-edit only.

The current fixture results are:

| Fixture | Class | Reason |
|---|---|---|
| `smoke_test.yaml` | B | comments present |
| `book_translation.yaml` | B | comments present |
| `academic_writing_paragraph.yaml` | B | comments present |
| `meta_workflow_creation.yaml` | B | comments present |
| `unknown_fields_workflow.yaml` | B | comments plus preserved extensions |
| `legacy_english_news_to_chinese_markdown.yaml` | C | legacy dialect |

The unknown-fields fixture deliberately contains a retired timeout field. The codec must preserve
it, while runtime validation must block it. That is an adversarial preservation test, not a
publishable workflow example.

## Unknown Fields

Unknown data is categorized for display, not silently normalized:

- editor extension data such as `x-nagare-viz`;
- future engine extensions;
- foreign or legacy fields.

The editor lists unsupported scopes. Class B metadata-only edits are allowed; structural export is
warned or blocked according to the active fidelity and runtime findings. Class C stays in raw mode.

## Blocking Conditions

Form export is blocked for:

- duplicate YAML keys or parse errors;
- duplicate step IDs, dependency cycles, or missing step/agent references;
- unsupported runtime backends or strategies;
- invalid required workflow, worker, step, input, output, artifact, or gate fields;
- retired fixed timeout/retry controls and retired worker fields;
- Class C structural edits.

## Contract Evidence

The Python and frontend suites assert:

- the fixture manifest is complete;
- no-op source equality;
- preservation of comments, unknown fields, and editor metadata;
- duplicate-key and DAG rejection;
- runtime rejection of preserved retired fields;
- editor export blocking and compatibility-class behavior;
- execution of an exported valid workflow.

These tests are active requirements; none is an allowed failure or `xfail`.
