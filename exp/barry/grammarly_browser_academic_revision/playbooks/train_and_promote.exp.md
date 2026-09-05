# Train and Promote the Grammarly Revision EXP

Use this playbook after each practice or production run. The purpose is to make
the EXP more reliable without turning one observation into a universal rule.

## Evidence boundary

Keep exact manuscript prose, complete candidates, provider receipts, account
URLs, screenshots, and mapping ledgers in the confidential AIRMS run folder.

Commit only:

- generalised operational lessons
- failure signatures and recoveries
- validator changes
- de-identified target-level counts
- provenance pointers
- lifecycle decisions

## Training loop

1. Freeze the source and run artefacts.
2. Complete the validation report, including failed and pending checks.
3. Reconcile the run summary, batch state, attempt ledger, and evidence index.
4. Identify one specific failure or uncertainty.
5. Update the smallest correct EXP component:
   - procedure problem -> playbook
   - invariant/default problem -> policy
   - missing check -> validator
   - recurring operational fault -> failure memory
   - observed rewrite result -> strategy register
6. Run the relevant tests.
7. Execute the next bounded practice task.
8. Compare against the prior evidence without changing multiple uncontrolled
   variables when avoidable.
9. Repeat until the lifecycle gate is met.

Do not rewrite history. Version the EXP and append dated evidence.

## Required next training sequence

### Training 002 — Disposable same-editor replacement

Goal: prove a safe complete-editor replacement path in the current Grammarly
Coda editor.

- use synthetic, non-sensitive, separately approved text
- include at least three distinct paragraphs
- verify before/after canonical hashes and paragraph order
- reload after autosave and verify again
- demonstrate bounded recovery from one failed selection attempt
- do not use a real manuscript until this passes

### Training 003 — One complete real batch

Goal: complete one 800–1,000-word real batch through:

- exact R0
- complete finding freeze
- local revision and quality gate
- same-document R1 or later bounded reassessment
- target-level outcomes
- exact Word write-back
- real Word reopen and protected-content validation
- user review

### Training 004 — Independent transfer and recovery

Goal: repeat on an independent batch and exercise a realistic recovery path
without repeating external requests or corrupting a prior checkpoint.

## Lifecycle gates

### Candidate

The current status. Procedure and evidence exist, but end-to-end production use
has not passed.

### Practiced

Default gate:

- disposable whole-editor replacement passed
- at least one real batch completed end to end
- all artefacts reconciled
- no source overwrite
- validator report complete

### Validated

Default gate:

- at least three completed batches
- at least two distinct source documents
- target-level outcomes and failed candidates retained
- exact Word write-back passed
- no unresolved high-severity integrity validator

Barry may adjust these thresholds. Record the change and rationale in the
manifest history; do not silently lower them.

### Candidate handover

Use when the validated package is ready for Barry to inspect but he has not yet
confirmed it is intuitive and safe enough.

### Stable

Requires:

- repeated successful use in this exact context
- at least one realistic failure recovered without losing verified work
- every applicable validator passing
- clear scope limits and active failure memory
- Barry's explicit confirmation

## Strategy evidence rule

For every tested intervention, count:

- stable clear
- tentative clear
- persisted or narrowed
- shifted, reappeared, or worsened
- indeterminate
- academic-quality rejection

One clear remains a single observation. Promote a tactic to repeated support
only after independent, academically acceptable evidence. Keep counterexamples
beside successes.

## Version rule

Increment the manifest version when a change alters:

- required procedure
- data/approval boundary
- output contract
- validator gate
- failure recovery
- lifecycle threshold

Minor prose clarification without behavioural impact may retain the version but
must still appear in the relevant training run or change record.
