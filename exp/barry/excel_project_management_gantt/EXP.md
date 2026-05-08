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

The second training step performed a controlled update:

- changed one phase date label
- moved a phase Gantt bar one month later
- moved one task Gantt bar one month later
- added one new checkpoint task and bar
- exported the edited workbook through Excel
- confirmed the output still fits 1 page wide by 1 page tall

This proves exact duplication and a controlled manual schedule edit. The EXP is
not yet stable for from-scratch creation.

The third training step performed a semantic date update:

- parsed the timeline headers in row 5
- corrected one inconsistent date range
- moved existing bars based on date text
- added one new task with a derived bar
- preserved page count and one-page print scaling
- passed rendered PDF visual comparison

This proves the EXP can now support assisted Gantt update work where bars are
derived from dates, provided visual QA is still performed before handover.

The fourth training step reconstructed the source workbook from a blank workbook
structure rather than copying the original file. It passed Excel PDF visual
comparison with zero rendered-pixel difference. This required copying not only
cell values and styles, but also the workbook theme palette, header/footer
labels, and the drawing connector used as the red today line.

## Operating Rule

For this EXP, treat the rendered PDF and Excel print layout as the first visual
truth. Excel grid appearance, merged ranges, row heights, column widths, fills,
and print scaling must be validated visually, not only through workbook cell
values.

When updating bars, preserve the Gantt's cell-based nature: bars are created by
cell fills, borders, alignment, and blank-space values rather than embedded chart
objects.

For semantic updates, derive bar positions from the timeline row before editing.
Capture a known-good bar style before clearing any existing bars. Do not use a
cell as a style source after it may have been cleared or overwritten in the same
editing pass.

For from-blank reconstruction, treat the xlsx as both a workbook and an Office
package. Cell content alone is not enough. Theme palette, drawings, VML,
headers/footers, print setup, and relationship files can affect the final PDF.
