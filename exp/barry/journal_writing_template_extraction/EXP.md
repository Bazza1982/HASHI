# Journal Writing Template Extraction EXP

Status: candidate handover

This EXP trains agents to convert a real journal-style article into a reusable
writing template. The template should capture structure, paragraph function,
language moves, pacing, and section logic without copying the source content.

## Purpose

Given a sample journal article, produce a guide that helps Barry write a new
paper in a similar genre and structure.

The output should answer:

- What sections does the article use?
- What does each section accomplish?
- Roughly how many paragraphs does each section contain?
- What does each paragraph do rhetorically?
- How are research gaps, theory, method, evidence, synthesis, and contribution
  staged?
- What language style and paragraph rhythm does the article use?

## What this is not

- Not a content summary.
- Not a paraphrase of the source article.
- Not a formatting clone.
- Not a generic academic-writing skill.

## Training runs

```text
training_runs/aaaj_slr_template_001/RUN.md
training_runs/published_pdf_aaaj_template_001/RUN.md
```

The first run used Barry's AAAJ-style SLR draft and is useful as an adaptation
comparison, but the correct training source for this EXP is a published journal
article PDF. The second run starts that correction using the published
"Unpacking dialogic accounting" PDF.

## Current handover standard

The published-PDF run now has a v2 template that is strong enough for Barry's
review because it includes:

- a deep-reading gate before any Word output
- a paragraph-level role map instead of only section summaries
- a richer Markdown writing template
- a six-page Word template with drafting spaces and quality checks
- an AI prompt pack for repeated use by future agents
- a mini test-write on a different topic to confirm transferability

Do not mark this EXP as stable until Barry confirms the handover output is
intuitive and useful. Until then, treat it as candidate handover for AAAJ-style
SLR writing-template extraction.
