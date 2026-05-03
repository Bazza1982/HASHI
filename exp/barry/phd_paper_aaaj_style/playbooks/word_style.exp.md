# Word Style EXP: Barry PhD Paper AAAJ

## Intent

Use Barry's Paper 1 AAAJ draft materials as training examples for future Word
document writing and formatting tasks in the same narrow style context.

## Current status

Stable for editing/rebuilding Barry's specific Paper 1 AAAJ draft from scratch
with the source template context. Still not stable as a general AAAJ academic
writing style for unrelated papers.

## Procedure for future agents

1. Read `manifest.json` and `EXP.md` first.
2. Inspect the training materials directory.
3. Identify the latest and strongest draft version before drawing conclusions.
4. Extract recurring style features:
   - title and author block format
   - abstract style
   - heading hierarchy
   - paragraph voice and density
   - citation and reference handling
   - table and figure captions
   - appendix or supplementary structure
   - journal-specific formatting
   - supervisor-preferred wording patterns
5. Record extracted rules as evidence-backed observations.
6. Test the rules on a small practice section before using them on a real paper.

## Required evidence

- source document names inspected
- extracted style notes
- screenshots or structural checks when formatting is important
- a practice output file if this EXP is used to generate or rewrite content

## Learned so far

- The primary target draft renders to a 66-page PDF in this environment.
- It contains 5 tables, 8 inline shapes, 7 media files, and 6 sections.
- Word object-model `FormattedText` insertion preserved object counts but failed
  layout validation by changing pagination from 66 to 80 pages.
- Starting from an empty Word document and using Word original-format insertion
  preserved the 66-page baseline but still failed page-level visual comparison.
- Exact visual baseline reproduction required creating the new document inside
  the source document's template context, clearing it, then rebuilding content.
- Page-count/PDF validation is required, but not sufficient; exact tasks need
  per-page rendered visual comparison.

## Recovery

- If style rules conflict across draft versions, prefer the newest complete
  draft unless Barry specifies otherwise.
- If journal requirements and supervisor preferences conflict, ask Barry before
  locking the rule into stable EXP.
- If a rule is inferred from one document only, mark it as tentative.

## Promotion criteria

This playbook is stable for the specific Paper 1 draft editing/rebuild context.
For broader AAAJ writing style generation, promote only after:

- style notes are extracted from multiple drafts
- a new sample section can be rewritten in the same style
- Barry confirms the output feels like the intended AAAJ/supervisor style
- validation checks are recorded
