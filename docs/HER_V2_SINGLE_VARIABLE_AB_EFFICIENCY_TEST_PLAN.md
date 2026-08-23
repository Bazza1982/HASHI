# HER v2 Real-Workload Single-Variable A/B Efficiency Test Plan

| Field | Value |
|---|---|
| Status | Approved test design; implementation and deployment pending |
| Date | 2026-08-24 |
| Primary experiment environment | HASHI2 |
| Production reference | Current stable HASHI1 behaviour; not the statistical A arm unless environments are calibrated |
| Control | Last accepted HER v2 baseline |
| Treatment | Exactly one efficiency feature flag at a time |
| Workloads | Real recurring email work, real workbook work, and other eligible production assignments |
| Primary goals | Reduce execution latency, token consumption, and cost without reducing work quality, safety, or completion rate |

## 1. Purpose

This document defines a controlled A/B programme for improving HER v2 execution
efficiency using real work rather than invented benchmark tasks. The primary
evidence comes from recurring production workflows such as daily email review
and real workbook assignments.

Each experiment changes exactly one independent variable. The task, input,
configuration, provider, model, reasoning level, tools, context, permissions,
initial state, and validation rules remain the same between the A and B arms.

The programme measures three outcomes:

1. execution speed;
2. token consumption and monetary cost;
3. execution quality, safety, and practical usefulness.

Quality and safety are hard gates. An efficiency improvement cannot compensate
for missed work, an incorrect report, a wrong workbook change, a duplicated
external action, a lost tool, a media-routing regression, incomplete evidence,
or an untruthful terminal state.

## 2. Primary experimental principle

The experimental unit is one real work occurrence:

- one actual daily email window;
- one actual workbook request against the workbook state supplied for that job;
- or one other genuine user or scheduled task that is relevant to the variable
  under test.

For a valid paired observation, A and B receive the same immutable real input
and the same starting state. Both arms may produce candidate work in isolation,
but the real external side effect is committed at most once.

Synthetic fixtures remain useful for deterministic safety and contract checks.
They are not primary evidence for execution speed, token savings, cost savings,
or practical quality.

## 3. Non-goals

This programme does not:

- deploy every proposed optimisation together;
- treat fabricated tasks as proof of production efficiency;
- compare different models or providers inside one A/B experiment;
- compare HASHI1 directly with HASHI2 without controlling for machine and
  environment effects;
- impose a fixed tool-round, call-count, token, or elapsed-time ceiling on HER;
- weaken Tool Registry permissions, workzone enforcement, evidence, or audit
  rules;
- execute the same real email, account, scheduler, delivery, or file side effect
  once in A and again in B;
- replace deterministic contract tests with live model observations;
- activate changes on HASHI1 during the HASHI2 experiment;
- perform an automatic `/reboot` or any `/restart` operation.

## 4. Sequential single-variable model

The experiment series uses a promoted-baseline design:

```text
A_n = the last accepted version
B_n = A_n plus exactly one new feature flag
```

The decision rule is:

- if `B_n` passes every quality and safety gate and reaches its pre-registered
  efficiency target, it is promoted and becomes `A_(n+1)`;
- if `B_n` fails a hard gate, its feature flag is disabled and the baseline
  remains `A_n`;
- if the result is inconclusive, neither path is promoted until more eligible
  real work is observed or the experiment is redesigned;
- after all individual experiments, the full accepted stack is compared with
  the original `A_0` on real work to detect interaction effects.

No experiment may change a second optimisation while the first one is still
being measured.

## 5. HASHI2 experiment topology

HASHI2 hosts both code paths for the statistical comparison:

- A: feature flag disabled;
- B: the one treatment flag enabled.

Both paths must run under the same HASHI2 machine, runtime, configuration,
Provider route, model, permissions, and load controls. This avoids attributing a
HASHI1-versus-HASHI2 hardware, network, cache, or configuration difference to
the treatment.

HASHI1 remains the stable production reference and rollback source. It is not
used as the statistical A arm unless both instances first run the same baseline
and the treatment is swapped between machines in a crossover design.

Before the first treatment:

1. finish and validate the current local corrections;
2. freeze a clean common base revision;
3. snapshot the complete experiment configuration;
4. deploy `A_0` to HASHI2 with every efficiency treatment disabled;
5. run unscored warm-up work through both paths;
6. verify that A and B are behaviourally identical when all treatment flags are
   disabled;
7. validate telemetry without changing model-visible input or execution logic.

## 6. Experimental epochs and controlled fields

An epoch is a period in which all controlled inputs remain frozen. Every valid
pair in an epoch must record the same:

- base Git revision;
- dependency and runtime versions;
- Agent configuration and feature flags, except for the treatment flag;
- Provider, model, and Provider reasoning level;
- HER effort and route configuration;
- Agent Persona and applicable system instructions;
- Tool Registry permissions and tool options;
- exact task prompt or scheduled-job prompt version;
- exact supplied context and attachment manifest;
- immutable input snapshot hash;
- starting filesystem, database, mailbox-view, or workbook-state hash;
- validation recipes and acceptance rubric;
- live, snapshot, replay, or isolated-clone execution mode.

Any unrelated code, configuration, prompt, schema, model, dependency, or task
definition change ends the epoch. A new baseline must be measured before the
experiment continues.

Natural changes inside the real workload, such as the number of emails arriving
on a given day or the size of a workbook, do not end the epoch. They are workload
covariates and must be recorded for normalization and blocked analysis.

## 7. Real-workload registry

Before a production workflow participates in an experiment, register it once
with:

- workflow ID and owner;
- real trigger and normal schedule, if recurring;
- exact prompt or instruction source;
- input-capture boundary;
- starting-state snapshot procedure;
- expected output and validation recipe;
- all possible external side effects;
- the single-commit mechanism;
- workload-volume and complexity measures;
- privacy classification and retention rule;
- variables for which the workflow is relevant.

The registry prevents an experimenter from choosing easier tasks after seeing
results and makes workload changes visible.

## 8. Paired real-work execution protocol

### 8.1 Preferred method: capture once, execute twice, commit once

For each eligible real work occurrence:

1. capture the real input once at the normal task boundary;
2. create an immutable input manifest and content hash;
3. clone the exact starting state into isolated A and B workspaces;
4. run both arms against the same input and state;
5. alternate A-first and B-first execution in balanced blocks to control warm
   caches and temporal load;
6. validate both candidate outputs independently;
7. apply or deliver only the pre-assigned live arm;
8. retain the other arm as a shadow result for measurement and blinded review;
9. record any rescue, correction, rollback, or user follow-up.

The live arm is assigned before either output is observed. Assignment uses
balanced blocks such as `A-B-B-A` or `B-A-A-B`, randomized per block. This lets
both versions perform real work over time without duplicating a real side
effect.

### 8.2 Safety rescue

If the pre-assigned live candidate fails a deterministic quality or safety gate:

- do not commit it;
- mark the pair as a treatment failure rather than silently excluding it;
- use a validated control candidate or normal manual recovery to complete the
  real job once;
- record the rescue latency, extra tokens, and human intervention as treatment
  cost;
- never commit both candidates.

### 8.3 Fallback method: blocked crossover

Some real workflows cannot be snapshotted or shadow-executed safely. For those
workflows, execute only one arm per real occurrence and alternate arms in
balanced blocks across comparable occurrences.

The blocked-crossover record must include workload size, complexity, time of
day, network state, and other material covariates. These observations are less
controlled than paired snapshots and must be reported separately. They cannot
override contradictory paired evidence.

## 9. Primary real workload: daily email work

The email workload uses the actual recurring email jobs selected in the
experiment manifest, including the current Gmail morning workflow, Outlook
morning or noon workflow, and any associated reconciliation or reporting step
that is part of the real job. The experiment uses the production prompt,
accounts, policies, priority rules, history, and report format in force for that
epoch.

### 9.1 Input and state control

For each email work occurrence:

- capture the mailbox window once;
- freeze the message IDs, account/folder metadata, bodies, attachment manifests,
  truncation flags, and upstream capture diagnostics;
- clone the prior report/profile/state files needed by the job;
- give A and B the same frozen message set and starting state;
- prevent the shadow arm from sending, replying, labelling, archiving, deleting,
  updating a live profile, or delivering a duplicate report.

Mailbox ingestion time is a shared upstream measurement when capture occurs
once. It is reported as part of live end-to-end service time but excluded from
the controlled A/B execution delta unless ingestion itself is the isolated
treatment.

### 9.2 Email workload covariates

Record at least:

- mailbox and account count;
- total messages in the window;
- newly captured and previously captured message counts;
- unread and relevant-message counts;
- total body characters;
- attachment count, type, and total bytes;
- truncated or unavailable body count;
- number of required classifications, extracted actions, profile updates,
  drafts, and report items;
- low, medium, or high complexity bucket, assigned by a fixed rule before A/B
  outputs are reviewed.

### 9.3 Email efficiency metrics

Measure both totals and normalized values:

- controlled execution latency per window and per message;
- latency per relevant message and per attachment;
- input, output, thinking, and cached tokens per window and per message;
- Provider calls, tool calls, and tool rounds;
- message-body rereads and repeated searches;
- prompt-visible tool-result size;
- cost per window and per relevant message;
- time to the first useful report item and final approved report.

### 9.4 Email quality metrics

Evaluate:

- relevant-message recall and priority precision;
- correct account, sender, subject, date, deadline, and person attribution;
- action-item and attachment coverage;
- correct distinction between new capture count and full-window count;
- compliance with existing ignore, report-only, save, and escalation policies;
- absence of missed urgent items, fabricated details, duplicate entries, wrong
  recipients, or unauthorised mailbox mutations;
- report usefulness, concision, and required structure;
- user corrections, reversals, or missed-item reports within the defined
  observation window.

Deterministic checks cover IDs, counts, dates, attachments, state changes, and
policy invariants. A fixed blinded rubric covers relevance, prioritisation, and
summary usefulness. Disagreements and a pre-registered random sample receive
human review.

## 10. Primary real workload: workbook work

Workbook evidence comes only from genuine workbook assignments requested or
scheduled during the experiment. The exact user instruction, workbook, linked
files, required output format, and starting workbook state are captured as the
work unit. No invented spreadsheet is counted as efficiency evidence.

### 10.1 Input and state control

For each workbook assignment:

1. hash and preserve the canonical workbook before work starts;
2. create byte-identical A and B copies plus identical linked-input snapshots;
3. run the exact real instruction against both isolated copies;
4. recalculate and validate both candidates with the same toolchain;
5. compare structured workbook diffs and rendered output where formatting
   matters;
6. promote only the pre-assigned valid candidate to the canonical destination;
7. keep a recoverable pre-change backup and never merge A and B changes.

### 10.2 Workbook workload covariates

Record at least:

- file type and byte size;
- workbook and worksheet count;
- used-range dimensions;
- cells, rows, columns, tables, formulas, charts, and named ranges in scope;
- linked files or external data sources;
- requested value, formula, structure, formatting, or analysis operations;
- number of cells and objects expected to change;
- low, medium, or high complexity bucket assigned before output review.

### 10.3 Workbook efficiency metrics

Measure both totals and normalized values:

- controlled execution latency per workbook, sheet, and intended change;
- input, output, thinking, and cached tokens;
- Provider calls, tool calls, and tool rounds;
- range reads, repeated reads, write calls, and recalculation time;
- prompt-visible tool-result size;
- cost per assignment, sheet, and intended change;
- rescue, repair, and manual-correction time.

### 10.4 Workbook quality metrics

Evaluate:

- exact requested values, formulas, formats, and structural changes;
- formula validity and recalculation results;
- preservation of unrelated cells, sheets, styles, charts, hidden state,
  validation, names, and links;
- expected totals, reconciliations, and domain invariants;
- absence of corrupt files, broken references, unintended edits, or lost
  metadata;
- fidelity of exported or rendered output where visual layout matters;
- user acceptance, correction count, rollback, and time to a usable artifact.

## 11. Other real workloads

A treatment may be tested on another genuine recurring or user-requested
workflow when that workflow is affected by the change. Examples include actual
code maintenance, document editing, Web or browser research, daily wiki work,
and report generation.

Each additional workflow must first be entered in the real-workload registry
and use the same capture-once, isolated-execution, single-commit rules. A
treatment is not forced onto an unrelated workload merely to increase sample
count.

## 12. Role of deterministic and synthetic tests

Synthetic and deterministic tests run before live work only to verify:

- configuration and feature-flag isolation;
- permission and workzone boundaries;
- tool schemas and protocol compatibility;
- exact known regression cases;
- native multimodal routing and non-multimodal local fallback;
- cancellation, retry, lifecycle, evidence, and delivery invariants;
- clone, replay, and single-commit safety.

Their results are reported as pass/fail safety gates. Their latency and token
results are diagnostic only and are not used to claim production efficiency.

## 13. Treatment sequence and workload relevance

Each row is a separate experiment. No row may be combined with another until
its own decision is recorded.

| ID | Only changed variable | Primary real workload | Primary hypothesis |
|---|---|---|---|
| B1 | Accept the additional GPT/Codex patch dialect | Actual code and document-edit jobs | Patch-format failures and recovery loops decrease |
| B2 | Compute a dynamic tool palette in shadow mode while still sending all tools | Email, workbook, browser, and other tool-rich real work | Required-tool prediction reaches perfect recall before activation |
| B3 | Activate the validated dynamic tool palette | Same real workflows used for B2 | Tool-schema input shrinks without hiding a required tool |
| B4 | Prompt the model to batch independent read-only calls | Multi-message email review and multi-range or multi-sheet workbook work | Model/tool round count decreases |
| B5 | Execute an already-requested read-only batch concurrently | Read-heavy email, attachment, workbook, and research work | Tool wall time decreases without changing results or ordering semantics |
| B6 | Cache identical file or range slices while the content hash is unchanged | Repeated reads in real workbook, document, and code work | Duplicate read payload and model input decrease |
| B7 | Return bounded prompt-visible tool results with full audit retention | Long email, attachment, workbook, browser, and code chains | Tool-result context grows more slowly without hiding necessary evidence |
| B8 | Remove duplicate raw/parsed material from valid Simple Finalisation input | All eligible real work | Finalisation input and latency decrease |
| B9 | Route low-effort Simple Finalisation from Pro to the fixed Quick model | Real completed jobs with simple final reporting | Finalisation latency and cost decrease without quality loss |
| B10 | Combine Immediate Response and Triage into one typed fast gate | Actual direct user conversations | Direct requests use one Provider call and fewer tokens |
| B11 | Reuse or safely reference stable immutable context prefixes | Repeated daily email and other recurring jobs | Repeated stable-context input decreases |

Measurement instrumentation is a prerequisite shared by both arms, not an
efficiency treatment.

### 13.1 Shadow requirement for dynamic tool selection

`B2` changes no Provider-visible tool definitions. It computes and records the
palette that would have been sent, then compares it with the tools actually
needed by the unchanged real execution.

`B3` may begin only when `B2` demonstrates:

- 100% required-tool recall across every eligible observed real work unit;
- no loss of native or local-fallback media tools;
- no permission widening;
- deterministic palette records for identical inputs and configuration;
- no hidden dependency discovered by rescue or manual review.

## 14. Measurement prerequisite

Telemetry must be identical in both arms and must not influence model input,
tool selection, execution, delivery, or terminal-state logic. Before any active
treatment is evaluated, it must truthfully capture:

- per-stage and per-Provider-call latency;
- Provider call index and tool-loop index;
- input, output, thinking, and cached tokens when supplied by the Provider;
- tool-definition count, serialized characters, estimated tokens, and
  fingerprint for every Provider call;
- message characters divided into stable context, stage prompt, model output,
  tool calls, and tool results;
- tool-call count, tool-loop count, status, duration, and error type;
- cost line items and pricing-source version;
- lifecycle, retry, structured-output, delivery, and media-routing events;
- real-workload volume and complexity covariates;
- shadow/live assignment and whether a side effect was committed.

Telemetry must pass a no-behaviour-change gate on real shadow work before it is
used to measure a treatment.

## 15. Per-run record

Every arm emits one machine-readable record containing at least:

```json
{
  "experiment_id": "",
  "epoch_id": "",
  "pair_id": "",
  "arm": "A|B",
  "run_role": "LIVE_CANDIDATE|SHADOW",
  "run_order": 0,
  "treatment": "",
  "base_revision": "",
  "feature_flags": {},
  "config_sha256": "",
  "persona_sha256": "",
  "prompt_sha256": "",
  "input_manifest_sha256": "",
  "initial_state_sha256": "",
  "provider": "",
  "model": "",
  "reasoning": "",
  "her_effort": "",
  "workflow_id": "",
  "workload_type": "EMAIL|WORKBOOK|OTHER",
  "workload_volume": {},
  "complexity_bucket": "LOW|MEDIUM|HIGH",
  "shared_ingest_ms": 0,
  "metrics": {},
  "quality": {},
  "terminal_state": "",
  "side_effect_committed": false,
  "rescue_required": false,
  "decision": "PENDING"
}
```

A mismatch in any controlled-field hash invalidates the pair. A treatment-caused
failure, rescue, timeout, or malformed output remains a scored result and cannot
be discarded as an invalid pair.

## 16. Performance, consumption, and execution metrics

### 16.1 Latency

Measure:

- shared ingestion or snapshot time;
- controlled arm latency from frozen-input release to candidate completion;
- commit or delivery latency for the pre-assigned live arm;
- actual live end-to-end time from task trigger to final delivery;
- time to first useful output;
- Immediate Response, Triage, Execution, and Finalisation latency;
- Provider-call, tool-call, and read-only-batch latency;
- P50, P95, maximum, and interquartile range.

The controlled arm latency is the primary paired speed metric. Shared ingestion
is not subtracted from the real user experience; it is reported separately so
the experiment does not claim to improve a stage it did not change.

### 16.2 Tokens and cost

Measure:

- total and per-stage input tokens;
- total and per-stage output tokens;
- thinking tokens without double counting;
- cached tokens when reported;
- Provider call count;
- tool-schema characters and estimated tokens per Provider call;
- prompt-visible tool-result characters;
- total monetary cost and cost by stage/model;
- normalized token and cost values for the relevant real-work unit.

### 16.3 Execution behaviour

Measure:

- tool calls and tool rounds;
- successful, failed, denied, cancelled, and incomplete tool calls;
- repeated identical calls that add no new evidence;
- Provider retries and stage failures;
- structured-output repairs and compatibility recoveries;
- delivery attempts and final transport receipts;
- media-routing decisions and fallback transitions;
- rescue, rollback, and manual-intervention events.

## 17. Paired and normalized analysis

For every valid pair:

```text
token_improvement   = (A_tokens   - B_tokens)   / A_tokens
latency_improvement = (A_duration - B_duration) / A_duration
round_improvement   = (A_rounds   - B_rounds)   / A_rounds
cost_improvement    = (A_cost     - B_cost)     / A_cost
```

Aggregate reports include:

- paired median improvement;
- P50 and P95;
- interquartile range;
- proportion of pairs improved;
- paired bootstrap 95% confidence interval;
- best, worst, rescue, and outlier cases;
- results stratified by workflow and complexity bucket;
- normalized email and workbook metrics;
- separate cold and warm/cache-aware observations when applicable.

Arithmetic means may be reported, but never as the only statistic because a
small number of long tool loops or unusually large real jobs can dominate them.
Raw totals must never be compared without workload-volume normalization.

## 18. Evidence sufficiency and stopping rules

There is no fixed quota of invented tasks. Evidence accumulates only when real
eligible work occurs.

Before each treatment starts, pre-register its affected workflows, primary
metric, minimum meaningful improvement, non-inferiority margins, and minimum
evidence floor. Unless a treatment-specific rule justifies another floor, use:

- a five-pair real-work pilot for fault discovery; pilot evidence cannot by
  itself approve promotion;
- at least ten valid paired occurrences for each primary workflow family;
- at least ten distinct operating days for a daily recurring workflow;
- representation from every naturally occurring complexity bucket before
  making a broad claim;
- extension in balanced blocks of four observations while the result remains
  statistically or practically inconclusive.

Infrequent workbook work is never padded with fictional jobs. The experiment
waits for actual assignments, narrows its claim to the observed workbook task
types, or records `INCONCLUSIVE`.

Stop early only for a hard quality or safety failure. Do not stop early because
the first few efficiency results look favourable.

## 19. Hard quality and safety gates

The treatment stops immediately if it causes any of the following:

- a required tool is absent;
- an unauthorised tool is exposed or executed;
- an urgent or relevant email is missed because of the treatment;
- a wrong recipient, account, mailbox, file, workbook, sheet, or range is used;
- a wrong or extra file or workbook mutation occurs;
- an external side effect or user-visible report is duplicated;
- native multimodal input or local fallback regresses;
- evidence, audit, cancellation, lifecycle, or delivery semantics become
  incorrect;
- A completes the same real work from the same state and B does not;
- B hides a real failure or limitation;
- an open tool transaction is lost, replayed, or mismatched;
- the treatment introduces a fixed HER tool-loop or execution ceiling.

A hard-gate failure cannot be averaged away by speed, token, or cost savings.

## 20. Efficiency acceptance targets

| Treatment | Minimum target after every hard gate passes |
|---|---|
| B1 | Known supported patch-format failures become zero; affected real-job tool rounds decrease by at least 25% |
| B2 | Required-tool prediction recall is 100% before active filtering |
| B3 | Tool-schema size decreases by at least 85%; affected input tokens decrease by at least 20% |
| B4 | Affected real-job tool-round median decreases by at least 20% |
| B5 | Eligible read-only batch latency decreases by at least 20% |
| B6 | Duplicate read payload decreases by at least 80%; invalidation remains exact after mutation |
| B7 | Long-chain input tokens decrease by at least 20% |
| B8 | Simple Finalisation input tokens decrease by at least 30% |
| B9 | Simple Finalisation latency decreases by at least 25% with no quality loss |
| B10 | Direct Provider calls decrease from two to one and input tokens decrease by at least 40% |
| B11 | Repeated stable-context input decreases by at least 30% |

For every treatment:

- real-work completion and hard-gate success must be non-inferior to A;
- no primary workflow may conceal a material quality regression behind an
  aggregate gain from another workflow;
- total P95 latency must not worsen by more than 5%;
- Provider and structured-output failure rates must not increase;
- if the primary paired median improvement is below 10%, the additional
  complexity is rejected or held unless it provides a separate proven safety
  benefit;
- user correction, rescue, and rollback cost is included in the result.

## 21. Privacy and experiment-data handling

Real work may contain private email, attachments, workbook values, and personal
records. Experiment telemetry therefore stores:

- hashes, counts, type metadata, and aggregate sizes by default;
- redacted excerpts only when required to explain a quality decision;
- full content only inside the existing authorised work boundary;
- no new broad log of raw email bodies, workbook cells, credentials, or tokens;
- a retention and deletion rule recorded in the workload registry;
- access controls at least as strict as the source workflow.

Blinded review removes arm labels but does not weaken the data-access boundary.

## 22. Decision record and promotion

Every treatment produces:

1. its pre-registration record;
2. raw per-arm records and pair-validity checks;
3. aggregate and normalized metrics tables;
4. invalid-pair, rescue, and anomaly reports;
5. blinded quality-review results and delayed user-correction observations;
6. a decision of `ACCEPT`, `REJECT`, or `INCONCLUSIVE`;
7. the exact promoted or disabled feature configuration.

Decision meanings:

- `ACCEPT`: every hard gate passes, quality is non-inferior, and the primary
  real-work efficiency target is met with sufficient evidence;
- `REJECT`: a hard gate fails or the gain does not justify complexity and
  operational risk;
- `INCONCLUSIVE`: evidence is insufficient, environment control failed, real
  workload coverage is too narrow, or Provider variance prevents a reliable
  conclusion.

An accepted treatment becomes the next control baseline. A rejected or
inconclusive treatment remains disabled.

## 23. Cross-provider replication

The first epoch uses the fixed GPT configuration chosen for HASHI2. Once a
treatment is accepted there, any Provider-neutral claim must be repeated in a
new epoch using a fixed Gemini model through OpenRouter or another approved
multimodal Provider.

Changing Provider or model creates a new epoch; it is never mixed into the same
feature A/B comparison. The new epoch uses the same registered real workflows,
snapshot rules, metrics, privacy constraints, and quality gates. Native
multimodal input and local non-multimodal fallback are both preserved and
verified.

## 24. Final stack validation

After all individual decisions:

1. compare the complete accepted stack with original `A_0` on new eligible real
   email, workbook, and other affected work;
2. run deterministic interaction gates for treatments that modify adjacent
   boundaries;
3. confirm that combined gains are not materially below the measured
   incremental gains without explanation;
4. perform a limited HASHI2 production canary with balanced live-arm assignment
   and no duplicated side effects;
5. retain a rollback flag for every accepted feature until the canary closes;
6. promote beyond HASHI2 only under a separate explicit deployment decision.

## 25. Per-treatment operating checklist

Before starting:

- confirm the previous treatment is closed;
- select only real workflows affected by the new variable;
- freeze the epoch manifest and one treatment flag;
- pre-register metrics, quality gates, evidence floor, and stopping rules;
- validate snapshot, isolation, privacy, and single-commit controls;
- pass deterministic safety gates.

For every real work occurrence:

- capture one immutable real input;
- verify identical A/B starting hashes;
- record the pre-assigned live arm and execution order;
- run isolated A and B candidates;
- validate both;
- commit at most one result;
- record telemetry, quality, rescue, and user corrections.

After the evidence floor:

- compute paired and normalized results;
- inspect results by workflow and complexity;
- review all failures, rescues, and outliers;
- decide `ACCEPT`, `REJECT`, or `INCONCLUSIVE`;
- freeze the decision before starting another variable.

## 26. Final acceptance question

For every treatment, ask:

> On the same real work, with the same input snapshot, starting state,
> configuration, model, permissions, and validation rules, did the one changed
> variable measurably reduce latency, tokens, or cost while preserving the
> complete useful outcome, truthful terminal state, tool and media capability,
> evidence integrity, security boundary, and single user-visible delivery?

If the answer is not supported by paired real-work records and the hard gates,
the treatment is not accepted.
