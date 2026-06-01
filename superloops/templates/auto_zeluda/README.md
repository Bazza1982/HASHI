# Auto Zeluda Superloop Template

## Purpose

Run an iterative qualitative coding loop for Zeluda-based research projects.

This template is for theory-led or abductive qualitative coding where the goal
is not merely to produce coded rows, but to move from codebook design through
coding, review, analysis, memo writing, codebook refinement, and repeated
coding rounds until there is enough well-supported empirical insight to begin
paper writing.

## Standard Roles

- `orchestrator`: Sakura. Owns the run charter, research question, theory
  alignment, coding scope, round decisions, sufficiency gate, and closeout.
- `coding_agent`: Yuhuan. Owns codebook generation, episode extraction,
  first-pass coding, revised coding, evidence table construction, and analytic
  memos assigned by the orchestrator.
- `reviewer`: Momo. Independently reviews the codebook, coded excerpts,
  evidence quality, theme support, negative cases, and sufficiency for writing.
- `human`: Barry. Owns research direction, final theoretical judgement, and
  approval to move from coding into manuscript writing.

The coding agent codes. The reviewer challenges the coding. The orchestrator
decides whether the loop continues, pauses, refines the codebook, or exits.

## Inputs

- Research question and sub-questions.
- Theory-led coding guide.
- Method section or method memo.
- Dataset manifest and source folders.
- Coding unit definition.
- Required output format for Zeluda/import/export.
- Round target: pilot, round 1, round 2, round 3, or final sufficiency review.
- Exit condition: enough high-quality evidence and themes to start writing.

## Default Role Assignment

```text
orchestrator: sakura
coding_agent: yuhuan
reviewer: momo
human: Barry
```

## Core Principle

The loop should code emotionally meaningful research episodes rather than
isolated words or single sentences unless the project explicitly requires
line-level coding.

Every coded claim should preserve:

- source file
- source type
- date, if available
- agent/persona, if available
- task context
- excerpt
- code assignments
- analytic memo
- reviewer status
- evidence strength

## Non-Negotiable Gates

### G1 Research Charter

Before coding begins, the orchestrator must record:

- research question
- theoretical framework
- dataset boundary
- coding unit
- round count target
- sufficiency criteria
- what is in scope and out of scope

### G2 Source Integrity

Before coding begins, the coding agent must confirm:

- input folders exist
- file manifest exists or is generated
- source types are distinguishable
- generated summaries are separated from direct/near-direct excerpts
- no source files will be edited in place

### G3 Codebook Generation

The coding agent generates an initial codebook from:

- the theory-led guide
- method section
- initial empirical reading memo
- a pilot sample of the data

The codebook must define:

- parent codes
- child codes
- inclusion criteria
- exclusion criteria
- examples
- ambiguous boundary notes

### G4 Pilot Coding

Pilot coding must be bounded. It should code a deliberately selected sample of
high-yield material before full coding begins.

Default pilot scope:

```text
generated_topics_relevant/AI_Agent_Failure_Analysis.md
generated_topics_relevant/HASHI_Superloop.md
generated_topics_relevant/Anatta_Emotional_Intelligence.md
```

Pilot output must include:

- coded episode table
- sample distribution by parent code
- weak/unclear code notes
- suggested codebook refinements

### G5 Independent Review

The reviewer must inspect actual coded excerpts and source context, not only a
summary.

Reviewer checks:

- theory-code fit
- source/excerpt traceability
- coding unit consistency
- overcoding or undercoding
- unsupported interpretations
- missing negative cases
- theme evidence strength
- whether generated summaries are being overused as primary evidence

Findings must be classified as:

- blocker
- non-blocker
- follow-up

### G6 Codebook Refinement

After each review, the orchestrator decides whether to:

- accept the codebook
- merge/split codes
- clarify definitions
- add negative-case codes
- narrow or expand the dataset
- run another coding round

The reviewer advises; the orchestrator decides.

### G7 Iterative Coding Rounds

The standard loop allows 2-3 rounds by default:

```text
Round 0: codebook generation and pilot coding
Round 1: focused coding of high-density generated topics and linked Daily context
Round 2: expanded coding across Daily chronological cases
Round 3: optional saturation / negative-case / gap-filling round
```

The loop may stop earlier if the reviewer confirms sufficient insight and
evidence. It may continue only with a clear reason and human approval if more
than three rounds are needed.

### G8 Analysis And Memo Writing

After each coding round, the coding agent writes an analytic memo covering:

- strongest emerging themes
- evidence quality
- representative excerpts
- contradictions or negative cases
- implications for the research question
- changes needed to the codebook
- readiness for writing

### G9 Sufficiency Gate

Before exiting into paper writing, the reviewer must assess whether the current
evidence supports strong findings.

Sufficiency requires:

- stable codebook
- traceable coded evidence
- multiple strong episodes for each major theme
- representative excerpts
- negative or boundary cases
- clear analytic memos
- no unresolved reviewer blockers
- no major uncoded dataset area that could overturn the current findings

### G10 Closeout

Final closeout must include:

- final codebook path
- coded dataset path
- review report path
- analysis memo path
- theme/evidence matrix path
- unresolved limitations
- reviewer sufficiency judgement
- orchestrator exit decision
- recommendation for manuscript writing

## Standard Loop

1. Capture research question, theory, dataset, and exit criteria.
2. Create taskboard and evidence log.
3. Generate or confirm source/data manifest.
4. Generate initial theory-led codebook.
5. Run pilot coding.
6. Reviewer reviews pilot codebook and coded sample.
7. Refine codebook.
8. Run coding round 1.
9. Write analytic memo 1.
10. Reviewer reviews round 1.
11. Refine codebook and sampling plan.
12. Run coding round 2.
13. Write analytic memo 2 and theme/evidence matrix.
14. Reviewer assesses sufficiency.
15. Optional round 3 for negative cases or weak themes.
16. Final sufficiency decision and handoff to paper writing.

## Anti-Patterns

- Treating keyword hits as qualitative coding.
- Coding isolated affect words without the surrounding work episode.
- Treating generated topic pages as raw transcripts without source status.
- Expanding to the whole corpus before the codebook is stable.
- Producing themes without representative excerpts.
- Letting the coding agent review its own coding.
- Letting the reviewer decide loop exit.
- Closing before negative cases and weak themes are considered.
- Claiming saturation merely because a lot of text was processed.
- Writing findings before the theme/evidence matrix is strong enough.

## Default Exit Condition

Exit when the reviewer confirms that the coded evidence and analytic memos are
sufficient to support a coherent findings section and the orchestrator records
that the project is ready to move into manuscript writing.
