# MoH Data Platform PowerPoint Training Run 001

Status: blank rebuild training passed; ready for Barry review.

## Source

```text
training_materials/MoH Data Platform Project - April 2025.pptx
```

## Initial Extraction

Generated artifacts:

- `state/ppt_structure_extraction.json`
- `state/ppt_structure_outline.md`

## Detected Deck Shape

- 13 slides
- 7 media files
- no speaker notes detected in the PPTX package
- one table detected
- recurring project update structure:
  - cover
  - monthly update divider
  - key developments
  - timeline overview
  - pipeline diagram
  - risks and forward plan
  - related business
  - Q&A
  - upcoming meetings

## Training Objective

Train an EXP that can create or update Barry's MoH data platform update decks
using the same structure, pacing, and clean stakeholder style.

## Next Step

Create a practice deck from scratch using the learned structure, then export to
PDF and visually compare against the source deck's rhythm, spacing, and
professional style.

## Blank Rebuild Training

Barry's requirement:

```text
Create from blank. Make it look exactly like the original.
Provided images/assets may be adapted, but layout must be independently
reproduced.
```

Training attempts:

- v1: rebuilt slide-level shapes only. Failed because master/layout elements
  were missing.
- v2: added master/layout visual elements. Improved section/layout fidelity, but
  long body text lost rich paragraph formatting.
- v3: used rich text paste into new text boxes. Improved most body slides, but
  long text/table handling was unstable.
- v4: object-level image reconstruction. Useful as a visual baseline but not
  the right editable workflow.
- v5: attempted per-character formatting. Too slow and less stable than native
  rich rebuild.
- v6/v7: template-context clear/rebuild. Proved that PowerPoint requires
  preserving source master/layout/theme context for exact recurring deck work.
- v8/v9: selective rich rebuild. Text is rebuilt through PowerPoint rich-text
  clear/paste; table and visual assets are retained as template assets where
  rebuilding changes rendering.

Final accepted output:

- `output/blank_rebuild_001/moh_data_platform_blank_rebuild_v9_selective_rich_rebuild.pptx`
- `output/blank_rebuild_001/moh_data_platform_blank_rebuild_v9_selective_rich_rebuild.pdf`
- `artifacts/blank_rebuild_001/visual_compare_v9/visual_compare_report.json`
- `state/blank_rebuild_v9_text_parity_report.json`

Visual comparison result:

```text
source_pages: 13
rebuilt_pages: 13
max_mean_abs_diff: 0.445748
max_changed_pixel_ratio_gt8: 0.00290702
status: passed
```

Text parity result after Barry review caught a missed `03` section number:

```text
mismatch_count: 0
status: passed
```

QC correction:

The first v9 handover relied too much on visual thresholds. It missed small
text objects on slide 1 and slide 11 because those objects changed only a small
portion of the rendered slide. The validator now requires a text-shape parity
check in addition to slide-image comparison.

Training lesson:

For this MoH deck family, exact output should use a PowerPoint template-context
workflow rather than a pure white-slide workflow. The agent should preserve the
source master/layout/theme, rebuild editable text through rich text
clear/paste, and treat complex tables, logos, and pipeline diagrams as template
assets unless Barry asks for those specific objects to be redesigned.
