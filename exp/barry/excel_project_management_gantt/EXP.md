# Excel Project Management Gantt EXP

Status: training

This EXP trains agents to create, edit, and update Barry's Excel project
management Gantt charts.

The first training material is:

```text
training_runs/gantt_chart_training_001/training_materials/Gantt Chart training.xlsx
```

## Initial Learned Shape

The workbook contains one sheet:

```text
Project Timeline
```

Initial extraction shows:

- 219 used rows
- 33 used columns
- one printable page when exported through Excel
- no embedded chart object
- no embedded images
- Gantt visual is cell-based, using row structure, merged cells, fills, borders,
  and print layout
- page setup fits the sheet to 1 page wide by 1 page tall

## Training Status

Run `gantt_chart_training_001` created an exact duplicate by copying the workbook
and validating it through:

- SHA256 file equality
- Excel export to PDF
- rendered PDF visual comparison

The duplicate passed with zero rendered-pixel difference.

This only proves exact duplication of an existing workbook. The EXP is not yet
stable for from-scratch creation or semantic schedule updates.

## Operating Rule

For this EXP, treat the rendered PDF and Excel print layout as the first visual
truth. Excel grid appearance, merged ranges, row heights, column widths, fills,
and print scaling must be validated visually, not only through workbook cell
values.

