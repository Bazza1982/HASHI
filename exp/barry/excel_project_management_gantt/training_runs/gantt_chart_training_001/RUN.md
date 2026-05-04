# Gantt Chart Training Run 001

Status: exact duplicate passed.

## Source

```text
training_materials/Gantt Chart training.xlsx
```

Original folder:

```text
C:\Users\Print\OneDrive - The University Of Newcastle\ai\Project management
```

## Output

```text
output/gantt_chart_training_duplicate_v1.xlsx
```

## Initial Extraction

```text
sheet: Project Timeline
used rows: 219
used columns: 33
charts: 0
images: 0
page setup: fit to 1 page wide by 1 page tall
```

The Gantt chart appears to be cell-based rather than an embedded Excel chart
object.

## Validation

The duplicate was created by exact workbook copy and validated with:

```text
state/workbook_structure_report.json
state/excel_export_report.json
state/duplicate_v1_visual_compare_report.json
```

Results:

```text
source SHA256 == duplicate SHA256
Excel PDF source pages: 1
Excel PDF duplicate pages: 1
max mean pixel difference: 0.0
max absolute pixel difference: 0
status: pass
```

Visual artifacts:

```text
artifacts/source_gantt_chart_training.pdf
artifacts/duplicate_gantt_chart_training_v1.pdf
artifacts/visual_compare_duplicate_v1/
```

## Next Training Step

Exact duplication is solved. The next training run should test a controlled
project update, such as changing one phase date range or adding one task, then
checking that the Gantt bars, row heights, formatting, and print layout remain
correct.

## Controlled Update Training

Output:

```text
output/gantt_chart_training_update_v1.xlsx
artifacts/gantt_chart_training_update_v1.pdf
```

Edits applied:

```text
C51: July 2025 to February 2026 -> August 2025 to March 2026
Row 51 Gantt bar: J:R -> K:S
Row 52 Gantt bar: L:N -> M:O
Row 203: added "6. Added checkpoint task for EXP update training"
Row 203 Gantt bar: AF:AG
```

Validation:

```text
Excel export status: pass
page count: 1
print scaling: 1 page wide by 1 page tall
visual diff status: pass
changed pixel ratio >8: 0.001376945167791364
max mean pixel difference: 0.08595430220739768
```

Reports:

```text
state/update_v1_edit_report.json
state/update_v1_excel_export_report.json
state/update_v1_visual_compare_report.json
```

Visual artifacts:

```text
artifacts/visual_compare_update_v1/
```

This proves the agent can perform a controlled manual schedule update while
preserving the workbook's one-page print layout. The EXP is still not stable:
future training should test deriving bars from dates and updating multiple
dependent tasks without manual cell-range selection.

## Semantic Date Update Training

Output:

```text
output/gantt_chart_training_semantic_update_v4.xlsx
artifacts/gantt_chart_training_semantic_update_v4.pdf
```

Purpose:

```text
derive Gantt bar columns from date text and timeline headers, instead of manually
choosing cell ranges
```

Edits applied:

```text
Row 60: August 2026 to June 2026 -> August 2025 to June 2026
Row 60 derived bar: K60:U60
Row 67: March 2026 to June 2026 -> April 2026 to July 2026
Row 67 derived bar: S67:V67
Row 203: added "6. Added semantic checkpoint task"
Row 203 derived bar: AF203:AG203
```

Validation:

```text
Excel export status: pass
page count: 1
print scaling: 1 page wide by 1 page tall
visual diff status: pass
changed pixel ratio >8: 0.0011253243347273993
max mean pixel difference: 0.08379403323820178
```

Reports:

```text
state/semantic_update_v4_report.json
state/semantic_update_v4_excel_export_report.json
state/semantic_update_v4_visual_compare_report.json
```

Visual artifacts:

```text
artifacts/visual_compare_semantic_update_v4/
```

Key lessons:

- Map row 5 timeline headers to columns before placing bars.
- Copy source bar style before clearing an old bar range.
- For new rows, use a captured style sample from a known existing bar, not a
  cell that may be cleared by an earlier update in the same pass.
- Validate both cell fills and Excel-rendered PDF output.

This proves the agent can perform a date-driven semantic Gantt update and
preserve the one-page visual layout. The EXP is now useful for assisted
project-chart editing, but should remain in training until Barry reviews an
edited workbook or asks to stabilise it.

## From-Blank Workbook Reconstruction Training

Final passing output:

```text
output/gantt_chart_training_rebuild_v5.xlsx
artifacts/gantt_chart_training_rebuild_v5.pdf
```

Purpose:

```text
recreate the workbook from a blank Excel file structure, not by byte-for-byte
copying the original xlsx
```

Method:

```text
1. Create a new workbook.
2. Rebuild the Project Timeline sheet.
3. Copy cell values, fonts, fills, borders, alignment, row heights, column
   widths, merged ranges, print setup, page margins, and print scaling.
4. Copy the workbook theme palette so theme-based bar colours render correctly.
5. Add the non-cell visual elements that openpyxl does not preserve by default:
   header/footer OFFICIAL labels and the red today-line drawing.
6. Rewrite the xlsx package as a clean zip, with no duplicate XML members.
```

Validation:

```text
Excel export status: pass
page count: 1
visual diff status: pass
changed pixel ratio >8: 0.0
max mean pixel difference: 0.0
```

Reports:

```text
state/rebuild_v1_report.json
state/rebuild_v2_report.json
state/rebuild_v5_package_patch_report.json
state/rebuild_v5_excel_export_report.json
state/rebuild_v5_visual_compare_report.json
```

Visual artifacts:

```text
artifacts/visual_compare_rebuild_v5/
```

Key lessons:

- Do not copy openpyxl internal `_style` ids between workbooks; copy style
  objects such as font, fill, border, alignment, number format, and protection.
- Theme colours require the source workbook's theme palette; otherwise the Gantt
  bars can render with the wrong colours.
- The red today line is a drawing connector, not a cell border.
- The OFFICIAL marks are header/footer content, not worksheet cells.
- When patching xlsx package parts, rewrite a clean zip. Appending replacement
  XML creates duplicate members and can make Excel refuse to open the workbook.

This proves the EXP can exactly reconstruct this source chart from a blank
workbook structure and reach PDF visual parity with zero rendered-pixel
difference.
