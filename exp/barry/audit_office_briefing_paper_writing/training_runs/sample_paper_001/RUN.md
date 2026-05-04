# Audit Office Briefing Paper Training Run 001

Status: source saved and initial structure extracted.

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

## Next Step

Run blank/template-context rebuild training:

1. create a practice document using the learned structure
2. export source and rebuilt documents to PDF
3. compare page flow, styles, bullets, and tables
4. add failure memory and validators before handover
