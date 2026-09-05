# Grammarly Browser Academic Revision EXP

Status: candidate

This EXP packages Barry's Grammarly browser-assisted academic-revision work as
context-specific operational knowledge. It replaces the long AIRMS SOP as the
active guide while preserving AIRMS as the confidential execution and research
record.

## Intent

Use Grammarly's live AI Detector sparingly to observe exact marked ranges and
provider explanations, revise the affected academic prose locally, then compare
the same bounded batch in the same browser document.

The working authority order is:

1. Barry's current instruction and approved external-review scope
2. the immutable source manuscript and its mapping ledger
3. this EXP's policy, playbooks, validators, and failure memory
4. the confidential AIRMS run record
5. Grammarly's provider observations

Grammarly is an external evaluator. Its score does not override evidence,
citations, meaning, grammatical quality, or Barry's voice.

## Current learned shape

The candidate route is:

1. freeze the selected DOCX and record its SHA-256
2. extract and map eligible prose locally
3. protect references, quotations, notes, tables, headings, and other excluded
   units
4. make paragraph-boundary batches of about 800–1,000 words
5. obtain explicit approval for the exact first upload unit
6. establish an exact R0 baseline in the logged-in Grammarly document
7. freeze marked ranges and verbatim explanations
8. draft and quality-check revisions locally
9. replace the complete batch in the same document only after the editor
   replacement path has passed a disposable preflight
10. run one formal reassessment and record target-level outcomes
11. write only validated changes to a named Word copy
12. reopen, compare, hash, and checkpoint before the next batch

The operational playbook is `playbooks/browser_local_revision.exp.md`. Adjustable
defaults are in `config/review_policy.json`.

## Current evidence

The 3 September 2026 Paper 4 run established:

- authenticated browser access and rendered-DOM extraction worked
- exact Grammarly ranges could be mapped to local manuscript blocks
- a bounded 927-word, eight-paragraph batch reconstructed exactly in the browser
- its R0 result was 87% with 18 marked parts
- six local R1 candidates were pre-registered and passed a preliminary academic
  quality gate
- the run stopped before a safe same-document R1 replacement and reassessment
- no Word output was created and no end-to-end rewrite success was established

It also produced valuable failure evidence: full-body paste duplication, a
misleading word counter, duplicate title controls, collapsed programmatic
selection, paragraph-only `Ctrl+A` behaviour, and the confound created by using a
second document for R1.

See `training_runs/paper4_batch001_poc_001/RUN.md` and
`failures/failure_memory.jsonl`.

## Production rule

For this EXP, “done” means one of:

- `clear`: the active batch reaches a stable 0% with no visible AI-pattern
  markers within the approved round limit
- `bounded_completion`: the round limit is reached, every unresolved exact
  range is preserved and annotated, and Word validation passes
- `safe_stop`: browser, mapping, explanation, replacement, evidence, or Word
  integrity is insufficient and no unverified change is applied

A safe stop is a valid operational result, but it does not count as a completed
training success.

## Confidentiality boundary

Do not copy exact manuscript passages, complete candidates, provider receipts,
screenshots, account details, or mapping ledgers into Git-tracked EXP files.
Those remain in the local AIRMS run directory. EXP keeps generalised lessons,
validation outcomes, failure signatures, and auditable pointers only.

## Promotion status

This EXP must remain `candidate` until a real bounded batch completes R0,
local revision, same-document R1 reassessment, exact Word write-back, and
validation.

Promotion to `stable` additionally requires repeated successful use in this
same context, recovery from at least one realistic failure, all applicable
validators passing, and Barry's explicit confirmation. The default evidence
thresholds are adjustable in `config/review_policy.json`; changing them must
be recorded rather than silently weakening the gate.

## Scope limit

Do not assume this EXP transfers to:

- another Grammarly interface or detector version
- another user account or machine
- another document editor
- generic authorship or misconduct decisions
- bulk manuscript upload without a new, exact approval
- in-browser live rewriting without local records and validation

Use `playbooks/train_and_promote.exp.md` when updating the EXP after a run.
