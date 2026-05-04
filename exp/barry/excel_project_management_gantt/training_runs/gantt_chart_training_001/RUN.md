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

