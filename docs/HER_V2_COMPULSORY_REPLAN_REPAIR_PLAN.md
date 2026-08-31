# HER v2 Compulsory Replanning Repair Plan

| Field | Value |
|---|---|
| Status | Implemented historically; dormant while higher-mode redesign is postponed |
| Date | 2026-08-24 |
| Authority | Historical internal contract; superseded for the public surface by `HER_V2_THREE_MODE_DECISION.md` |
| Scope | HER v2 work turns at Adaptive (`high`), Reviewed (`xhigh`), and Assured (`max`) execution modes |
| Fixed cadence | 300 monotonic seconds or 10 newly completed Execution tool results, whichever is observed first |
| Runtime activation | Targeted Arale hot reload passed on 2026-08-24; capability-level threshold canaries remain separate |

Adaptive, Reviewed, and Assured are no longer exposed by `/effort`. This file
continues to document and regression-test retained internal code only; it does
not define a current production choice.

## 1. Repair objective

Replace the incorrectly implemented optional high-risk checkpoint assessor with
the approved compulsory Replanning cycle. The cadence is not a safety judge and
does not choose whether Replanning should happen. Once either threshold is due,
HER v2 must enter its principal `REPLANNING` lifecycle state at the next safe
boundary, run the three required self-checks, activate a new plan version, send
one progress commentary, and then either resume Execution or stop adding work
when the goal is already complete.

This repair exists to keep the model aligned with the original user goal:

- do not stop before the authorised success criteria are met;
- do not continue adding work after they are met;
- do not keep following an approach invalidated by current evidence; and
- do not use Replanning to broaden the user's authority or replace the goal.

## 2. Locked functional contract

### 2.1 Eligibility

The compulsory cadence applies to every HER v2 work classification when the
selected execution mode is one of:

- Adaptive (`high`);
- Reviewed (`xhigh`); or
- Assured (`max`).

Direct (`zero`) never enters Triage or Execution and therefore cannot install
the cadence. Fast path (`low`) and Planned (`medium`) do not install it either.
Eligibility is determined only by HER execution mode.

The first cadence window starts only after the principal lifecycle enters
`EXECUTING`. Planning, Triage, Immediate Response, commentary packaging,
Review, Verification, and Finalisation do not count toward it.

### 2.2 Trigger and safe-boundary order

A Replan becomes due when either inclusive condition is first observed in the
current Execution window:

- 10 newly completed Tool Gateway results; or
- 300.0 monotonic seconds since that window began.

Successes, completed tool errors, and policy denials count once by exact receipt
identity. Starts, incomplete/cancelled results, duplicates, and non-Execution
activity do not count.

The due flag is not an execution timeout. It must never cancel a provider or an
active tool merely because time elapsed. HER waits for a safe boundary:

1. a completed tool result is durably preserved before Replanning and returned
   once afterward with the active Replan control data;
2. already-admitted parallel tools may settle;
3. no later tool is admitted once the due boundary is being taken; and
4. the primary workflow leaves Execution and enters `REPLANNING` exactly once
   for the coalesced due reasons.

If the provider completes Execution after a threshold became due without
requesting another tool, stage completion is itself the safe boundary. HER
must run the compulsory Replan before accepting that execution cycle as the
latest completion decision. Count and time becoming due together produce one
Replan, not two, and elapsed historical windows never create catch-up bursts.

After each completed Replan, the result counter and monotonic start are reset.
Provider recovery within the same interrupted Execution invocation must not
silently reset or evade a due window.

### 2.3 Mandatory lifecycle action

The required action chain is:

```text
EXECUTING
  -> threshold due at a safe boundary
  -> REPLANNING (mandatory; no CONTINUE/ASK/HALT checkpoint decision)
  -> activate plan version N+1
  -> publish exactly one Replan commentary event
  -> EXECUTING when completion is below 100%
     or EXECUTION_COMPLETED when completion is 100%
  -> start a fresh cadence window only when Execution resumes
```

The old tool-free `CHECKPOINT` evaluator, its `CONTINUE / USER_INPUT_REQUIRED /
HALT` decision, `may_replan=false`, and its no-commentary assertion contradict
this contract and must be removed from the active path and from tests that
bless it.

Explicit `/stop`, `/steer`, runtime cancellation, audit-persistence failure,
Tool Gateway permission/approval decisions, and genuine missing user authority
retain their existing typed paths. They are not cadence decisions and do not
wait for the next Replan.

### 2.4 Three mandatory Replanning questions

Every cadence-triggered Replanning response must answer and validate these
three questions as structured data.

1. **How complete is the original goal?**
   - Provide an approximate integer `completion_percent` from 0 through 100.
   - Provide a non-empty `completion_basis` tied to the original request,
     authority, success criteria, completed work, and current evidence.
   - Below 100 means work remains and Execution resumes.
   - Exactly 100 means HER stops adding work and proceeds to Review when the
     selected mode requires it, otherwise Finalisation.
2. **Is the active plan still appropriate?**
   - Provide `plan_changed` as a boolean.
   - Always provide a complete replacement-plan envelope for version N+1.
   - If current evidence changes the approach, `plan_changed=true` requires a
     concrete `change_reason` and the replacement steps must reflect it.
   - If nothing material changed, `plan_changed=false`, `change_reason` is
     empty, and the plan content remains semantically unchanged; the completed
     calibration still creates and activates version N+1.
   - Neither result may change the immutable goal, classification, authority,
     or success criteria.
3. **What update should be sent to the user now?**
   - The neutral commentary must state the current completion percentage,
     whether the plan changed, why when it changed, and the next action.
   - `next_step` is a required verified field used both in the model-authored
     message and in deterministic fallback rendering.

Habits are not re-read. Current execution evidence and the original user goal
remain authoritative.

### 2.5 Plan versions and no side-effect replay

Every successful compulsory Replan activates a new Ledger plan ID, including a
calibration whose plan content does not change. The old plan remains audit
history and may not be overwritten in place.

Every resumed Execution invocation receives:

- the newly active plan and plan version;
- the triggering checkpoint/Replan identity;
- completed receipt identities and bounded evidence summaries;
- completed-work and limitation context accumulated before the interruption;
- an explicit instruction to continue from current state and never repeat a
  completed side effect merely because a new provider invocation began.

The Tool Gateway remains the authority for permissions and idempotency. Runtime
must preserve exact completed receipts across the interruption, must not use
provider retry to replay the interrupted invocation, and must not present a
completed side effect as incomplete.

### 2.6 Mandatory commentary and exactly-once fallback

Each compulsory Replan has one stable checkpoint ID derived from the turn and
a monotonically increasing Replan serial. That identity is the idempotency key
for the commentary event and its audit records.

The successful structured Replanning result should contain neutral commentary.
If it is missing, empty, malformed, or cannot be extracted, Runtime constructs
a deterministic neutral message from only these validated fields:

- configured Agent display name at the Persona fallback boundary;
- `completion_percent`;
- `plan_changed`;
- validated `change_reason` when applicable; and
- `next_step`.

The neutral message then enters the same isolated Persona packaging boundary as
other commentary. A model omission or damaged commentary instructs that
boundary to use the existing deterministic minimal fallback directly. That
fallback prefixes the verified neutral facts with the already resolved Agent
display name; Runtime must not create a separate internal-name identity path.
If normal Persona packaging is unavailable, loses protected facts, or fails,
the same display-name fallback must deliver the verified facts. Event
reservation happens before packaging so model omission, packaging retry,
transport ambiguity, provider recovery, or replay cannot send the logical
Replan commentary more than once.

Commentary delivery remains presentation-only: failure is audited but may not
cancel, retry, reclassify, or otherwise change the Replanning/workflow result.
The Replan event is marked required for presentation and is not suppressed by
the ordinary optional-commentary display toggle.

## 3. Limit and liveness audit

The compulsory Replan cadence is an interval, not a maximum. Implementation
must contain no Replan-count ceiling and must not let cumulative periodic
Replans exhaust Review or Verification remediation eligibility.

No Replanning invocation or whole HER workflow may be ended by an unauthorised:

- absolute/stage/provider elapsed timeout;
- total wall-clock or time budget;
- token or output-token budget;
- turn, step, call, loop, tool-round, or Replan-count limit; or
- catch-up/failure rule derived from how many cadence windows elapsed.

The following existing controls remain allowed only in their narrow scopes and
must be proven not to act as a Replan/workflow ceiling:

- explicit user `/stop` or `/steer` and ordinary task cancellation;
- the configured meaningful-progress idle detector used only to stop endless
  structured-envelope repair, not a healthy Replanning provider operation;
- one typed fresh-connection recovery when replay is proven safe;
- transport inactivity/protocol guards scoped below the complete provider/tool
  workflow;
- an explicitly requested timeout on one tool invocation;
- fixed Review and at-most-three Verification attempts selected by Reviewed or
  Assured mode; and
- permission, approval, audit-integrity, and process-cleanup safety controls.

Legacy `replan_limits` must not cap compulsory Replanning. The implementation
should remove that runtime authority and reject or safely ignore legacy limit
configuration rather than letting it terminate a valid long-running workflow.

## 4. Test design before implementation

### 4.1 Structured Replanning contract

Add parser tests proving:

- `completion_percent` accepts only integers from 0 through 100;
- `completion_basis`, `plan_changed`, `next_step`, plan steps, and success
  criteria are required;
- a changed plan requires a non-empty reason;
- an unchanged plan rejects a fabricated change reason and preserves semantic
  plan content;
- missing commentary does not invalidate the Replan because deterministic
  fallback is mandatory; and
- goal/classification/authority cannot be supplied as replacement fields.

### 4.2 Cadence coordinator

Use an injected monotonic clock and barriers to prove:

- 9 results and 299.999 seconds are not due;
- result 10 and exactly 300.0 seconds are due;
- count/time coalesce into one stable checkpoint ID;
- the triggering result is preserved before Replanning and returned exactly
  once with the resulting control data;
- no 11th tool is admitted first;
- active parallel tools settle without cancellation and elect one Replan;
- completed failures and denials count; incomplete/duplicate receipts do not;
- a completed Replan resets count and time once with no catch-up burst; and
- close, explicit stop, cancellation, and audit failure release/cancel waiters
  without a late Replan or message.

### 4.3 Runtime journeys

Add end-to-end fake-provider journeys proving:

1. `low` and `medium` never install compulsory cadence;
2. `high`, `xhigh`, and `max` always install it;
3. an eligible short task below both thresholds completes with zero Replans;
4. the tenth result forces `EXECUTING -> REPLANNING -> EXECUTING`, plan version
   N+1, one commentary, then a fresh 0/300 window;
5. exactly 300 seconds forces the same action before another tool is admitted;
6. a tool-free Execution response returned after 300 seconds triggers a
   completion-boundary Replan instead of bypassing the cadence;
7. `completion_percent < 100` resumes without replaying completed side effects;
8. `completion_percent == 100` does not start another Execution and routes to
   Review or Finalisation according to mode;
9. unchanged plans still receive a new version and truthful "plan unchanged"
   commentary;
10. omitted commentary produces the deterministic message once;
11. Persona failure produces the existing Agent display-name fallback once
    with the same validated facts;
12. Review and Verification remediation continue to work independently after
    any number of periodic Replans; and
13. `/stop`, `/steer`, audit failure, genuine user-input disposition, and Tool
    Gateway denial retain their current typed behaviour.

### 4.4 No-limit regressions

Add structural and controlled-clock tests proving:

- no `max_replans`, Replan attempt deadline, token budget, or turn/loop ceiling
  reaches the Replanning request or audit payload;
- more cadence cycles than every former `50/100/200` ceiling can complete
  without blocking Replanning or Finalisation;
- a Replanning provider operation may cross historical 60/180/300/600-second
  boundaries without Runtime cancellation;
- total tool results remain unbounded; only admission between windows pauses;
- the meaningful-progress detector is not reset by a synthetic trigger event
  and does not impose an absolute stage deadline; and
- Review/Verification attempt limits remain scoped only to those assurance
  stages and are not consumed by periodic Replanning.

### 4.5 Adapter and presentation tests

For each tool-capable provider family, prove the typed 100%-completion Replan
control is not flattened, ordinary continuation control preserves the completed
result, and `max_loops` remains unset. Presentation tests must prove stable checkpoint IDs,
exactly-once event reservation, normal Persona rendering, model-commentary
omission fallback, packaging failure fallback, and no duplicate delivery after
replay.

## 5. Wrong assertions to remove or rewrite

The following old assertions directly contradict the locked design and must no
longer pass:

- periodic control is gated by a model-authored Triage risk label;
- a checkpoint model decides `CONTINUE`, `USER_INPUT_REQUIRED`, or `HALT`;
- periodic control has `may_replan=false`;
- checkpoint/replan emits no commentary;
- a tool-free operation may cross 300 seconds and complete without a final
  safe-boundary Replan;
- the tenth tool result may be discarded, replayed, or detached from the
  compulsory Replan control that follows it;
- Replanning is limited to 50/100/200 total invocations; and
- Review or Verification remediation is denied because periodic Replans used a
  shared `max_replans` budget.

Tests for exact receipt counting, monotonic scheduling, active-tool safety,
parallel single-flight election, audit durability, user stop/cancellation,
permission denial, no tool-loop cap, no provider deadline, immutable goal and
classification, plan replacement only inside `REPLANNING`, and Persona
presentation isolation must be retained and adapted to the compulsory action.

## 6. Implementation sequence

1. Introduce a typed due-boundary signal and remove model decision authority
   from the cadence coordinator.
2. Add the structured three-question Replanning result and validator.
3. Change Runtime eligibility to effort `high` and above, catch the due signal,
   transition into principal `REPLANNING`, activate plan N+1, and route by
   completion percentage.
4. Preserve receipt/evidence context and install explicit no-side-effect-replay
   continuation context for the next Execution invocation.
5. Add stable checkpoint-commentary identity, mandatory deterministic neutral
   fallback, Persona packaging fallback, and exactly-once delivery/audit.
6. Remove periodic Replan ceilings and decouple assurance remediation from
   periodic Replan counts.
7. Delete or rewrite contradictory tests, then run focused unit, runtime,
   adapter, lifecycle, commentary, configuration, and no-limit suites.
8. Run Ruff correctness rules, compilation, `git diff --check`, forbidden-limit
   searches, and a final requirement-to-diff audit.
9. Update the product design, changelog/checkpoint documentation, and this
   plan's verification record only after the implementation proves the new
   contract.

## 7. Completion gate

This repair is complete only when every locked requirement maps to executable
code and a passing test, no contradictory checkpoint evaluator remains on the
active path, no unauthorised limit can suppress compulsory Replanning or the
whole workflow, and no source activation/reboot has been performed without
separate user authority.

## 8. Verification record

Completed on 2026-08-24 in the HASHI1 working tree:

- removed the old `CHECKPOINT` stage, assessor schema, decision types, prompt
  asset, provider route, and contradictory prompt tests;
- installed effort-based compulsory cadence for `high`, `xhigh`, and `max`;
- implemented safe-boundary 10-result/300-second coordination, three-question
  structured Replanning, plan version activation, below-100 continuation,
  100% stop, exact receipt preservation, and no-side-effect-replay context;
- implemented stable commentary identity, deterministic field-based fallback,
  protected Persona facts, Agent display-name Persona fallback, and at-most-once
  event reservation;
- removed all Replan-count policy/configuration authority and rejected the
  legacy `max_replans`, `replan_limit`, and `replan_limits` fields;
- proved 205 consecutive cadence cycles, provider time crossing historical
  timeout values, unbounded tool loops, and assurance-limit independence;
- passed the complete pre-integration offline product suite (`2677 passed`,
  `2 skipped`, `40 deselected`) and the deterministic core gate (`237 passed`),
  with the explicit HER v2 suite at `346 passed`, `1 skipped`;
- after integration with the two preceding Auto Compact commits, passed the
  combined focused suite (`403 passed`, `1 skipped`), the core gate
  (`237 passed`), and the complete offline product suite (`2685 passed`,
  `2 skipped`, `40 deselected`);
- passed focused Ruff correctness rules, Python compilation, whitespace/diff
  checks, and 207 internal Markdown target checks with no missing target; and
- committed the repair to local `main` as `7f2c1ac`, directly after the two
  retained Auto Compact commits `3950cc0` and `81f5d76`;
- completed the separately authorised targeted `/reboot min` with
  `requester=arale` and `targets=('arale',)`, verified the current HER runtime
  contract, returned Arale online, and recreated the Backend API and API
  Gateway with reloaded code; and
- did not push, tag, create a Release, or claim a real threshold-triggered
  compulsory-Replan canary.
