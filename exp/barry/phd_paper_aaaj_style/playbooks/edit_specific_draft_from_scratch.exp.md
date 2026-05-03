# Edit Specific Draft From Scratch EXP

Status: stable for Barry's PhD Paper 1 AAAJ draft.

## Intent

Edit or rebuild Barry's specific PhD Paper 1 AAAJ Word draft from a new blank
Word document while preserving the source draft's exact visual style and layout.

## When to use

Use this playbook when Barry asks for:

- editing this specific Paper 1 AAAJ draft
- rebuilding the draft from scratch
- preserving the exact Word/PDF appearance
- making content edits without breaking template layout
- producing a visually identical or near-identical Word/PDF output

## Required context

- Source draft: `Paper 1 - SLR with AAAJ template (Jan 2026).docx`
- Template context: source draft document context
- Validation: rendered PDF page-by-page visual comparison

## Stable procedure

1. Open the source draft read-only.
2. Create a new Word document using the source draft as template context.
3. Clear the new document content.
4. Rebuild the content into the empty document.
5. Apply requested edits in the rebuilt document.
6. Save as a new `.docx`; never overwrite the source.
7. Export both source and edited output to PDF when visual preservation matters.
8. Render the PDFs page by page.
9. Compare every page visually.
10. Treat the run as passed only if the visual threshold is satisfied.

## Validation standard

For exact reproduction:

- source and rebuilt page counts must match
- table count must match unless the edit changes tables intentionally
- inline shape count must match unless the edit changes visuals intentionally
- section count must match
- rendered pages must match pixel-for-pixel for unchanged pages
- changed pages must be reviewed against the requested edit

## Known failure modes

- `FormattedText` transfer can preserve object counts while changing pagination.
- Matching page count is not enough; content can shift between pages.
- Directly syncing source `settings.xml`/`webSettings.xml` into a rebuilt package
  can corrupt the DOCX.

## Evidence

Validated by:

```text
training_runs/blank_rebuild_001/artifacts/visual_compare_v6/visual_compare_report.json
```

The passing baseline had:

- 66 source pages
- 66 rebuilt pages
- maximum mean pixel difference: 0.0
- maximum changed pixel ratio: 0.0
