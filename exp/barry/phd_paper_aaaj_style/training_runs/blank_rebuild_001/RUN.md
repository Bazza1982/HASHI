# Blank Rebuild Training Run 001

Status: exact visual baseline passed; EXP remains candidate.

## Target

Primary source:

```text
training_materials/with_new_template_paper_1_slr_aaaj_jan_2026/Paper 1 - SLR with AAAJ template (Jan 2026).docx
```

## Goal

Create a new Word document from an empty document that matches Barry's PhD
Paper 1 AAAJ-style draft, including document structure, tables, pictures,
references, and layout.

## Evidence

- `state/source_style_map.json`
- `state/blank_rebuild_build_log.json`
- `state/blank_rebuild_v2_build_log.json`
- `state/training_run_report.json`
- `artifacts/source_target_aaaj_style.pdf`
- `output/blank_rebuild_aaaj_style_practice.docx`
- `output/blank_rebuild_aaaj_style_practice.pdf`
- `output/blank_rebuild_aaaj_style_practice_v2_original_format.docx`
- `output/blank_rebuild_aaaj_style_practice_v2_original_format.pdf`
- `output/blank_rebuild_aaaj_style_practice_v6_source_template_context.docx`
- `output/blank_rebuild_aaaj_style_practice_v6_source_template_context.pdf`
- `artifacts/visual_compare_v6/visual_compare_report.json`

## Results

- Source PDF pages: 66
- V1 PDF pages: 80
- V2 PDF pages: 66, but failed page-level visual comparison
- V6 PDF pages: 66, passed page-level visual comparison with zero pixel
  difference across all rendered pages
- Source tables: 5
- Rebuilt tables: 5
- Source inline shapes: 8
- Rebuilt inline shapes: 8
- Source sections: 6
- Rebuilt sections: 6

## Findings

V1 used Word object-model `FormattedText` insertion into a blank document. It
preserved object counts but failed layout validation because pagination changed
from 66 to 80 pages.

V2 used an empty Word document and Word's original-format insertion path. It
preserved the 66-page PDF baseline and matched the key structural counts, but
failed visual exactness because page content shifted.

V6 created a new document using the source document as the template context,
cleared the new document, and rebuilt the content into the empty document. This
passed the strict visual comparison: all 66 rendered PDF pages matched the
source with zero measured pixel difference.

## Caveat

V6 is an exact baseline reconstruction, not yet a stable style EXP. It starts
from an empty document inside the source template context and is not a
filesystem copy, but it still relies on source context and source content
insertion. The next stage should extract explicit AAAJ/Berry/supervisor style
rules and test them on a smaller new sample.

## Next training stage

1. Extract stable style rules from multiple draft versions.
2. Separate AAAJ template requirements from Barry's writing habits.
3. Separate supervisor preferences from generic academic conventions.
4. Rebuild a new practice section using extracted rules rather than source
   content insertion.
5. Ask Barry to judge whether the generated section feels right.
