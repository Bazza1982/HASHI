# Excel EXP

## Intent

Create and validate Excel analysis workbooks with formulas, summaries, filters,
conditional formatting, and charts.

## Context

Known to apply to Microsoft Excel on Barry's HASHI Windows desktop with
`use_computer`, `windows_helper`, and optional Office object automation.

## Procedure

1. Prefer clipboard paste for tables and multi-line data entry.
2. Build formulas explicitly and validate representative cells.
3. Use stable summary formulas such as `SUMIF` when testing regional rollups.
4. Use AutoFilter through `Alt+D,F,F` when UI filter toggling is part of the
   test.
5. Add charts after the data range is stable.
6. Validate workbook structure: formulas, AutoFilter range, conditional
   formatting count, chart count, and saved output path.

## Evidence to keep

- final `.xlsx`
- screenshot after data entry
- screenshot after chart creation
- validation notes or report JSON

## Recovery

- If typed data does not appear in cells, use clipboard paste into `A1`.
- If filter shortcut behavior is inconsistent, use `Alt+D,F,F`.
- If the UI chart creation is visually unstable, build the chart object through
  Office automation and validate in Excel UI.

## Scope limit

These formulas and shortcuts are evidence-backed in the HASHI Windows Office
context. Revalidate before using them as generic Excel behavior.
