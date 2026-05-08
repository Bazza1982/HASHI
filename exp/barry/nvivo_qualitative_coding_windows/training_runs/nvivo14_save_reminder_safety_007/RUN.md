# NVivo 14 Save Reminder Safety 007

Date: 2026-05-09
Status: completed
EXP target: `barry/nvivo_qualitative_coding_windows`

## Trigger

Barry pointed out that NVivo's `Save Reminder` should be treated as useful
project-safety guidance, not just an interruption. Legacy desktop software can
crash easily, especially with large qualitative projects and heavy files.

## Rule Added

For real NVivo work:

1. Save when `Save Reminder` appears.
2. Save after meaningful coding batches, memo writing, import work, or query
   outputs.
3. If NVivo crashes, hangs, or disappears, pause the workflow.
4. Check whether `NVivo.exe` is still running.
5. Reopen the project from the expected local path.
6. Confirm recent sources, codes, references, memos, and outputs are still
   present before continuing.

## Exception

During deliberately disposable sample-project experiments, it is acceptable to
choose not to save if preserving the sample state is more important than keeping
the experiment changes.

## EXP Updates

- `EXP.md`
- `playbooks/qualitative_coding.exp.md`
- `validators/nvivo_desktop_validators.md`
- `failures/failure_memory.jsonl`
