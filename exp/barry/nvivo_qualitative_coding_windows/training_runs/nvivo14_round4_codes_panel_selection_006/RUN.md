# NVivo 14 Round 4 Codes Panel And Selection 006

Date: 2026-05-09
Status: completed
EXP target: `barry/nvivo_qualitative_coding_windows`

## Goal

Train the next layer after Quick Coding:

- create a code from the `Codes` panel/ribbon path
- create a short targeted text selection
- attempt to code the short selection to an existing code
- identify remaining blockers for uncode/recovery

## What Worked

### Create code from Codes panel

Validated path:

1. open the `Codes` area
2. click ribbon `Create`
3. click `Code`
4. fill the `New Code` dialog
5. confirm with `Enter`

Evidence:

- `Round4 Panel Code` appeared in the Codes list
- initial counts were `Files 0`, `References 0`

### Short text selection

Validated path:

1. keep source in `Read-Only`
2. click into the source text
3. send repeated `SHIFT+RIGHT`

Evidence:

- only a short passage at the beginning of the introduction was selected
- selection appeared as black highlight with inverted text

## What Did Not Work Reliably

### Bottom Code To token replacement

Attempting to replace `Round3 Quick Coding Test (Codes)` with
`Round4 Panel Code` inside the bottom `Code to` field did not behave like a
normal edit box. The pasted text appended to the existing token.

Lesson:

- do not use the bottom token field as a normal editable text box
- do not press `Enter` after a malformed appended token

### Drag selection to existing code row

Dragging the selected passage onto `Round4 Panel Code` did not increment the
target code counts.

Likely causes:

- selection drag did not carry data from the embedded document surface
- drop target hover was not confirmed
- a `Save Reminder` modal appeared during this phase and may have interrupted
  the workflow

## Remaining Work

Round 5 should validate one of these:

- exact drop target behavior for dragging a selected passage onto an existing
  code
- an alternate command path for coding current selection to a selected existing
  code
- uncode path after a small controlled reference has been created

## Current Practical Rule

Use:

- `Create -> Code` for code creation
- `CTRL+A` or `SHIFT+RIGHT` for reliable text selection
- Quick Coding only for creating/applying a new code to the current active
  selection

Do not yet rely on:

- token replacement in `Code to`
- drag-to-existing-code
- uncode automation without a small controlled reference
