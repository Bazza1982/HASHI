# Gantt Chart Validators

Use these checks before handing an Excel Gantt artefact to Barry.

## Workbook Checks

- Sheet count and sheet names are expected.
- Used range dimensions are expected.
- Merged ranges are preserved where relevant.
- Row heights and column widths are preserved where visual layout matters.
- Conditional formatting, fills, borders, and formulas are preserved or
  intentionally updated.

## Visual Checks

- Export through Excel, not a third-party renderer.
- Render source and output PDFs to images.
- Check page count and print scaling.
- Check Gantt bars, phase labels, grid lines, date headers, and today/reference
  line.
- For exact duplicates, pixel difference should be zero.
- For edits, visual differences should be localised to the intended changed
  tasks/dates/bars.

## Semantic Update Checks

- Timeline headers are parsed from the workbook, not hard-coded from memory.
- Date text and derived bar columns match.
- Old bar cells are cleared only for the target row.
- Existing bar style is captured before clearing.
- New rows use a captured style sample from a known-good bar.
- Added rows preserve row height, indentation, borders, and print fit.

## Handover Checks

- Output `.xlsx` exists.
- PDF preview exists.
- Visual comparison or visual inspection report exists.
- Known deviations are documented.

## From-Blank Reconstruction Checks

- Source and rebuilt workbook hashes differ when the task forbids direct copying.
- Excel can open the rebuilt workbook without repair prompts.
- The source workbook theme is preserved.
- Header/footer marks are present in the PDF.
- Drawing elements such as the red today line are present.
- The xlsx zip has no duplicate package members after patching.
- For exact reconstruction, rendered PDF pixel difference should be zero or
  explicitly explained.
