# Gantt Chart Update Playbook

Use this playbook when Barry asks to edit project dates, tasks, phases, owners,
or Gantt bars.

## Working Pattern

1. Inspect the workbook structure before editing.
2. Identify whether the Gantt bars are formula-based, fill-based, or manual
   cell formatting.
3. Preserve row grouping, merged ranges, row heights, column widths, and print
   scaling.
4. Make edits in a copy first.
5. Export the edited workbook to PDF.
6. Render the PDF and inspect the timeline visually.
7. Compare against the previous version when the edit should be localised.

## Date-Driven Bar Updates

When the date text drives the Gantt bar:

1. Parse the timeline header row into month-to-column mappings.
2. Parse task or phase date text into start and end months.
3. Capture the existing bar style before clearing old bars.
4. Clear only the old bar cells for the target row.
5. Apply the captured style to the derived month columns.
6. For a new row, use a captured known-good style sample from an existing bar,
   not a cell that may have been modified earlier in the same update.
7. Export to PDF and confirm changes are localised.

## Current Capability

This EXP has passed exact duplication, controlled manual update, and semantic
date-driven update training. It is suitable for assisted editing with visual QA,
but not yet stable for fully independent from-scratch project chart creation.
