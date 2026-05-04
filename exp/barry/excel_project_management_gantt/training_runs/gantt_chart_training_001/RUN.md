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
