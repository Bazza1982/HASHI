# HER Debug Two-Stage Superloop

## Purpose

Drive the complete HER x DeepSeek certification plan through a persisted
test-repair-retest loop without starting Pro traffic early, silently losing a
failed worker reply, or substituting another provider or model.

This directory is a design template only. Creating it does not start a loop,
dispatch Ajiao, create a nudge, change Ajiao's settings, or spend API funds.
The authoritative test oracle remains
[`docs/HER_COMPREHENSIVE_TEST_PLAN.md`](../../../docs/HER_COMPREHENSIVE_TEST_PLAN.md),
and every confirmed product defect is retained in
[`docs/HER_bug_journal.md`](../../../docs/HER_bug_journal.md).

## Later activation contract — not executed

When the operator later authorizes this template to start, activation must:

1. instantiate the template as a paused loop and expand the complete campaign
   queue before any live request;
2. validate the two 24-cell stage definitions and frozen route/model allowlist;
3. create the unlimited one-minute `/nudge` from the `lin_yueru` runtime with
   an exit condition restricted to evidenced `PASSED` or `BLOCKED_FUNDS`;
4. persist that nudge ID in loop state and verify its job owner is
   `lin_yueru`, not `ajiao`;
5. load `liveness_nudge.template.md` as the controller continuation policy;
6. leave the loop paused until the operator's start instruction, then dispatch
   only the first eligible packet.

The intended later nudge command shape is:

```text
/nudge 1 until her_debug <loop_id> has evidenced terminal_result PASSED or BLOCKED_FUNDS; follow its liveness_nudge.template.md
```

This design turn does not run that command.

## Fixed ownership

- `lin_yueru@HASHI2` is the superloop controller and final verifier.
- `ajiao@HASHI2` is the test-and-repair worker.
- The idle `/nudge` belongs to `lin_yueru`, never to Ajiao.
- Ajiao keeps ownership of an assigned packet until she returns a terminal
  receipt or the controller explicitly records a reassignment authorized by the
  operator.

The nudge exists to wake the controller when the controller becomes idle. It
does not act as a worker watchdog and does not interrupt Ajiao. A nudge wake
must inspect Ajiao's actual state before doing anything:

- If Ajiao is still running, record the observation and keep waiting. Do not
  send `/stop`, cancel, restart, reassign, or dispatch a duplicate packet.
- If Ajiao returned a failed or incomplete response, preserve that response as
  evidence, keep the campaign non-terminal, and issue a correlated follow-up
  after confirming that she is no longer running.
- If Ajiao is idle and no terminal receipt exists, send one bounded status or
  continuation follow-up for the same packet and persist the next check.
- If a valid result is waiting, classify and verify it before advancing.

No failed worker reply may leave `next_action` empty. The controller either
verifies a result, follows up, enters an explicit wait, opens a defect, or
selects the next eligible packet.

## Frozen live targets

Only these live routes and model slugs are legal:

| Stage | Route | Model slug |
| --- | --- | --- |
| 1 | Official DeepSeek | `deepseek-v4-flash` |
| 1 | OpenRouter | `deepseek/deepseek-v4-flash` |
| 2 | Official DeepSeek | `deepseek-v4-pro` |
| 2 | OpenRouter | `deepseek/deepseek-v4-pro` |

Stage 1 contains 24 Flash cells: two providers by two HASHI modes by six
efforts. Stage 2 contains the corresponding 24 Pro cells. No test, retry,
repair check, reviewer check, fallback, or nudge-triggered action may send live
test traffic to any other API or model.

The scripted provider used by deterministic Layer A is a local protocol
fixture, not a live API or model, and can never satisfy a live cell.

## Stage machine

```text
planned
  -> preflight_and_lab
  -> layer_a_offline
  -> stage_1_flash: cheap -> presentation/continuity -> expensive
  -> stage_1_flash_gate
  -> stage_2_pro: cheap -> presentation/continuity -> expensive
  -> final_same_candidate_gate
  -> PASSED
```

`stage_2_pro` is mechanically locked until all Stage 1 Flash work, regressions,
defect retests, and the Stage 1 gate pass. A shared-runtime repair during Stage
2 invalidates the older Flash evidence, pauses Pro advancement, and returns the
campaign to Flash revalidation before Pro may continue.

Cheap waves always precede paid native-boundary, MAX/MAX+, endurance, and fault
waves. The controller expands work in this order:

1. deterministic Layer A;
2. stage wave;
3. effort `low`, `medium`, `high`, `xhigh`, `max`, `max+`;
4. provider and mode according to the persisted batch manifest;
5. scenario and presentation variant.

Every atomic packet has a stable key:

```text
stage/provider/model/mode/effort/wave/scenario/presentation
```

No packet can pass by sampling or by a neighboring route's result.

## Per-packet cycle

For each effort/task combination, the controller repeats this cycle:

1. Freeze the candidate hash and dispatch exactly one bounded test packet to
   Ajiao with its expected receipt and evidence paths.
2. Actively monitor without interrupting her while the dispatch is running.
3. Verify the returned evidence and classify the outcome.
4. On pass, persist the verdict and select the next eligible packet.
5. On a HER/HASHI defect, preserve the first complete evidence bundle and open
   or update its bug-journal record before editing code.
6. Add a deterministic failing regression when possible, apply the smallest
   repair, build a new immutable candidate, and retest the exact failure.
7. Repeat repair and exact retest until fixed, then run the plan's defined
   blast-radius retests and complete the journal's root-cause, fix, regression,
   candidate-hash, and verification fields.
8. Invalidate any earlier verdict that no longer belongs to the current
   candidate, then resume the ordered queue.

A model deviation, harness fault, transient route failure, worker transport
failure, or failed Ajiao response is not silently converted into a product bug
or a campaign terminal state. It receives its own classification and explicit
next action. Mandatory inconclusive packets remain outstanding.

## Funds rule

Official DeepSeek and OpenRouter are both required. A confirmed insufficient
funds/credits response from either required route makes the mandatory matrix
impossible and ends the loop as `BLOCKED_FUNDS`.

Confirmation requires the provider's explicit response plus a read-only
balance check when available, or one bounded confirmation probe on that same
route. Rate limits, timeouts, DNS failures, bad credentials, and generic 5xx
responses are not funds exhaustion. They enter an explicit same-route wait or
repair path. The controller must never fall back to another API or model.

## Liveness and no-silent-failure contract

The controller nudge is unlimited and remains enabled while the campaign is
non-terminal. `scheduler_auto_advance=false`: the scheduler and nudge cannot
mark a pending phase task in progress. This restriction does not cover the next
packet of the current `in_progress` task: campaign start authority persists, so
the controller must dispatch exactly one selected packet after the worker,
candidate, wait, duplicate-dispatch, and stage interlocks pass. It must never
request fresh non-nudge authority for each packet. After a wake, the controller
may advance only after reading state, taskboard, waits, issues, active dispatch,
queued replies, and evidence.

Three consecutive observations of the same idle, unstarted selected packet are
the stagnation limit. The next decision must dispatch that packet, persist a
concrete wait or blocker, or fail validation; merely moving the next-check time
again is a functional livelock.

Every dispatch records:

- packet and attempt IDs;
- worker and controller identities;
- dispatch reference and expected receipt;
- worker state: `not_dispatched`, `running`, `replied`, `failed_reply`,
  `idle_without_receipt`, or `offline`;
- last progress observation and next follow-up time;
- follow-up count and references;
- result classification and next action.

The nudge emits its completion marker only after one of the two permitted
terminal results has been persisted with evidence:

- `PASSED`: Layer A, all 24 Flash cells and gates, all 24 Pro cells and gates,
  all required ancillary suites, final same-candidate certification, cleanup,
  and reply drain succeeded.
- `BLOCKED_FUNDS`: a required route's funds exhaustion was confirmed by the
  rule above, completed evidence was preserved, and every unrun packet was
  listed.

The generic Superloop state remains contract-compatible: `PASSED` maps to
`state.status=completed`, while `BLOCKED_FUNDS` maps to
`state.status=blocked`; the precise outcome is stored in `terminal_result`.
Funds exhaustion may take this exceptional terminal transition from any live
task. Unrun tasks remain visibly outstanding and are never relabeled as
successful to satisfy ordinary task dependencies.

All other conditions, including repeated worker failure, offline state,
transient provider errors, broken harnesses, unresolved bugs, and missing
operator authority, are persisted `waiting` or `blocked` states. They do not
close the loop and do not disable the nudge.

## Closeout barrier

Before `PASSED`, the controller must personally prove:

- Layer A is green;
- the Flash gate and Pro gate are green on the final candidate;
- all 48 core cells and all mandatory scenario/presentation/boundary evidence
  are present;
- no affected P0/P1/P2 defect remains open;
- every fix has its regression and completed journal entry;
- no disallowed route/model appears in a live manifest;
- Ajiao's original settings are restored;
- all same-loop Ajiao replies are drained and classified;
- no active dispatch, open wait, or blocker remains.

Health, process, or port status alone is never acceptance evidence.
