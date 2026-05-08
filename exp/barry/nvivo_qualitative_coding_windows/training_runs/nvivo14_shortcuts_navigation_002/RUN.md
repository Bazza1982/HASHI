# NVivo 14 Shortcuts Navigation 002

Date: 2026-05-09
Status: completed
EXP target: `barry/nvivo_qualitative_coding_windows`

## Goal

Push round 2 training beyond launch/setup by validating practical navigation
 paths inside the sample project and by aligning Barry's guide with official
 Windows help details that affect coding, queries, and exports.

## What was executed

1. Kept the sample project open in desktop NVivo.
2. Queried the live UI for named controls after keyboard navigation changes.
3. Tested documented Windows shortcuts against the open project:
   - `CTRL+1`
   - `CTRL+2`
   - `CTRL+3`
   - `CTRL+4`
   - `CTRL+6`
   - `CTRL+7`
   - `F1`
   - `F5`
4. Cross-checked official help details for:
   - coding stripes
   - matrix coding queries
   - word frequency query limits
   - keyboard shortcuts
5. Updated the candidate EXP to reflect terminology drift, query persistence,
   and keyboard-first navigation.

## Validated desktop findings

- `CTRL+1` surfaced `Files`
- `CTRL+2` surfaced `Codes`
- `CTRL+3` surfaced `Cases`
- `CTRL+4` surfaced `Memos`
- `CTRL+6` surfaced `Maps`
- `CTRL+7` surfaced `Formatted Reports`
- `F1` opened `NVivo Help` in Chrome
- `F5` refreshed without breaking the current project session

## Documentation-derived findings added to EXP

- coding stripes cannot be shown while a file is in edit mode
- matrix query preview results should be stored explicitly if they need to
  persist
- scanned image-only PDFs need OCR before word-frequency analysis will be useful
- `CTRL+SHIFT+E` exports the selected item
- `CTRL+Q` moves focus between codable content and the Quick Coding bar

## Limits

- Deep coding in the detail view still was not fully automatable with raw UI
  control names alone
- The Queries area does not always expose a clean `Queries` label through UIA
  even when keyboard navigation changes the workspace
