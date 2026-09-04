# Paper 4 Grammarly Batch 001 Proof of Concept

Date: 2026-09-03

Status: stopped before R1 reassessment; candidate evidence only

EXP target: `barry/grammarly_browser_academic_revision`

## Goal

Convert the emerging Grammarly browser procedure into a bounded proof of
concept and determine whether exact browser detection, local academic revision,
same-context reassessment, and Word write-back could be executed safely.

## Confidential evidence root

Exact manuscript text and browser evidence remain outside Git:

`text
C:\Users\thene\projects\UON_PhD\Barry's PhD\09_other\AI\AIRMS\reports\grammarly_browser\runs\paper4_grammarly_20260903T113531_AEST
`

This training record contains metrics and lessons only.

## Frozen source

- Material: Paper 4 QRAM manuscript
- Source SHA-256:
  `ba95852ddb5c7277e1f83808ef2a175b24df76d192d858b2c16ad177764fd21e`
- Eligible body: 101 paragraphs, 10,126 words
- Local-only Notes: 5 paragraphs, 275 words
- Source overwritten: no
- Notes uploaded: no

## What was executed

1. Verified the logged-in Grammarly Docs browser session.
2. Extracted and mapped the source locally.
3. Corrected external scope from proposed body-plus-Notes to body only before
   Notes were uploaded.
4. Observed and recovered from a duplicated whole-body paste.
5. Froze a repaired whole-body result for historical evidence.
6. Pivoted to paragraph-boundary batching.
7. Built Batch 001 from 8 complete mapped paragraphs:
   - R0: 927 words
   - exact browser/local canonical SHA match
   - 8 non-empty browser rows in correct order
   - R0: 87%, 18 marked parts
8. Pre-registered six local R1 candidates:
   - R1: 942 words
   - citations, years, other numbers, paragraph order, and protected terms
     passed the local preliminary gate
9. Stopped before R1 detector reassessment because safe same-document
   whole-editor replacement had not been proven.
10. Created no Word output.

## Failures observed

- Whole-body paste duplicated rendered content.
- The word counter was not sufficient to detect the duplication.
- A title action reached an off-canvas duplicate control.
- Programmatic selection collapsed when the typing helper inserted text.
- Native `Ctrl+A` selected one paragraph, not the whole Coda editor.
- A proposed paired R1 document would have introduced a document-level
  comparison confound.
- Explanation harvesting scaled to 19 items before one correction loop was
  proven.
- Top-level run records later drifted behind the newer batch artefacts.

All are represented in `../../failures/failure_memory.jsonl`.

## Validation outcome

Passed:

- immutable-source check
- body-only scope and Notes protection
- local mapping
- Batch 001 paragraph-boundary construction
- exact R0 browser reconstruction
- frozen R0 evidence
- preliminary local R1 academic-quality checks

Not passed or not run:

- disposable whole-editor replacement preflight
- same-document R1 insertion
- R1 detector reassessment
- target-level rewrite outcomes
- final batch extraction
- Word write-back
- real Word reopen/save
- user acceptance

Machine-readable result:

`state/validation_report.json`

## Learning applied to the EXP

- Browser work is now limited to sparse detection and complete-batch
  replacement/recheck; revision remains local.
- Full-editor replacement is a mandatory preflight gate.
- Browser text integrity requires reconstruction and hashes, not word count.
- Paired documents are a declared experimental amendment, not a silent fallback.
- Run records must reconcile at every checkpoint.
- The EXP remains `candidate` and carries no rewrite-efficacy claim.

## Next training action

Run a separately authorised disposable, synthetic three-paragraph editor
replacement test. Only after that succeeds should one real bounded batch
complete R0, local revision, same-document R1, exact Word write-back, and all
validators.
