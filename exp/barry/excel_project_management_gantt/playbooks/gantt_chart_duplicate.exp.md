# Gantt Chart Duplicate Playbook

Use this playbook when Barry asks for an exact duplicate of a trained Excel
Gantt workbook.

## Steps

1. Copy the source workbook into the training material area.
2. Create the duplicate workbook.
3. Compare source and duplicate hashes when exact copy is expected.
4. Open both with Excel and export to PDF.
5. Render exported PDFs to images.
6. Compare source and duplicate page renders.
7. Record whether page count, page size, and pixel differences match.

## Acceptance

For exact duplicate training, pass only when:

- workbook hash matches, or the intended edit explains the hash change
- Excel PDF page count matches
- rendered visual difference is zero or fully explained
- print layout remains 1 page wide by 1 page tall unless Barry asks otherwise

