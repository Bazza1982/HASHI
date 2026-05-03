# EXP Training Program

EXP is not created by instruction alone. It becomes stable through repeated,
evidence-backed practice in a specific context.

Use this program when training a new EXP or upgrading a candidate EXP into a
stable one.

## Core idea

An EXP should be trained with:

- a clear goal
- real templates
- good and bad examples
- practice tasks
- evaluation criteria
- execution traces
- failure memory
- validation evidence

The output is not just a prompt. A stable EXP should include reusable procedure,
template fingerprints, known failure recovery, validators, and evidence.

## Lifecycle

```text
candidate -> practiced -> validated -> stable -> retired/updated
```

## Stages

### 1. Candidate

Create a candidate EXP when a context-specific trick, workflow, or preference
appears useful but has not been proven.

Record:

- target user and context
- intended task class
- initial assumptions
- known templates or examples
- first evidence, if any

### 2. Practiced

Practice with realistic tasks, not synthetic one-off instructions.

Training inputs should include:

- template files
- source data
- expected output examples
- bad examples to avoid
- clear task goals
- time and quality constraints

Keep execution traces:

- what the agent tried
- which helper tools were used
- where the UI or application behaved unexpectedly
- which shortcuts or procedures were reliable
- screenshots and output files

### 3. Validated

An EXP becomes validated only after its outputs pass explicit checks.

Validation should include:

- structural checks where possible
- visual inspection where layout matters
- real application open/save/export checks
- screenshots or logs as evidence
- comparison against the user-provided template or example

### 4. Stable

Promote an EXP to stable after repeated successful use in the same context.

A stable EXP must include:

- intent
- context
- procedure
- required inputs
- preferred tools
- failure memory
- validators
- evidence examples
- scope limits

### 5. Retired or updated

Retire or update an EXP when:

- the user's preference changes
- the template changes
- the application version changes
- helper-tool behavior changes
- repeated failures appear

## Training packet format

For a new EXP training run, provide:

```text
Goal:
  What should the agent learn to do well?

Context:
  User, machine, software, workflow, and constraints.

Templates:
  Files or paths the agent should use.

Examples:
  Good examples and bad examples.

Practice tasks:
  3-5 realistic variations.

Evaluation criteria:
  What counts as good enough?

Evidence:
  Where screenshots, output files, logs, and reports should be stored.
```

## Office example

For Office desktop EXP, a strong training packet might include:

- a PowerPoint council presentation template
- one excellent council deck
- one ugly or unacceptable deck
- a source briefing document
- target audience and presentation length
- requirements for notes, slide order, visual style, export, and rehearsal
- validation screenshots from editing view and slideshow mode

## Promotion rule

Do not promote a candidate EXP to stable just because the instructions sound
good. Promote it only when the agent has used it successfully, produced
evidence, and recovered from at least one realistic failure mode.

Before any handover, also follow the trainer-side checklist in `AGENT.md`.
That guide records cross-EXP review lessons, including the rule that visual
comparison alone is not enough for PowerPoint or document rebuilds.
