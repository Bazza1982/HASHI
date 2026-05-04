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

## Handover Checks

- Output `.xlsx` exists.
- PDF preview exists.
- Visual comparison or visual inspection report exists.
- Known deviations are documented.

