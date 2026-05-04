# Gantt Chart Duplicate Playbook

Use this playbook when Barry asks for an exact duplicate of a trained Excel
Gantt workbook.

## Exact Duplicate

If Barry asks only for an exact duplicate, a direct workbook copy is acceptable
when the intent is preservation. Validate with SHA256 and Excel-rendered PDF
comparison.

## From-Blank Reconstruction

If Barry asks to recreate the workbook from blank:

1. Build a new workbook and sheet.
2. Copy visible cell values and formulas.
3. Copy font, fill, border, alignment, number format, and protection objects.
4. Preserve column widths, row heights, merged ranges, page margins, print
   scaling, and orientation.
5. Copy the source workbook theme palette before saving.
6. Identify non-cell visuals in the xlsx package:
   - drawings
   - VML drawings
   - header/footer text
   - sheet relationship files
7. Patch required package parts by rewriting a clean xlsx zip.
8. Export through Excel and compare rendered PDFs.

Do not copy internal openpyxl `_style` ids into a new workbook. They can point to
missing style table entries and break merged-cell handling.

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
