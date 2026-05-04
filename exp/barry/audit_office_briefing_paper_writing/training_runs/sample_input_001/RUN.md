# Audit Office Briefing Paper Real Input Run 001

Status: draft generated and QA passed.

## Input

```text
training_materials/sample_input.docx
```

Original folder:

```text
C:\Users\Print\OneDrive - The University Of Newcastle\ai\paper_writing
```

## Task

Use the unformatted input content and produce a well-formatted Audit Office
briefing note using the learned briefing paper EXP.

## Output

Final draft:

```text
output/draft_001/data_platform_retention_briefing_note_draft_v6.docx
output/draft_001/data_platform_retention_briefing_note_draft_v6.pdf
```

Earlier internal drafts:

```text
output/draft_001/data_platform_retention_briefing_note_draft_v1.docx
output/draft_001/data_platform_retention_briefing_note_draft_v2.docx
output/draft_001/data_platform_retention_briefing_note_draft_v3.docx
output/draft_001/data_platform_retention_briefing_note_draft_v4.docx
output/draft_001/data_platform_retention_briefing_note_draft_v5.docx
```

## Method

The run used the learned Audit Office briefing paper style context, cleared the
body, then wrote a new structured briefing note from the raw input material.

The input content was mapped into:

- Purpose
- What is proposed and why it matters
- Current context
- Legal, policy and privacy considerations
- What is in scope
- What is out of scope
- Strategic Alignment
- Lifecycle management for Nuix and DAC-hosted environments
- Benefits of the proposed approach
- Risks and mitigations
- Recommendation

## QA Results

```text
pages: 3
words: 868
paragraphs: 106 by Word export / 43 meaningful text paragraphs by XML QA
tables: 3
required sections: all present
section order: pass
mojibake check: pass
PDF render: pass
```

Reports:

```text
state/draft_v6_word_export_report.json
state/draft_v6_pdf_render_report.json
state/draft_v6_text_qa_report.json
```

Rendered page previews:

```text
artifacts/draft_006_pages/
```

## Review Notes

Draft v1 was structurally sound but the strategic alignment table split
awkwardly across pages. Draft v2 fixed table row splitting but pushed only the
recommendation bullets to a mostly blank final page. Draft v3 compressed the
tables and later sections to produce a cleaner 3-page review draft.

Barry reviewed the v3 Word draft and identified that it was not ready to
stabilise: the top box used visible grid borders instead of the sample's
rule-line layout, colours and fonts were partly inconsistent, and paragraph
spacing was awkward.

Draft v4 preserved the source metadata table styling and removed manual font
overrides, but failed review because merged-cell handling cleared several
metadata values. Draft v5 fixed the metadata values but inserted the strategic
alignment table in the wrong order. Draft v6 is the first post-review candidate
that passes visual inspection and text/structure QA: metadata table layout is
source-like, body tables are in the correct order, sections are complete, and
no mojibake was detected.
