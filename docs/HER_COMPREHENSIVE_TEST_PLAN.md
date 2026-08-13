# HER × DeepSeek Comprehensive Certification Plan

Status: executable test design
Target: HASHI2 / Ajiao isolated test workzone
Created: 2026-08-11
Companion record: [HER_bug_journal.md](HER_bug_journal.md)
Controller template: [HER Debug Two-Stage Superloop](../superloops/templates/her_debug/README.md)

> **2026-08-13 integration note:** this document preserves the original
> HASHI2/Ajiao certification campaign and its 48-cell oracle. HASHI1 now carries
> the same certified `.10` package plus later Habit and multimedia changes, but
> has not inherited a completed 48-cell live verdict. The source audit found
> that Flex/composed HER could resume a stored session while receiving full
> context (`HER-20260813-022`); `2270f5be` fixes that code path and adds
> deterministic regressions, while live matrix verification remains pending.
> Supplemental Habit/media gates are in section 19.

## 1. Purpose

This plan certifies that HASHI Engine Runtime (HER) behaves correctly with both
DeepSeek routes, both supported DeepSeek V4 models, every HER effort level, and both
HASHI working modes over short and long-running sessions.

Execution is deliberately split into two cost-ordered stages:

1. **Stage 1 — Flash only.** Complete and repair all Flash coverage first.
2. **Stage 2 — Pro only.** This stage stays mechanically locked until Stage 1 passes
   with no unresolved blocking HER issue.

Within each stage, cheap deterministic and short tests run before long, native-boundary,
MAX, or MAX+ work. Flash and Pro are never mixed in one active stage.

The target is HER and HASHI integration correctness. It is **not** a benchmark of
DeepSeek intelligence, a prompt contest, or permission to redesign HER. A run is judged
by observable protocol and state:

- provider/model routing is correct;
- streamed text and reasoning fragments are neither lost nor altered;
- messages, progress, verbose output, and final delivery remain ordered and readable;
- tool calls execute once, return once, and leave verifiable state;
- effort limits, finalization reserve, and MAX+ time handling end cleanly;
- fixed and flex context are neither lost nor replayed twice;
- `/stop`, bare continuation, `/new`, restart, and compaction preserve the state the
  current HASHI design promises;
- incomplete work is reported honestly and never converted into a terminal status that
  contradicts HER's own loop/tool evidence, or a generic error after valid work completed;
- long sessions do not corrupt subsequent turns, Memory+, or session identity.

Wrong code produced by a model on a genuinely difficult fixture is not automatically a
HER bug. A missing terminal event, corrupted stream, lost prompt, duplicate side effect,
wrong route, false completion, runaway loop, or incorrect recovery **is** a HER bug.

## 2. Scope and test oracle

### 2.1 In scope

- HASHI2 orchestration, HER adapter, packaged HER binary, Tool Gateway, and delivery.
- Official DeepSeek API and OpenRouter routes.
- `deepseek-v4-flash` and `deepseek-v4-pro` on both routes.
- HER efforts `low`, `medium`, `high`, `xhigh`, `max`, and `max+`.
- HASHI `fixed` and `flex` modes.
- Cold starts, warm sessions, long sessions, interrupted sessions, process failures,
  provider faults, exact iteration exhaustion, and MAX+ time exhaustion.
- Thinking on/off, verbose on/off, typing on/off, and final-message promotion.
- Consecutive user messages, queue order, interrupted-task continuation, Memory+,
  context compaction, and session recreation.
- Safe local use of all tools with full permission inside the dedicated lab.

### 2.2 Out of scope

- Comparing Flash and Pro answer quality, creativity, or benchmark scores.
- Changing effort budgets, MAX+ policy, provider reasoning depth, or HER architecture.
- Real mutations outside Ajiao's disposable lab.
- HASHI1 deployment or validation.
- Testing unrelated wrapper, audit, or dual-brain behavior except where shared delivery
  code needs an offline regression check.

### 2.3 Mode oracle

The visible behavior, rather than a particular internal implementation, is the oracle:

- **Fixed:** one persistent HER session; after the first turn HASHI sends incremental
  prompts; the same session identity continues; conversation history is not reinjected.
- **Flex:** HASHI supplies full context per request and permits provider/backend changes.
  HER may keep an internal checkpoint only if the effective context is still delivered
  exactly once. Replaying the same history through both HASHI and HER is a failure.
- **Both modes:** the current unfinished user task, completed tool side effects, and
  authorized continuation must remain recoverable. `/new` starts a clean session.

Before live execution, record the effective prompt audit for one fixed and one flex
probe. If implementation documentation disagrees about session reuse, open a
`SPEC-AMBIGUITY` journal entry before changing code. The certification oracle above
must not be silently changed to make a failing build pass.

## 3. Complete combination matrix

### 3.1 Provider/model routes

| Route code | Provider | Base route | Flash model | Pro model |
| --- | --- | --- | --- | --- |
| `OR` | OpenRouter | OpenRouter OpenAI-compatible API | `deepseek/deepseek-v4-flash` | `deepseek/deepseek-v4-pro` |
| `DS` | Official DeepSeek | DeepSeek OpenAI-compatible API | `deepseek-v4-flash` | `deepseek-v4-pro` |

The preflight records the resolved base URL and model slug without recording API keys.
A successful answer from the wrong route is a failure.

### 3.2 Effort contract

| Effort | Native maximum iterations | Planning | Additional contract |
| --- | ---: | --- | --- |
| `low` | 12 | off | short direct execution |
| `medium` | 32 | on | adaptive execution plan; no review capability |
| `high` | 96 | on | optional plan-selected self-review |
| `xhigh` | 192 | on | optional plan-selected independent read-only review |
| `max` | 384 | on | optional plan-selected independent read-only review |
| `max+` | 512 | on | same review plus optional isolated rerun of exact plan-declared tests; no private time or token cap |

The test harness must assert the environment passed to HER. It must not infer the
effort from how long the model appeared to think.

Effort is a capability ceiling, not a required amount of work. Every planned effort
must prove that a direct-response task such as a greeting stops after planning and one
reply, without tools, testing, or review. Non-trivial fixtures must also prove that the
selected task profile—not the effort label alone—controls verification, testing,
review targets, and stop conditions.

### 3.3 Core cells

The full factorial matrix contains:

```text
2 providers × 2 models × 2 modes × 6 efforts = 48 mandatory cells
```

Cell IDs use:

```text
HER-LIVE-{OR|DS}-{FLASH|PRO}-{FIXED|FLEX}-{LOW|MEDIUM|HIGH|XHIGH|MAX|MAXPLUS}
```

No cell may be replaced by sampling. Execution may be staged, but release certification
requires all 48 cells to have a final verdict against the same candidate package.

The matrix is split into two strict 24-cell gates:

```text
Stage 1: 2 providers × Flash × 2 modes × 6 efforts = 24 cells
Stage 2: 2 providers × Pro   × 2 modes × 6 efforts = 24 cells
```

Stage 2 cannot start from a merely “mostly green” Flash result. All Stage 1 cells,
presentation runs, native boundaries, continuity cases, and applicable fault cases must
pass, and every blocking Flash defect must be fixed, journaled, and retested first.

The campaign checklist is therefore:

| Provider | Model | Mode | low | medium | high | xhigh | max | max+ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| OR | Flash | Fixed | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| OR | Flash | Flex | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| OR | Pro | Fixed | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| OR | Pro | Flex | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| DS | Flash | Fixed | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| DS | Flash | Flex | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| DS | Pro | Fixed | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| DS | Pro | Flex | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |

The actual result report replaces each box with its immutable batch/cell verdict link;
the planning document itself remains unchanged.

### 3.4 Two-stage gate and rollback rule

The allowed model set is frozen by stage:

- Stage 1 allows only the two configured Flash slugs in section 3.1.
- Stage 2 allows only the two configured Pro slugs in section 3.1.
- No controller, test, repair, retry, reviewer, fallback, or nudge request may select a
  different model or API provider.

If a Pro-stage repair changes HASHI, HER source, the packaged binary, prompt assembly,
Tool Gateway, or delivery code shared with Flash, Stage 1 evidence for the old candidate
becomes stale. The loop returns to a Flash revalidation gate before continuing Pro. A
cheap affected-cell Flash check runs immediately; full Stage 1 must pass against the
final immutable candidate before final certification.

## 4. Ajiao test lab and full-permission safety

### 4.1 Isolation

All mutable work occurs below this repo-relative directory:

```text
workspaces/ajiao/her_test_lab/
```

Each run gets an exact, non-reused directory:

```text
workspaces/ajiao/her_test_lab/runs/<run_id>/
```

The lab contains a disposable Git repository, generated fixtures, a local Tool Gateway
fixture, evidence, and a cleanup manifest. No test receives a path above its run root.

### 4.2 Permission profile

The test instance uses a temporary overlay with:

- global HER maximum permission `danger-full-access`;
- route permission `danger-full-access`;
- permission prompts skipped;
- every local test tool enabled.

Full permission applies only to the isolated Ajiao lab. It does not waive these safety
rules:

- no production secrets are copied into fixture files or logs;
- no destructive command targets a parent directory, home directory, workspace root,
  or unresolved variable;
- external mutation tools are disabled or replaced with local fakes;
- web tests are read-only unless they use a local HTTP fixture;
- email, messaging, payment, cloud, and social side effects are never real;
- cleanup resolves and verifies the exact run root before deleting disposable content;
- evidence and the bug journal are retained.

### 4.3 Baseline capture

Before every batch, save:

- HASHI commit, dirty state, Python version, and dependency lock hash;
- packaged HER version, source commit, binary SHA-256, and certification baseline hash;
- provider, model, mode, effort, permission overlay, and display-policy values;
- Ajiao process identity, health, current session checkpoint, and Memory+ state;
- a redacted configuration snapshot and a workspace manifest.

After every batch, restore Ajiao's original mode, provider, model, effort, permission
ceiling, display policy, and Memory+ setting. A failed restoration blocks the next batch.

## 5. Test layers

Live tests alone cannot reliably force exact provider fragmentation or exactly 513
model turns. The suite therefore has four layers. All are release gates.

### Layer A — deterministic offline contract tests

Use a scripted provider and Tool Gateway. These tests are cheap, repeatable, and must
prove exact edge behavior before any paid API run:

1. Every stdout line is valid JSONL in stream mode.
2. Exactly one `run_started` and one terminal `run_finished` are emitted.
3. Leading, trailing, repeated, newline-only, tab-only, and whitespace-only reasoning
   fragments concatenate byte-for-byte.
4. `thinking_delta`, `thinking_redacted`, and `thinking_summary` are counted once; redacted
   reasoning is never reconstructed.
5. Assistant deltas concatenate exactly to the promoted final answer.
6. Tool start/end pairs are balanced and correlated by ID.
7. Structured permission events never print an interactive prompt into JSONL.
8. Thinking-only output receives one tool-free visible-finalization retry.
9. Repeated thinking-only output ends as `incomplete/no_final_text` with a deterministic
   report and a terminal event.
10. Reasoning-only assistant history is not sent as an invalid DeepSeek history message.
11. Provider 400, 401, 403, 408, 429, 500, truncated SSE, malformed JSON, connection reset,
    and delayed response produce the expected sanitized outcome.
12. Native limits 12/32/96/192/384/512 are hit exactly using a scripted model.
13. MAX+ iteration-ceiling handling is tested; neither an internal wall-clock nor
    cumulative token usage terminates the task.
14. Repeated identical tool calls, excessive total calls, and consecutive tool errors are
    stopped by the Gateway with explicit partial-progress guidance.
15. Process cancellation after `run_started`, after a successful tool result, and just
    before `run_finished` leaves a recoverable, correctly owned checkpoint.
16. Fixed/flex prompt assembly, `/stop` rebinding, `/new`, Memory+, compaction, and final
    delivery are verified without a network dependency.
17. At MEDIUM through MAX+, a direct-response greeting produces one adaptive plan and one
    reply with no task tools, test, self-review, or independent review; LOW replies without
    a planning call.
18. HIGH exposes only optional self-review. XHIGH, MAX, and MAX+ expose optional
    independent review, and the plan may select `none` at every level.
19. An independent reviewer receives a separate read-only registry, can inspect actual
    source files, Git status/diff/log/show/blame, and file SHA-256, and cannot mutate the
    workspace even when the primary agent can.
20. Every task tool input/output remains immutable and page-addressable by stable evidence
    ID. Compact-packet truncation preserves the proposed answer and lets the reviewer fetch
    the omitted raw result instead of resending the ledger.
21. `revise` and `block` verdicts reach the primary agent as advisory feedback. Exhausting
    review revisions still produces an agent-owned, uncertainty-aware final answer; a final
    reviewer `pass` is not required.
22. Only a MAX+ profile that selects `isolated_recheck`, independent review, and exact test
    commands receives `ReviewRun`. Unplanned commands are denied, the source workspace is
    not writable, network is unavailable, and no unsafe fallback runs when isolation is
    absent.

### Layer B — staged live cells

Stage 1 runs the mandatory cell pack on all 24 Flash cells. Only its complete gate may
unlock Stage 2, which runs the same pack on all 24 Pro cells. This confirms the same
contracts against the real OpenRouter and official DeepSeek routes without spending Pro
funds before shared Flash defects are repaired.

### Layer C — native-boundary and endurance runs

Every core cell receives a native-limit run. These runs are scheduled after Layer B is
clean because high limits are expensive. A deterministic local step tool makes work
depend on sequential turns rather than puzzle-solving ability.

For MAX+, the only private native execution boundary is 512 iterations. No special
1,500-second runtime budget exists. `/timeout` remains HASHI's separate operator-owned
outer control and is tested independently from HER's iteration ceiling.

### Layer D — failure injection and restart recovery

Run provider-proxy faults, Tool Gateway faults, process kills, HASHI restart, storage
faults confined to the run directory, and delivery retries. This layer verifies that a
valid partial result is not replaced by a misleading generic error.

## 6. Mandatory live cell pack

Each of the 48 cells executes these ten scenario groups, Stage 1 before Stage 2. A
multi-turn group counts as one group but retains every turn as separate evidence.

| ID | Scenario | Required result |
| --- | --- | --- |
| `C00` | Cold route and binary preflight | Correct package, endpoint family, model slug, mode, effort, clean session, and full lab permission |
| `C01` | Easy exact-output task | Unicode, emoji, Markdown, code fences, spaces, and newlines arrive unchanged; one final message |
| `C02` | Easy local tool transaction | Read, write, shell, and Git actions each occur once; artifact hashes match |
| `C03` | Effort-sized nominal task | Deterministic fixture acceptance tests pass or the agent reports honest unfinished work |
| `C04` | Near-boundary sequential task | Finalization reserve engages without losing prior tool evidence |
| `C05` | Deliberately over-bound task | Clean `incomplete` result; correct stop reason; no crash, spin, or false success |
| `C06` | Eight-turn continuity chain | Canaries, session/context, file state, and response order remain exact |
| `C07` | `/stop` and bare continuation | Original task is rebound; completed side effects are not repeated; work resumes safely |
| `C08` | Consecutive messages and delivery | Busy-queue order, verbose updates, thinking display, and final promotion remain correct |
| `C09` | Warm repeat and clean reset | Second task does not inherit stale task state; `/new` creates a clean identity |

The ten groups produce 480 mandatory cell-scenarios before the display-policy expansion
and fault suite. `C06`–`C08` deliberately contain several requests, so reporting only a
run count is insufficient; the evidence must also record request and event counts.

## 7. Difficulty ladder by effort

### 7.1 Common deterministic fixtures

Each fixture is versioned and has machine-verifiable acceptance tests. The prompts state
the desired result and safety boundary but never reveal the patch.

- **Easy exactness:** create a receipt containing Chinese, English, emoji, tabs, blank
  lines, leading/trailing spaces, and a fixed SHA-256 source string.
- **Safe mutation:** edit three fixture files, run a local verifier, commit inside the
  disposable repository, and report the commit plus artifact hashes.
- **Nominal repair:** diagnose seeded defects and satisfy fixture tests.
- **Sequential edge:** use `her_step_lab`, which unlocks exactly one next step per model
  round, rejects skipped or repeated step tokens, and records every accepted step.
- **Impossible/over-bound:** request more sequential steps than the current native limit.
  Completion is impossible; the correct outcome is a precise incomplete report.

### 7.2 Per-effort workload

| Effort | Nominal deterministic repository task | Sequential edge target | Over-bound target |
| --- | --- | ---: | ---: |
| `low` | inspect three small files, fix one seeded defect, run one focused test | 11 rounds | 13 rounds |
| `medium` | repair a three-module CSV-to-JSON tool and run unit tests | 31 rounds | 33 rounds |
| `high` | repair a six-module incremental-cache fixture and run unit/integration tests | 95 rounds | 97 rounds |
| `xhigh` | refactor a ten-module event pipeline while preserving its public fixture API | 191 rounds | 193 rounds |
| `max` | complete a staged schema migration, rollback check, and full local verification | 383 rounds | 385 rounds |
| `max+` | complete a staged migration whose plan selects direct inspection, evidence review, rollback rehearsal, and exact isolated rechecks | calibrated below 512 rounds | 513 rounds |

The sequential harness counts **model loop iterations**, not raw tool calls; batching
several tool calls in one model response cannot falsely satisfy the target. Before the
live native run, a short calibration confirms that the local tool latency is negligible.

For routine Layer B exit testing, also run the same over-bound scenario with a test-only
iteration override of 8. This validates every provider/model/mode route quickly. It does
not replace the native-boundary run in Layer C.

### 7.3 Capability-neutral verdict

- If the model follows the fixture protocol and HER corrupts or loses it: **HER/HASHI
  failure**.
- If the model refuses a safe, authorized fixture or ignores a step token while the
  runtime remains correct: **model deviation / inconclusive**, rerun cold up to three
  times and do not call it a HER regression.
- If the model claims success while fixture acceptance fails, but HER faithfully records
  the provider end and tool ledger: **model deviation / inconclusive**, not a HER bug.
- If HER's terminal status contradicts its own event stream, stop reason, or tool ledger:
  **HER contract failure**.
- If the agent honestly reports incomplete before the native limit, the exit contract
  passes but the exact-limit case remains unexecuted and must be rerun with the scripted
  provider.

## 8. Streaming, thinking, verbose, and message delivery

### 8.1 Exact stream assertions

For every request:

- JSONL parses line by line; no prompt text or interactive permission text appears
  outside a JSON event.
- Event timestamps and sequence numbers are monotonic.
- `run_started` precedes all work; `run_finished` is the sole terminal event.
- raw provider reasoning fragments equal HER reasoning fragments byte-for-byte after
  concatenation; no trimming and no inserted spaces are allowed.
- HASHI's displayed thinking equals the allowed HER reasoning stream, subject only to
  documented display chunking; words may not join or split.
- encrypted/redacted reasoning produces only the redaction notice and zero reconstructed
  text.
- token figures are non-negative; actual provider reasoning usage wins over estimates;
  summaries never double count actual deltas.
- assistant deltas, preview edits, and final message contain the same final text exactly
  once.
- Markdown/code fences remain balanced; long-message chunking loses and duplicates no
  bytes.
- a valid `run_finished` can never be followed by a reported backend failure for that
  request.

### 8.2 Presentation-policy matrix

For every one of the 48 core cells, run all eight combinations:

```text
thinking on/off × verbose on/off × typing on/off = 8 presentation runs per cell
```

This adds 384 presentation runs. The task and seed are identical within each group so
only presentation behavior changes. Final user-visible content and workspace state must
be identical across all eight runs. Disabled thinking must suppress display, not alter
provider reasoning history or token accounting. Disabled verbose output must suppress
progress messages, not tool execution. Disabled typing must not create or strand a
placeholder.

## 9. Tools and execution-loop assertions

The Tool Gateway evidence must show:

- every call has one start and one end with matching request, call, tool, and session IDs;
- successful side-effect tools are never automatically replayed after delivery or
  provider failure;
- a retry of a read-only call is visible and bounded;
- parallel tool calls, when supported, are all accounted for before the next model turn;
- tool stdout/stderr and result JSON cannot merge into adjacent stream events;
- nonzero shell exit, malformed tool result, timeout, and cancellation remain distinct;
- repeated identical calls and consecutive errors stop at the documented Gateway guard;
- the final report distinguishes verified successes, failures, and unverified work;
- iteration count, tool-loop count, tool-call count, and tool-result count reconcile;
- MAX/MAX+ review and checkpoint events remain correlated to the same request and are
  written atomically;
- no test writes outside its resolved run root even though full permission is active.

## 10. Continuity and long-session suite

### 10.1 Eight-turn chain in every cell

Each turn carries a unique canary. The chain is deterministic:

1. create a task ledger and canary A;
2. mutate fixture state and record canary B;
3. ask for the exact current state without repeating A/B in the new user prompt;
4. introduce a second subtask and canary C;
5. send two user messages while the agent is busy;
6. verify queue order and that both messages are represented once;
7. ask for a compact progress report and remaining work;
8. complete, verify artifact hashes, and report all canaries in order.

Fixed-mode audits must show incremental prompts and stable session identity. Flex-mode
audits must show full HASHI context and exactly-once effective history. Neither mode may
inject stale context from another cell.

### 10.2 Interrupted task

Start a multi-step mutation, wait until at least one tool result is verified, issue
`/stop`, then send only “continue”. Assert:

- HASHI persisted the authoritative original prompt;
- continuation metadata refers to the interrupted request;
- the partial file and tool ledger survived;
- completed mutations are not repeated;
- session ownership stays with Ajiao and the selected route/model;
- the terminal response is completed or a valid incomplete report, never an unexplained
  error exit.

Repeat cancellation at three points: after `run_started`, immediately after a successful
tool result, and during visible finalization.

### 10.3 Session and context transitions

Run these transition paths for Flash and Pro on both providers:

- fixed → fixed warm turn;
- fixed → flex → fixed;
- flex provider switch OR ↔ DS;
- flex model switch Flash ↔ Pro;
- `/new` followed by a canary-leak probe;
- adapter recreation with a valid checkpoint;
- adapter recreation with a checkpoint for another model;
- HASHI2/Ajiao controlled restart after checkpoint persistence;
- context compaction before, during, and after a multi-step task.

A switch may intentionally start a new HER session, but it must never lose the HASHI
task context promised by the selected mode or resume another model's session.

### 10.4 Memory+

Run Memory+ off and on. With Memory+ on:

- write a harmless unique continuity fact through the normal observer path;
- verify the next turn receives the correct compact card once;
- verify fixed uses the session profile and flex uses the stateless/full-context profile;
- force compaction and confirm the unfinished task plus latest verified state survives;
- confirm the hidden Memory+ update block never appears in the visible answer;
- reset the lab memory after the cell so canaries cannot contaminate later tests.

### 10.5 Endurance

For each provider/model/mode route (eight route-mode pairs), run:

- 50 consecutive short turns at `low`;
- 30 mixed tool turns at `high`;
- one native-boundary task at every effort;
- one MAX+ task until completion or its 512-iteration boundary;
- one 12-hour warm-session soak with periodic safe probes;
- one 24-hour hard-timeout simulation using a fake clock, plus a shorter live idle-timeout
  probe where operationally safe.

Check memory growth, process count, file descriptors, orphaned HER processes, duplicate
Telegram messages, session-file size, compaction count, and latency drift.

## 11. Exit and fault matrix

Every terminal path must have a deterministic expected status and user message.

| Fault or boundary | Expected behavior |
| --- | --- |
| normal provider end | `completed`, provider stop recorded, one final answer |
| native iteration ceiling | `incomplete/max_iterations`, verified partial work listed, continuation offered |
| repeated thinking-only response | `incomplete/no_final_text`, deterministic visible report |
| one thinking-only response | one tool-free visible-finalization retry, then normal terminal event |
| provider 400 invalid history | sanitized error or recovered visible finalization; never raw prompt leakage |
| 401/403 | fail closed with route/auth diagnosis; no secret text |
| 429/5xx/transient reset | bounded retry; side effects not duplicated; final state explicit |
| malformed/truncated stream | protocol failure naming the last safe event; no invented completion |
| tool error loop | Gateway guard stops loop and asks for partial report |
| `/stop` | child process terminates, original task persists, no generic failure delivery |
| kill after tool success | tool result remains auditable; next turn verifies before retrying |
| missing `run_finished` | request fails closed; error is sanitized and identifies protocol failure |
| valid `run_finished` then stderr noise | completed result remains authoritative |
| HASHI delivery retry | backend is not rerun; final message delivered at most once |
| `/new` | old session cleared; new canary-leak probe is clean |

Faults are injected through a local proxy, fake provider, fake clock, or test process.
Do not trigger real account lockouts or provider abuse controls.

## 12. Evidence and automated verdicts

Each run directory contains:

```text
run_manifest.json
config_redacted.json
prompt_audit.json
provider_trace_redacted.jsonl
her_stdout.jsonl
her_stderr_redacted.log
hashi_events.jsonl
tool_audit.jsonl
delivery_transcript.jsonl
workspace_before.json
workspace_after.json
acceptance_test.log
verdict.json
```

`run_manifest.json` records cell ID, scenario ID, request IDs, seed, commits, package
hashes, route, mode, effort, presentation policy, start/end times, and parent batch ID.

`verdict.json` records every assertion separately. Allowed verdicts are:

- `PASS` — all contract assertions passed;
- `FAIL-HER` — HER source/runtime contract failed;
- `FAIL-HASHI` — HASHI context, queue, adapter, or delivery contract failed;
- `FAIL-PROVIDER-CONTRACT` — one provider route returned an incompatible stream that the
  route adapter did not handle;
- `INCONCLUSIVE-MODEL` — model did not follow the deterministic fixture, but no runtime
  fault was observed;
- `INVALID-ENVIRONMENT` — lab, route, quota, or harness was not in the declared state.

An inconclusive or invalid run does not satisfy a mandatory cell. It must be rerun after
the cause is removed. The suite never converts it to pass.

### Required cross-checks

- Provider trace → HER JSONL → HASHI stream event → delivery transcript hashes reconcile.
- Tool audit → filesystem/Git manifest reconcile.
- Session ID and request ID ownership reconcile.
- Usage totals are non-negative and do not double count summaries.
- Terminal status and user-facing message agree.
- No key-like or prompt-secret canary appears in errors or logs.

## 13. Pass/fail gates

### Per request

A request passes only when all applicable route, protocol, stream, tool, state, terminal,
delivery, and secrecy assertions pass. Artifact success cannot excuse a terminal error;
a pretty answer cannot excuse missing artifacts.

### Per cell

A cell passes only when:

- all ten mandatory scenario groups pass;
- all eight presentation-policy runs pass;
- the native-boundary run ends correctly;
- no unresolved P0/P1/P2 journal entry affects the cell;
- three cold repeats of the core exactness scenario pass;
- one warm repeat passes after the continuity chain.

### Stage 1 — Flash gate

Stage 1 passes only when:

- all 24 Flash cells pass on the same candidate;
- all Flash presentation, boundary, continuity, endurance, and applicable fault cases
  pass;
- all Flash-discovered bugs are fixed, journaled, and verified;
- no P0/P1/P2 issue affecting Flash remains open;
- HASHI and HER deterministic certification is green.

Only a persisted `stage_1_flash=passed` gate may select a Pro model.

### Stage 2 — Pro gate

Stage 2 passes only when all 24 Pro cells satisfy the same rules. A shared-runtime fix
made during Stage 2 marks the Flash gate `revalidation_required`; Pro advancement pauses
until the required Flash revalidation succeeds.

### Release candidate

The HER candidate is certified only when:

- deterministic offline suite is fully green;
- all 48 cells pass against one immutable package SHA-256;
- fault and restart suites pass;
- full HASHI test suite and full HER source certification pass;
- every fixed bug has an automated regression test and linked journal entry;
- reruns cover the failing cell, its same-route neighbor, its other-mode twin, and the
  full offline suite;
- Ajiao's original settings are restored and a final clean smoke passes.

There is zero tolerance for corrupt reasoning whitespace, missing terminal events,
duplicate mutations, lost task identity, wrong route/model, secret leakage, or false
completion. Flakiness is a failure, not a reason to average results.

## 14. Execution order

1. **Freeze the oracle and candidate.** Record commits, package hash, configuration, and
   known ambiguities. Do not edit the candidate during a batch.
2. **Build and self-test the lab.** Verify fixture hashes, local proxy, fake provider,
   sequential-step tool, evidence redaction, and cleanup guard.
3. **Run Layer A.** Stop immediately on any deterministic contract failure.
4. **Stage 1 Flash cheap wave.** Run low→medium→high→xhigh→max→max+ short smokes on both
   providers and both modes, with easy and nominal fixtures before long work.
5. **Stage 1 Flash display/continuity wave.** Run presentation, consecutive-message,
   fixed/flex, `/stop`, Memory+, and restart cases.
6. **Stage 1 Flash expensive wave.** Run native limits, MAX/MAX+, endurance, and fault
   injection only after cheaper Flash waves are green.
7. **Close Stage 1.** Fix/retest every defect, update the journal, run full Flash gate,
   and persist `stage_1_flash=passed`.
8. **Stage 2 Pro cheap wave.** Only now run the same short matrix with Pro.
9. **Stage 2 Pro display/continuity and expensive waves.** Keep the same cheap-first
   ordering; never fall back to another model.
10. **Fix one defect class at a time.** Journal it before repair, add a failing
    deterministic regression, implement, then rerun the defined blast radius. Shared
    repairs trigger the Flash revalidation rule in section 3.4.
11. **Final certification.** Run complete HASHI tests, HER source certification, all 48
    cells on the final candidate, and a clean Ajiao smoke.

Provider order is rotated by batch and seeds are fixed in the manifest. API cost/quota
approval is an execution prerequisite, not a HER task token budget. The tests must never
reintroduce the removed cumulative token ceiling.

### Funds-exhaustion terminal rule

Both official DeepSeek and OpenRouter are required routes. Therefore, a confirmed
insufficient-funds/credits result from **either** required route makes the comprehensive
matrix impossible and ends the campaign as `BLOCKED_FUNDS`, not `PASS`.

- Confirm the classification from an explicit provider response plus the same
  provider's read-only balance/credit check when available, or one bounded same-route
  confirmation probe.
- Do not classify a transient 429, timeout, DNS error, authentication mistake, or generic
  provider failure as funds exhaustion.
- Do not switch to another API, another model, a local model, or a mock and call the live
  cell passed.
- Preserve completed evidence and list every unrun cell. Never mark remaining work as
  skipped-success.
- A temporary rate limit enters a persisted wait with bounded backoff; it does not unlock
  a fallback provider/model.

## 15. Stop-the-line rules

Pause the live matrix and open a journal entry immediately when any of these occurs:

- possible secret or full private prompt leakage;
- write outside the resolved run root;
- duplicate external or filesystem side effect;
- wrong provider/model or cross-agent session identity;
- missing/corrupt terminal event;
- valid work followed by a false error exit;
- thinking text changes after provider capture;
- uncontrolled loop, orphan process, or cleanup guard failure;
- candidate source/package hash changes mid-batch.

Do not continue collecting hundreds of identical failures. Preserve the first complete
evidence bundle, reproduce offline, fix, and restart the affected phase with a new batch
ID.

## 16. Bug handling and regression policy

All defects are recorded in [HER_bug_journal.md](HER_bug_journal.md) before repair. Each
entry must contain the exact cell and run ID, expected/actual behavior, first failing
event, evidence paths, owning layer, root cause, fix commits, regression test names,
package SHA, and retest matrix.

No journal entry is deleted. A recurrence reopens the original ID and increments its
recurrence count. A fix is not `verified` until its regression fails on the bad build,
passes on the fixed build, and the live affected cells pass. This turns every discovered
hidden issue into a permanent release gate.

## 17. Final deliverables

The completed campaign produces:

- immutable batch manifests and redacted evidence bundles;
- a 48-cell matrix report plus presentation, endurance, and fault reports;
- provider/model/mode/effort comparison limited to runtime behavior;
- an updated HER bug journal;
- one regression test per confirmed defect;
- exact HASHI and HER source commits and packaged binary SHA-256;
- a signed-off certification result: `PASS`, `FAIL`, or `BLOCKED`, never “mostly works”.

## 18. `her_debug` Superloop controller

The persisted campaign driver is the
[`her_debug`](../superloops/templates/her_debug/README.md) Superloop template.
`lin_yueru@HASHI2` owns the controller and final verification; `ajiao@HASHI2`
owns each dispatched test-and-repair packet.

Its `/nudge` is controller liveness, not an Ajiao watchdog. It belongs to
`lin_yueru` and wakes the controller to inspect state, replies, evidence, and
the next action. While Ajiao is running, a nudge may record progress and plan
the next check but must not stop, cancel, restart, reassign, or duplicate her
active packet. A failed or incomplete Ajiao response is preserved and followed
up after her running state clears; it is never allowed to end the campaign
silently.

The template persists exactly two terminal results:

- `PASSED` after every required Flash and Pro combination, effort, scenario,
  ancillary suite, defect repair, regression, and same-candidate gate succeeds;
- `BLOCKED_FUNDS` after insufficient funds are confirmed on either required
  live route and all completed/unrun work is reconciled.

All other failures remain explicit work, waits, or blockers. They never unlock
another API/model and never disable controller follow-up.

## 19. Post-plan Habit and multimedia gates

The following features landed after the original matrix was designed. They are
mandatory additions for any release candidate that includes them; passing the
48 provider/model/mode/effort cells does not imply these gates passed.

### 19.1 Session-mode correction

- retain the deterministic regression proving fixed HER captures one session and
  sends only the next incremental prompt on turn two;
- prove Flex, Wrapper, Audit, and Dual Brain full-context turns do not pass
  `--resume` or load an old HER internal conversation;
- verify mode switches, provider/model changes, `/new`, `/retry`, `/stop`, and
  process restart never cross agent or model session identity;
- rerun the fixed/flex twins across both providers after the correction.

### 19.2 Habit ownership

- retain the adapter-direct JSON `/habit` path as the authoritative owner and
  verify `HERAdapter.habit_pipeline_owner` remains `adapter`;
- prove one eligible foreground run gets exactly one intended Planning
  injection and one Meditation owner;
- cover no-change, create, update, delete, invalid output, timeout, restart
  replay, recoverable reset, audit, and Verbose notification behavior;
- prove non-HER and ephemeral calls neither read nor write the Habit store.

### 19.3 Media and multimodal

- receive a real Telegram image and prove the provider receives pixels, not a
  path or base64 text;
- read a mixed text/scanned PDF and prove bounded text plus rendered-page
  ordering;
- normalize and transcribe real audio, and record a truthful failure when the
  safe transcription route is unavailable;
- exercise canonical MCP image content and every enabled legacy screenshot
  family, including parallel tool-result ordering and `isError` handling;
- inspect Gateway, tool-audit, session, and Habit records for base64, private
  prompt, path-escape, and credential leakage;
- repeat after `/reboot min` before allowing any wider `/reboot max` rollout.

The current artifact identity and unresolved release boundary are recorded in
[HASHI_UNRELEASED_CHECKPOINT_2026-08-13.md](HASHI_UNRELEASED_CHECKPOINT_2026-08-13.md).
