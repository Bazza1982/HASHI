# EXP Trainer Guide

This file is for future agents continuing EXP training.

Read this before starting or handing over any EXP training artefact. EXP
training is not finished when an output looks good at first glance. Training is
finished only when the artefact passes the validators that match the task.

## Trainer Role

When training an EXP, the agent is responsible for:

- preserving the source materials in the EXP training area
- recording what was attempted
- recording what failed and why
- producing the final artefact
- validating the final artefact with task-specific checks
- updating failure memory so later agents do not repeat the same mistake

Do not rely on memory alone. If a mistake happens, write it down in the EXP.

## Standard Training Loop

Use this loop for every substantial EXP:

1. Save the source material.
2. Extract structure and object/content fingerprints.
3. Produce a first practice artefact.
4. Compare it against the source or expected output.
5. Identify the specific failure mode.
6. Update the playbook, validator, or failure memory.
7. Produce the next practice artefact.
8. Repeat until the artefact passes both automated checks and human-review
   readiness checks.

## Final Artefact Review Rule

Before handover, run the strongest relevant checks available. Do not stop at
one type of check.

For document or Office artefacts, combine:

- visual comparison
- structural comparison
- text/content parity
- object/count parity
- open/save/export validation in the real application
- manual screenshot or PDF inspection when the output is visual

If visual layout matters, export the artefact to PDF or images before review.

## PowerPoint-Specific Lessons

The MoH data platform PowerPoint training exposed an important failure:

visual-difference thresholds can miss small text objects.

In that run, a rebuilt deck looked acceptable and passed image-difference
thresholds, but it missed small text such as a section number (`03`) and a cover
date. The failure happened because those objects occupied a tiny part of the
slide, so average image-difference metrics stayed low.

Therefore, PowerPoint rebuild training must include:

- slide-image visual comparison
- text-shape parity against the source deck
- slide count parity
- table/object presence checks
- manual inspection of section numbers, dates, footers, and small labels

Do not mark a PowerPoint rebuild as ready for review if text parity has not
passed.

## Template-Context Rule

PowerPoint and Word exact-style rebuilds often require template context.

Pure white-page or white-slide reconstruction is useful for learning positions,
but it can miss hidden master/layout/style behaviour. For exact recurring
Office templates, the production route should usually be:

1. use the source as template context
2. preserve master/layout/theme/style assets
3. clear and rebuild editable content
4. retain complex visual assets when native rebuilding changes rendering
5. validate against the source

Record clearly which assets were rebuilt and which were retained as template
assets.

## Failure Memory Rule

Every meaningful training failure must be written into:

```text
<exp-domain>/failures/failure_memory.jsonl
```

Good failure memory states:

- what failed
- why it failed
- how it was detected
- what future agents must do differently

## Handover Rule

Use `candidate_handover` when the trainer thinks the artefact is ready for
Barry to inspect.

Use `stable` only after Barry confirms the output is good enough and the EXP has
clear scope limits, validators, and failure memory.
