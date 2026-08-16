# HER Conversation Continuity, Planning and Release-Certification Fix Plan

Status: implemented and fully certified; Momo ordinary/FIFO/MCP and r2 Ultra canaries passed;
controlled rollout pending explicit authorization
Scope: HASHI1 Python runtime, native HER runtime, scheduler bridge and HER packaging
Supersedes: the earlier conversation-continuity-only version of this document
Primary incidents: Sunny continuity regression and Lily High-effort planning loop, 2026-08-16
Target release: a newly certified `0.1.0-hashi.22`; rejected `.21` must remain inactive

## 1. Executive decision

Treat the current failures as one contract repair with four coordinated parts:

1. restore one authoritative, ordered user conversation while cron execution remains isolated;
2. make TaskFrame planning/review bounded, evidence-monotonic and unable to invent new
   authorization restrictions;
3. separate internal planning state from user-visible acknowledgement/commentary and make
   exact event delivery idempotent;
4. rebuild from a certified HER source lineage and make certification a non-bypassable
   prerequisite to manifest promotion.

Do not load `.21`. Do not blindly port `.21` onto `.20`. First identify the exact streaming
delta that is still needed, then reimplement or cherry-pick only that delta onto a legitimate
descendant of the certified `.20` source line. Package the complete repair as `.22` for both
Linux and Windows from the same source commit.

Zelda's logging, monotonic-clock and transport-audit work is a separate change. It landed during
this audit as `1b5ac744` (`fix(her): preserve commentary audit and monotonic timing`) and is the
recorded Python baseline for this plan. Do not overwrite or silently absorb those edits.

## 2. Evidence from the currently loaded runtime

### 2.1 Loaded code and binary

- HASHI Python runtime was hot-reloaded from the current HASHI1 checkout.
- The actual native executable selected at Lily startup was the certified
  `0.1.0-hashi.20` Linux package.
- Its embedded source identity is `5ed5b30ef2ab0f80ab6d4fd08a1b7b64e77faf05`.
- `.21` was rejected and is not loaded.

Therefore the latest Lily failure is real in `.20` plus the current Python integration; it is
not explained by a stale reboot and is not caused by `.21` being active.

### 2.2 Lily High-effort incident

One High-effort request ran for about 520 seconds and was stopped manually before it reran the
requested task or produced a final answer. Its raw HER stream contained:

```text
16 provider/model turns
16 tool executions
11 TaskPlan events: 1 initial + 10 critical_review
13 planning control events
8,039 thinking_delta fragments
411,603 input tokens + 10,485 output tokens
```

The first six successful inspection tools caused the expected periodic checkpoint. A failed
native `read_file`, the switch to `bash`, and later tool activity then caused near-continuous
critical review. Ten non-initial TaskPlans were converted into user commentary. Several reused
the same opening acknowledgement, and the exact event identity
`req-0001:commentary:critical_review:faf0dbc728b85baa` was accepted and delivered twice.

The TaskFrames did not advance with the evidence already collected:

- `completed` remained empty;
- `remaining_work` kept describing work that had already been inspected;
- the plan introduced a new restriction against using `bash`, even though the user's request
  already authorized the one safe rerun and `bash` was the available execution route;
- generic descriptions such as `任务配置与入口读取工具` were stored in `planned_tools`, while
  actual tools were `bash`, `read_file`, `TaskList` and `CronList`;
- each mismatch was treated as divergence and could provoke still more review.

This was not useful extra diligence. High effort amplified plan churn, duplicated presentation,
authorization hesitation and model cost without increasing verified progress.

### 2.3 Medium is also affected

Native planning is enabled at `medium`, `high`, `xhigh`, `max` and `max+`. Only `low` disables
it. The Python cadence controller currently forwards source-classified commentary even when
its effort-level `progress_enabled` flag is false.

A live Medium request on 2026-08-16 delivered six task-commentary messages and ten required
control messages in one request. This proves the issue is not confined to High. Medium lacks
the High self-review loop, so its amplification is normally smaller, but replan commentary,
format-fallback leakage and token waste still occur.

### 2.4 Tool authority is ambiguous

The failing Lily run called native `CronList`. It returned:

```text
count: 0
crons: []
```

HASHI Scheduler did have Lily cron jobs. The native tool queried the HER/Claw cron store, not
the authoritative HASHI Scheduler. A tool with an unqualified name therefore supplied a
plausible but wrong fact and pushed the agent into further investigation and replanning.

Native `read_file` also enforced the HER workspace boundary while `bash` could inspect the
HASHI repository. The model was penalized for switching between two tools that exposed
different filesystem scopes even though the original plan used only generic tool descriptions.

### 2.5 Existing tests certify incorrect behaviour

Current tests explicitly preserve several wrong expectations:

- busy persistent session + new direct user text becomes `isolated_per_run`;
- a replan `TaskFrame.acknowledgement` is accepted as model-authored commentary;
- Medium trusts and forwards any source-classified commentary;
- routing the same exact `event_id` twice presents it twice;
- internal planning `invalid_format` and fallback events can become mandatory user controls.

The tests pass because they encode the regression, not because the runtime behaviour is right.

## 3. Why `.21` was invalid and how release control failed

### 3.1 Wrong source lineage

The `.21` package declared source commit
`45ff403aeecd3ea9f72279be488f60a2df0f88df`. That commit:

- is dated 2026-05-25 and titled `Stream actual provider reasoning details`;
- belongs to the old `main` source line;
- is not a descendant of the declared certified upstream commit
  `4ea31c1bc91c4e9bcbd67d51c550c01e127e6d0d`;
- reports native runtime version `0.1.0`, while `.20` reports the later HASHI-derived runtime;
- does not contain the `.20` TaskFrame, independent-review, semantic-compaction and session
  contract visible in the certified binary.

The existing `scripts/verify_her_certification.py` ancestry check would have rejected this
source. The artifact was nevertheless packaged and selected in a commit before that complete
certification was made an enforced promotion step.

### 3.2 Structural tests were insufficient

The normal Python packaging tests only established that:

```text
adapter version == manifest version == baseline version
manifest source == baseline source
binary digest == manifest digest
```

If manifest, baseline and adapter constants are changed together to the same wrong values,
those tests still pass. They do not prove ancestry, source availability, Rust tests, Clippy,
binary features, JSONL protocol compatibility or HASHI execution/session behaviour.

### 3.3 Mixed-platform release metadata was unsafe

The `.21` manifest used a top-level `.21` version and source for Linux while retaining the
Windows `.20` binary. One release record therefore described two different implementations.
Even if the Linux source had been valid, the release identity and cross-platform contract were
ambiguous.

### 3.4 Reproducibility is currently incomplete

The scanned native source checkouts do not currently contain the `.20` source object
`5ed5b30e...`, even though the packaged binary embeds it. Before `.22` work begins, recover the
exact certified `.20` source from an authoritative remote, tag, bundle or archive and preserve
it durably. A binary plus a manifest is not a reproducible source release.

## 4. Authoritative runtime invariants

### 4.1 One visible conversation

HER may use multiple execution sessions, but one Agent/chat has one authoritative visible
conversation ordered by actual user receipt and successful assistant delivery.

- interactive user turns always use the persistent conversation and run FIFO;
- scheduler/cron executions remain isolated;
- a cron message successfully delivered to the user enters the main visible timeline at that
  delivery position;
- a user message binds only to context already delivered when it was enqueued;
- later cron output cannot retroactively capture an earlier user message;
- old receipts, recovery batches and Habits cannot steal a new short reply.

### 4.2 Planning is internal control state

`TaskFrame` is not a stream of Persona messages.

- `acknowledgement` is an initial acknowledgement and may be delivered at most once;
- a non-initial TaskPlan is technical/audit state by default;
- only a dedicated, explicitly authored progress field/event may become user commentary;
- unchanged intent, repeated acknowledgement and internal review verdicts are not progress;
- planning errors remain technical unless the user must actually decide or grant authority.

### 4.3 Authorization is immutable across replan

Create a request-scoped authorization envelope from the newest user turn and actual runtime
permission results. Replanning may narrow a dangerous action when a real policy/permission
boundary is encountered, but it cannot invent a new approval requirement from reviewer prose,
Habit text, failed parsing, tool naming mismatch or caution alone.

A proposed replan must be rejected or normalized if it:

- changes the authoritative goal;
- adds a `do_not_do` restriction unsupported by the user, policy or a real permission denial;
- turns a previously authorized necessary action into a new confirmation blocker;
- treats historical context, receipt text or a reviewer suggestion as new authority.

### 4.4 Evidence and progress are monotonic

Across replans:

- verified `completed` work cannot disappear;
- successful tool results cannot be described as not having run;
- failures and unresolved items cannot silently disappear;
- the next action must be consistent with the execution ledger;
- an unchanged frame cannot reset review cadence or create a new user update.

### 4.5 Effort is a capability ceiling

Higher effort allows deeper task-matched verification; it does not require more messages,
more review rounds or more hesitation. Review must stop once it has no new evidence, finding,
risk transition or plan change to contribute.

### 4.6 Delivery is idempotent by event identity

Within one request, one user-visible `event_id` may be accepted successfully at most once.
This is a small in-memory idempotency set around the existing presenter, not a second durable
delivery/outbox system.

- persist/audit every raw occurrence;
- mark an ID delivered only after the existing presenter accepts it;
- allow retry after an exception or explicit non-acceptance;
- suppress a later replay of an already accepted ID;
- never globally deduplicate by prose: distinct IDs with the same text may be legitimate;
- prevent repeated planning prose at the producer/materiality layer.

### 4.7 Tool names identify one authority

Every scheduler, filesystem and task tool exposed to HER must be namespaced or mapped to one
authoritative store. A model must be able to distinguish, for example:

```text
hashi_scheduler_list
claw_local_cron_list
hashi_file_read
claw_workspace_read
```

Do not expose the native cron tools to HASHI agents under unqualified names when HASHI
Scheduler is the intended authority.

## 5. Effort-level risk and required behaviour

| Effort | Native planning/review | Current risk | Required result |
| --- | --- | --- | --- |
| `low` | planning off | not vulnerable to the TaskFrame replan loop; still shares conversation, tool, control and delivery bugs | no TaskPlan or review calls; normal execution and one final answer |
| `medium` | adaptive planning; no review loop | confirmed live replan commentary/control leakage; periodic replans can waste tokens | one acknowledgement maximum; internal replans only on material evidence/cadence; no review loop |
| `high` | adaptive planning + optional self-review | confirmed strongest current failure: review churn, authorization drift and repeated commentary | one review per new material trigger; bounded cadence; evidence-monotonic replan; guaranteed finalization |
| `xhigh` | adaptive planning + optional independent read-only review | same TaskFrame/presentation defects plus planning-review revision paths | hard revision cap; advisory review cannot create commentary or block the primary answer |
| `max` | deeper independent review | structurally vulnerable to every lower-level defect and more review/tool round trips | review only selected targets; no repeated inspection after unchanged evidence |
| `max+` | max review + one optional isolated exact test rerun | greatest amplification surface across planning, checkpoints, review and test rerun | one exact planned rerun maximum; every stage bounded and non-duplicating |
| `ultra` | Python orchestrator; native planning disabled inside workers | exact native TaskFrame loop is not nested, but shared router/continuity bugs and outer lifecycle replays still apply | keep inner `CLAW_TASK_PLANNING=0`; test outer commentary IDs, retries and final assembly independently |

The absence of a newly reproduced `xhigh/max/max+` duplicate in this scan is not evidence of
safety. Their code paths consume the same TaskPlan mapping and add more review phases. They
must be covered by a deterministic effort matrix before `.22` certification.

## 6. Native HER changes

### 6.1 Restore a certified source base

Before editing native code:

1. recover and verify the exact `.20` source commit `5ed5b30e...`;
2. prove `4ea31c1b...` is its ancestor;
3. create a protected `.22` branch/tag from that source;
4. retain an authoritative Git remote and an offline bundle/archive containing every release
   commit;
5. compare `.21` against the certified line and list the exact streaming delta, without
   adopting its old runtime wholesale.

### 6.2 Separate TaskFrame from commentary

Change the native JSONL contract so the roles are explicit:

```text
TaskAcknowledgement -> one initial Persona acknowledgement
TaskPlan            -> internal technical state only
TaskCommentary      -> optional Persona-authored material progress update
```

Do not overload `TaskFrame.acknowledgement` as commentary. Add an explicit commentary field or
event only if the planning/provider call actually authored a current progress message. Give
TaskPlan/TaskCommentary a monotonic revision and stable source event identity.

### 6.3 Validate replan transitions

Add a transition validator between the previous frame, immutable authorization envelope and
current execution ledger. It must enforce:

- exact active-goal preservation;
- authorization-boundary preservation;
- monotonic completed/evidence/failure state;
- no claim that zero tools ran when the ledger is non-empty;
- remaining work and next action consistent with current evidence;
- exact/canonical tool capabilities drawn from the actual registry;
- no generic prose in fields interpreted as tool identifiers.

If a replan response is malformed or violates these invariants, keep the last valid frame,
record one technical fallback, and continue execution. Do not emit the preserved
acknowledgement again and do not trigger another checkpoint solely because parsing failed.

### 6.4 Bound review and detect plan livelock

Review triggers must be keyed by material condition, not raw tool occurrence.

- canonicalize aliases before testing plan divergence;
- consume each unique divergence/failure/permission trigger once unless its evidence changes;
- enforce an effort-specific minimum review interval and total review budget;
- do not allow a model-selected interval of `1` to turn every successful tool into review;
- do not reset cadence for an unchanged frame;
- stop replanning after repeated no-change frames and return control to primary execution;
- reserve finalization independently so review cannot consume the ability to answer;
- enforce declared stop conditions when the deliverable/evidence is sufficient.

The loop detector should use structured deltas, not text similarity alone. A checkpoint is
non-progressing when it adds no completed work, evidence, failure resolution, material risk
change, next-action change or authorized strategy change.

### 6.5 Make internal control failures non-user-facing

Native `control_invocation` events should declare whether user action is required. Invalid
TaskFrame JSON, format retries, reviewer parse failures and preserved-frame fallback are audit
events, not required user controls. Only real permission requests, missing user decisions or
terminal actionable blockers use the mandatory control lane.

## 7. HASHI Python integration changes

### 7.1 Correct TaskPlan mapping

Update `adapters/her.py`:

- remove `frame.acknowledgement` from non-initial commentary fallback;
- map TaskPlan to technical planning telemetry only;
- accept only the new explicit TaskCommentary event/field as user commentary;
- preserve origin, phase, revision and source event ID without inventing Persona text;
- classify internal `error` fields by actionability instead of making every error mandatory.

### 7.2 Enforce commentary cadence and materiality

The cadence controller currently forwards source commentary immediately and bypasses its own
90/180/300-second cadence. Change it to:

- deliver the initial acknowledgement immediately once;
- deliver a blocking/user-decision message immediately;
- coalesce normal progress to the newest material update within the configured cadence;
- suppress unchanged progress revisions;
- retain every suppressed event in technical audit with the reason;
- never generate Persona prose in the cadence controller.

Effort controls whether the native runtime generates a progress event; the presentation layer
still enforces identity, materiality, idempotency and user toggles.

### 7.3 Add exact-ID idempotency

Update `orchestrator/her_message_router.py` with a request-local set of successfully presented
user-visible/control event IDs. Keep persistence before filtering. Do not mark delivery on
presenter failure. Do not deduplicate separate scheduled events or separate requests by text.

### 7.4 Control reasoning-fragment overhead

The Lily request produced 8,039 `thinking_delta` records. Preserve exact provider fragments for
the reasoning contract, but buffer/coalesce transport and ordinary INFO logging so one-character
fragments do not create thousands of accepted-delivery log lines. Audit may retain bounded
raw/chunk evidence without changing reasoning bytes or exposing redacted content.

## 8. Tool Gateway and scheduler changes

1. Hide native Claw cron tools from normal HASHI agent tool lists unless explicitly requested
   under a `claw_local_*` namespace.
2. Expose read-only HASHI Scheduler list/status/run-history tools through the Tool Gateway.
3. Expose any authorized single-task rerun through the HASHI Scheduler API rather than asking
   a model to guess a shell command.
4. Build `planned_tools` from canonical registry capabilities. Keep human-readable strategy in
   `planned_actions`, not `planned_tools`.
5. Align file-read documentation and scope. If native workspace reads and HASHI repository
   reads differ, namespace them and describe the boundary to the model.
6. Treat a tool from the same canonical capability as planned even when its provider/MCP alias
   differs, unless crossing that namespace changes authority or side effects.

## 9. Conversation continuity changes

### 9.1 Direct session routing

Update `orchestrator/runtime_pipeline.py`:

```text
interactive user source -> persistent
scheduler/cron source    -> isolated_per_run
explicit isolated resume -> isolated_resume
```

Remove `persistent_session_busy -> isolated_per_run` for direct messages. Let adjacent direct
turns wait on the existing persistent-session lock and execute FIFO.

### 9.2 Direct delivery ordering

Use a small per-chat sequence/lock around the existing detached completion and final-send path.
Two direct turns may wait in the background, but their persistent transcript commits and final
deliveries cannot reverse order. This is ordering protection, not a new outbox.

### 9.3 Enqueue-time reply target

When an interactive message is enqueued, capture the latest eligible already-delivered
isolated turn for that chat. At execution time, only that exact snapshot may be considered.
Do not let a later cron delivery replace it and do not search older receipts for a token match.

### 9.4 Cross-session receipt simplification

Use receipts for exact isolated context/session resume and audit only. A newer delivered
interaction supersedes the prior active target. Consuming, skipping or terminally failing a
bound reply resolves that exact receipt and cannot reactivate another historical receipt.

### 9.5 Recovery commands remain explicit

Only narrow commands such as these authorize recovery:

```text
全部补跑
全部跳过
task-id=N
```

Bare `继续`, `可以`, `ok`, `yes` and `一起做完` remain ordinary conversation. Recovery execution
or skip must immediately persist `resolved` or `resolved_with_errors` and disappear from pending
context after restart.

## 10. Test corrections and additions

### 10.1 Replace wrong Python expectations

Replace tests that currently require:

```text
persistent busy + direct text -> isolated_per_run
TaskFrame acknowledgement     -> replan commentary
Medium source commentary      -> unconditional presentation
same event_id routed twice    -> presented twice
```

Required expectations:

```text
persistent busy + direct text -> persistent FIFO
TaskFrame acknowledgement     -> initial delivery once only
TaskPlan                       -> technical only
explicit TaskCommentary        -> effort/materiality/cadence controlled
same accepted event_id twice   -> one presentation, two audited occurrences
```

### 10.2 Native Rust planning tests

Add deterministic tests for:

- acknowledgement emitted once across initial, replan, critical review and finalization review;
- unchanged TaskFrame produces no TaskCommentary;
- explicit material TaskCommentary carries a new revision/event identity;
- completed/evidence fields cannot regress;
- non-empty tool ledger cannot become `no tools run`;
- replan cannot add unsupported authorization restrictions;
- a real permission denial may narrow the plan and request user action once;
- generic planned-tool prose is rejected or kept outside the tool-ID field;
- aliases canonicalize without false divergence;
- repeated same-capability divergence/review is consumed once;
- malformed replan preserves the old frame and continues without commentary;
- review/no-change loop breaker hands control back to execution;
- finalization remains reachable after the review budget is spent.

### 10.3 Effort matrix

For each of `low`, `medium`, `high`, `xhigh`, `max`, `max+` and `ultra`, cover:

1. direct answer;
2. six- and twelve-tool successful task;
3. one failed tool followed by a successful alternate;
4. one genuine new capability divergence plus repeated use of that capability;
5. invalid initial and invalid replan JSON;
6. enough evidence to satisfy stop conditions;
7. provider returns reasoning but no visible final once;
8. same stream event replayed twice.

Assert bounds on planning calls, review calls, commentary messages and terminal outcome. Do not
assert merely that the task stays below the very large iteration ceiling.

### 10.4 Tool authority tests

- `hashi_scheduler_list` sees the HASHI jobs used by the Agent.
- unqualified/native `CronList` is absent or explicitly identified as local Claw state.
- a zero result from one namespace cannot be presented as proof about the other.
- planned canonical capabilities match actual Gateway aliases.
- native and HASHI filesystem scopes are explicit and tested.

### 10.5 Continuity and race tests

Use explicit barriers rather than sleeps to prove:

- two direct turns serialize and deliver in order;
- cron execution remains isolated;
- cron delivery before user enqueue may become the reply target;
- cron delivery after user enqueue cannot capture that message;
- a newer primary interaction supersedes an old cron target;
- stale `CONTINUE`, recovery and Habit state cannot steal `继续`;
- resolved/consumed state does not reactivate after restart.

### 10.6 Release-certification tests

Add a staged release tool that refuses promotion unless all of these pass:

- source HEAD equals candidate source commit and is clean;
- declared upstream is an ancestor;
- source commit is reachable from a protected tag/remote and included in a retained bundle;
- full Rust workspace tests and pinned Clippy baseline pass;
- Linux and Windows binaries embed the same candidate source and release version;
- binary SHA-256 values match staged metadata;
- executable `version`, `doctor`, `status` and stdin/session/resume smokes pass;
- JSONL contract advertises and emits required TaskAcknowledgement, TaskPlan,
  TaskCommentary/control-actionability, reasoning, tool, usage and terminal events;
- HASHI session, continuity, effort and Tool Gateway integration suites pass;
- the active manifest is changed only after the immutable evidence bundle is complete.

The normal test suite must also reject a manifest that mixes `.21` Linux with `.20` Windows
under one top-level release identity.

## 11. `.22` packaging and promotion workflow

1. Stage candidate source, binaries, metadata and evidence outside the active manifest.
2. Run source certification before copying any candidate binary into the active release tree.
3. Run the complete effort/continuity/tool contract matrix against staged Linux and Windows
   binaries.
4. Produce an immutable evidence record containing source/upstream commits, source bundle hash,
   toolchain versions, test commands/results, binary hashes and platform smokes.
5. Atomically promote manifest, baseline, adapter expected version and both platform entries in
   one reviewed commit.
6. Re-run packaged discovery from a clean HASHI checkout.
7. Canary one non-critical Agent first; verify logs, message counts, final answer and session
   continuity.
8. Roll out to Lily/Sunny only after canary success. Keep `.20` as the rollback target.
9. Retain `.21` only as rejected forensic evidence and ensure no manifest selects it.

## 12. Runtime-state cleanup after code is certified

After `.22` passes and before broad rollout:

1. remove Sunny's incorrect Habit `Short approval = explicit recovery-batch choice`;
2. reconcile the proven stale Sunny recovery batch to its true terminal state without deleting
   history;
3. resolve/supersede polluted active cross-session receipts while preserving audit records;
4. start a clean persistent HER session for Agents affected by the old isolated-turn or planning
   loop behaviour;
5. do not bulk-delete unrelated Habits, receipts, sessions or scheduler history.

## 13. Implementation order

1. Start from Zelda's landed `1b5ac744` logging/clock baseline and preserve its audit contract.
2. Preserve this plan and capture current `.20`, `.21`, live-log and test evidence.
3. Recover the exact `.20` source and create the `.22` source branch.
4. Add failing native and Python regression tests before production changes.
5. Repair native TaskFrame/commentary, authorization/evidence transitions and review bounds.
6. Repair Python mapping, control actionability, cadence and exact-ID idempotency.
7. Repair authoritative Tool Gateway scheduler/filesystem namespaces.
8. Implement persistent direct-turn FIFO, delivery ordering and enqueue-time reply targeting.
9. Repair recovery terminal state and perform targeted Sunny cleanup tooling.
10. Run focused tests, the full HASHI suite and full native Rust certification.
11. Build and certify `.22` for Linux and Windows from the same source commit.
12. Promote only after the staged evidence bundle passes, then canary, `/reboot min`, `/new` for
    polluted sessions, and live mixed direct/cron tests.

## 14. Acceptance criteria

The repair is complete only when all of the following are true:

- direct user messages never become isolated merely because the persistent session is busy;
- adjacent direct messages execute, commit and deliver FIFO;
- cron execution remains isolated while delivered cron output enters the main conversation;
- a short reply targets only context visible at enqueue time;
- acknowledgement is delivered at most once per request;
- a TaskPlan cannot become commentary by reusing `acknowledgement`;
- exact replay of an accepted user-visible event ID cannot be presented twice;
- Medium cannot leak internal replans/format errors as repeated user messages;
- High review adds evidence or a material correction and cannot loop on unchanged plans;
- XHigh/Max/Max+ review and test reruns remain within explicit hard bounds;
- Ultra keeps native worker planning disabled and does not replay outer lifecycle commentary;
- replans preserve user authorization and all verified execution evidence;
- internal planning/reviewer format failures remain audit/technical events;
- HER uses the authoritative HASHI scheduler/tool namespace;
- every effort scenario returns a final answer or one accurate terminal blocker within its
  planning/review budget;
- `.22` source is reproducible, ancestry-certified and retained;
- Linux and Windows `.22` binaries share one source/version contract;
- focused, full HASHI, full Rust and live canary tests pass;
- Aptenra remains unchanged.

## 15. Immediate freeze rule

Until implementation starts under this plan:

- keep `.20` selected;
- do not activate `.21`;
- do not apply another runtime fix before implementation begins under this unified plan;
- allow documentation/evidence updates only;
- if a critical live task encounters the loop, stop it and switch effort to `low` only as an
  explicit temporary operational mitigation, not as the code fix.

## 16. `.22` completion record (2026-08-16)

The plan was implemented on the certified `.20` descendant and promoted as
`0.1.0-hashi.22`. The recovered `.21` multimodal changes are limited to native commits
`26ec36f` and `9c410a1`; the invalid `.21` source line remains inactive.

The first `.22` candidate passed offline certification and ordinary Momo FIFO/resume/MCP
canaries, but the live Ultra canary correctly rejected it after three independent CLI workers
exposed a cross-process managed-session filename collision. That candidate tag is retained for
audit. Native commit `246b04e9fa28ef0b6f74c2d924ab3697b95197bd` adds process identity to managed session and
atomic-temp paths while retaining legacy session-ID timestamp parsing. The corrected candidate
is tagged `her-0.1.0-hashi.22-certified-r2` and packaged by HASHI commit `86bfe45a`.

Final certification and canary evidence:

- Rust workspace: 1,480 passed, 0 failed, 1 ignored;
- Clippy: exactly 40 pinned upstream diagnostics and no new diagnostic;
- HASHI Python/Veritas: 2,233 passed, 0 failed, 2 skipped, 23 known warnings;
- Windows native smokes: version, doctor, status and prompt help all passed;
- Linux SHA-256: `e6c88b9dd37c9191f9aad0df9fd0cf9bbeb4365778a10153a48b4cf752096c91`;
- Windows SHA-256: `cd127b283d0bb8aa5db9d1863a617bb84a2c8cd0174ed305c72c5f97b294724d`;
- Momo r2 Ultra: `ultra-20260816-181432-55931c9a6d77`, three required workers completed
  on their first attempt with unique PID-namespaced session IDs, zero run errors and terminal
  marker `MOMO22_R2_ULTRA_PASS`.

Only Momo received the authorized `/reboot min`. No broad HER Agent reload or Sunny runtime
cleanup was performed, and Aptenra remained unchanged.
