# NVivo 14 Round 3 Quick Coding Risks 003

Date: 2026-05-09
Status: completed
EXP target: `barry/nvivo_qualitative_coding_windows`

## Goal

Attempt a deeper round of sample-project interaction:

- reach the real detail view
- test Quick Coding / `Code to` behavior
- probe a minimal query or visualization route
- convert any failures into practical operating rules

## What was executed

1. Captured live screenshots directly from the NVivo window to inspect the true
   desktop state.
2. Opened `Overview of Sample Project`.
3. Observed and cancelled the modal `Waiting for printer connection...` popup.
4. Reached the detail view with selected text visible and the bottom
   quick-coding bar on screen.
5. Switched from `Read-Only` to `Editable`.
6. Attempted to use:
   - `CTRL+Q`
   - direct typing into the visible `Code to` area
   - a ribbon-based coding path
7. Recovered from document pollution by undo and then by force-reopening the
   sample project without saving.

## Validated findings

- Live screenshot capture is feasible and materially improves NVivo desktop
  validation in this environment.
- The sample overview file can open into a real readable detail view.
- The printer-connection popup is a repeatable blocker.
- `Read-Only` versus `Editable` status matters immediately for interaction.
- Blind quick-coding attempts are dangerous: expected code-name input can land in
  the document content instead.

## Result

The first Round 3 attempt did not yield a safe, successful disposable code
creation path, but it produced higher-value risk knowledge:

- do not trust `CTRL+Q` blindly
- do not trust visible quick-coding placeholders as proof of keyboard focus
- do not rely on partial undo as a clean recovery
- prefer no-save recovery after failed editable-document automation

## Retry Result

After fixing the default-printer blocker, quick coding was revisited and
successfully completed.

Validated successful path:

1. open `Overview of Sample Project`
2. keep source in `Read-Only`
3. create a real text selection with `CTRL+A`
4. verify the bottom `Code to` pane changes from disabled to enabled
5. click inside the bottom `Code to` pane
6. verify the focused element bounds sit inside the bottom input field
7. paste `Round3 Quick Coding Test`
8. press `Enter`
9. confirm the source row changes to `Codes: 1`, `References: 1`
10. open the `Codes` panel and confirm the new code appears with `Files: 1`,
    `References: 1`

The failure was procedural, not a hard tool limitation. Raw UI Automation alone
does not expose a friendly edit control for the quick-coding input, but
screenshot validation plus focused-element bounds are enough to operate it.

## Recommended next step

Round 4 should train on:

- code creation from the `Codes` panel first, before coding into source text
- targeted drag selection rather than broad `CTRL+A`
- coding only a short passage and then uncoding it
- screenshot checkpoints before every keyboard entry
