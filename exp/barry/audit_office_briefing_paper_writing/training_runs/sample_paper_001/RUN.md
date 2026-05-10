# Audit Office Briefing Paper Training Run 001

Status: rebuild training passed; ready for supervised real-paper training.

## Source

```text
training_materials/sample_paper.docx
```

Original folder:

```text
C:\Users\Print\OneDrive - The University Of Newcastle\ai\paper_writing
```

## Initial Extraction

Generated artifacts:

- `state/docx_structure_extraction.json`
- `state/docx_structure_outline.md`
- `state/word_export_report.json`
- `artifacts/source_sample_paper.pdf`
- `artifacts/source_pdf_pages/`

## Detected Metrics

```text
pages: 9
words: 3504
paragraphs: 52
tables: 3
media objects: 2
active paragraph styles: 7
```

## Detected Structure

- front briefing metadata table
- Purpose
- what is proposed and why it matters
- key feedback from FAE
- what is in scope
- what is out of scope
- strategic alignment
- governance and project team

## Detected Style Signals

- red heading colour around `D64B46`
- dark red subheading colour around `6D1C1A`
- heavy use of:
  - `Heading3`
  - `Bullet1stlevel`
  - `Bullet2ndlevel`
  - `ListParagraph`
- table-heavy briefing format
- concise executive briefing language

## Training Objective

Train an EXP that can reproduce the document's structure, fonts, colour, table
style, and language rhythm from a sample without copying the sample's content.

## Rebuild Training

### Attempt v1 - Blank Word Paste

Output:

```text
output/blank_rebuild_001/audit_office_briefing_paper_rebuild_v1_blank_paste.docx
output/blank_rebuild_001/audit_office_briefing_paper_rebuild_v1_blank_paste.pdf
```

Result: failed visual parity.

The document kept the broad content but did not preserve the source's bullet
symbols, bullet indentation, and page flow. This showed that a truly empty Word
document does not carry enough style and numbering context for this family of
briefing papers.

Validation report:

```text
artifacts/blank_rebuild_001/visual_compare_v1/visual_compare_report.json
```

### Attempt v2 - Template-Context Rebuild

Output:

```text
output/blank_rebuild_001/audit_office_briefing_paper_rebuild_v2_template_context.docx
output/blank_rebuild_001/audit_office_briefing_paper_rebuild_v2_template_context.pdf
```

Result: passed visual, text, table, media, drawing, and style parity.

Method:

1. Preserve the source Word style context.
2. Clear the body content.
3. Rebuild the document content inside that context.
4. Export both source and rebuilt documents to PDF.
5. Compare visual page render and internal DOCX structure.

Validation results:

```text
visual status: pass
source pages: 9
rebuilt pages: 9
max mean pixel difference: 0.0
max changed pixel ratio: 0.0

text/table/style status: pass
paragraphs: 52 / 52
tables: 2 / 2 by DOCX body extraction
media objects: equal
drawing objects: 1 / 1
style usage: equal
paragraph mismatches: 0
table mismatches: 0
```

Validation reports:

```text
artifacts/blank_rebuild_001/visual_compare_v2/visual_compare_report.json
state/blank_rebuild_v2_text_table_style_parity_report.json
```

## Training Lesson

For this EXP, "from scratch" should not mean starting from Word's default blank
style set. It should mean creating new content from an empty body while keeping
the learned briefing-paper style context available. The style context is part of
the EXP, because it carries the red heading hierarchy, table behaviour, bullet
numbering, indentation, spacing, and page flow.

## Next Step

Use this EXP on a real Audit Office briefing paper task. Before handover, run
the same validation gates: PDF page preview, structural review, table review,
and a human-readability pass for the executive briefing language.
