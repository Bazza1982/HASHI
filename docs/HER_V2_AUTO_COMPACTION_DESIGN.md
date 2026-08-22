# HER v2 Auto Compaction Design

| Field | Value |
|---|---|
| Status | Approved design; implementation not started |
| Date | 2026-08-22 |
| Scope | HASHI-owned context capacity management for HER v2 |
| Governing specification | [HER v2 product and technical design](HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md) |
| Runtime activation | Any later functional implementation requires separately authorised `/reboot`; this design performs no reload |

## 1. Objective

HER v2 must be able to continue useful work when accumulated conversation,
stage evidence, or a long request-local provider loop approaches a selected
model's physical context capacity. Auto Compact converts only eligible older
context into a validated continuity capsule, preserves protected material
verbatim, retains the complete raw source, and then continues the same logical
request.

Auto Compact is capacity management, not an execution budget. It must never
introduce a turn, stage, tool-round, call-count, cumulative-token, or ordinary
provider-attempt ceiling.

The design has five primary goals:

1. select a compaction model by explicit HER v2 configuration rather than
   assuming that Quick/Fast can read the source material;
2. preserve user authority, current work, side-effect truth, and exact evidence
   while reducing only replaceable historical context;
3. work with stateless providers without inventing provider sessions;
4. leave existing OpenRouter and DeepSeek long tool loops intact in the first
   implementation; and
5. bound the isolated, tool-free compaction maintenance call with an explicitly
   authorised Tier 2 or Tier 3 watchdog without leaking that deadline into
   normal HER execution.

## 2. Locked decisions

The following decisions are authoritative for this feature:

1. **Quick/Fast is not the compactor.** HER core must not select the Quick or
   Fast model merely because it is expected to be cheap or fast. Its context
   window may be smaller than the material that needs compaction.
2. **Compact is an independently configurable route.** `/model` exposes its
   provider, model, provider reasoning, and timeout tier. The route may inherit
   Pro for backward-compatible migration or be configured explicitly, including
   to a separately granted future local model.
3. **Gemini remains stateless.** Neither ordinary HER work nor compaction may
   add resume/session semantics to `gemini-cli`. HASHI owns the effective
   context snapshot and continuity capsule.
4. **OpenRouter and DeepSeek retain their long tool loops.** Initial delivery
   must not split, cap, or replace their current request-local model/tool loop.
   An in-loop safe-boundary hook is a later capability-gated phase.
5. **Semantic compaction starts at Tier 2.** It requires reasoning and therefore
   must never use Tier 1. A remote reasoning-capable compactor normally uses
   Tier 2; a declared local or slow compactor may use Tier 3. Selection is
   configuration/capability driven, never an engine-name allowlist.
6. **The deadline exception is compactor-only.** Ordinary HER stages, Persona,
   learning, sub-agents, provider adapters, and tool loops retain the governing
   no-attempt-deadline invariant.
7. **Failure is atomic and truthful.** A failed or cancelled compaction leaves
   active context unchanged. Raw source history is never destroyed after a
   successful compaction.

## 3. Terms and authority

| Term | Meaning |
|---|---|
| Raw context | Immutable HASHI transcript, runtime evidence, and source records before compaction |
| Effective context | The exact context assembled for one target model call |
| Protected set | Context that must remain verbatim and cannot be replaced by a model summary |
| Eligible set | Older context that policy permits Auto Compact to replace |
| Continuity capsule | Validated, typed semantic summary of one exact eligible source range |
| Capacity profile | Provider/model-declared context capacity and tokenizer provenance |
| Compact route | The tool-free provider/model/reasoning/timeout configuration used only for semantic compaction |
| Soft pressure | Projected request fits but crosses the configured proactive watermark |
| Hard pressure | The protected set plus required request envelope cannot safely fit the target model, or the provider returns a typed context-capacity rejection |

The current user request remains the only active user authority. A continuity
capsule is labelled historical background and can never become a system,
developer, user, plan, permission, or tool message. Model-authored capsule text
is advisory data; deterministic runtime state remains authoritative.

## 4. Ownership and architecture

Auto Compact is a HASHI-owned context service called by HER v2. It is not a
principal lifecycle stage and cannot classify, plan, execute tools, replan,
review, finalise, contact the user, or change terminal state.

```text
HASHI context assembler
        |
        v
typed context envelope -----> target provider capacity profile
        |                              |
        +------ ContextCapacityController
                         |
             no pressure | soft/hard pressure
                         |          |
                         v          v
                  target call   freeze eligible prefix
                                      |
                                      v
                              Compact route (no tools)
                                      |
                                      v
                           validate + atomic commit
                                      |
                                      v
                                  target call
```

### 4.1 HASHI responsibilities

HASHI owns:

- typed context assembly and segment authority labels;
- provider/model capacity metadata and exact grants;
- token measurement or conservative estimation provenance;
- protected/eligible selection;
- raw-source persistence, capsule persistence, and compare-and-swap commit;
- compactor process cancellation and cleanup;
- audit, redaction, telemetry, and `/model` configuration;
- optional future safe-boundary hooks inside request-local API tool loops.

### 4.2 HER v2 responsibilities

HER v2 supplies the current immutable goal, classification, plan, execution
state, evidence references, and lifecycle position to the context service. It
consumes either the unchanged effective context or a successfully committed
replacement. HER v2 never interprets a failed compaction as completed user
work and never counts compaction telemetry as a Replan, Review, or execution
unit. A newly validated chunk that covers previously uncompacted source and
strictly reduces it may count as substantive capacity progress; start events,
heartbeats, retries, repeated coverage, and no-shrink output do not.

### 4.3 Provider adapter responsibilities

Adapters declare capabilities rather than being selected by engine name:

- context-window size and its source when known;
- tokenizer/counting support when available;
- typed context-capacity errors;
- whether a safe pre-request boundary is exposed inside a long tool loop;
- whether the route is local/slow for timeout-tier resolution;
- enforceable isolation of the dedicated Compact system prompt from the Agent
  Persona and ordinary execution prompt;
- enforceable tool disablement for the Compact route.

An adapter that cannot prove both prompt isolation and tool disablement is
ineligible as a compactor even if the same adapter is valid for tool-enabled
Execution.

## 5. `/model` configuration contract

HER v2 continues to expose Quick and Pro as reusable task-route slots. Auto
Compact adds an independent Compact route; it is not a third task model slot
and it is not silently assigned to Simple Execution.

The persisted conceptual state is:

```json
{
  "her_v2_configuration": {
    "provider": "deepseek-api",
    "fast_model": "quick-model",
    "pro_model": "pro-model",
    "compact": {
      "mode": "inherit_pro",
      "provider": null,
      "model": null,
      "reasoning": "inherit_pro",
      "timeout_tier": "auto"
    }
  }
}
```

`inherit_pro` is the migration default. It resolves the current Pro provider,
model, and provider-specific reasoning at invocation time, but remains visibly
labelled as inheritance. This avoids Quick/Fast and generic reasoning-name
assumptions while keeping existing Agent configuration valid. An explicit
Compact route persists its own exact provider/model grant and provider-specific
reasoning, and does not change when `/provider` changes Quick and Pro.

`/model` must expose:

- current Compact mode: `inherit_pro`, `explicit`, or `off`;
- effective provider and model;
- declared context window and metadata provenance, or `unknown`;
- provider reasoning;
- timeout tier: `tier_2`, `tier_3`, or `auto`;
- whether data crosses from the main provider to a different Compact provider;
- eligibility/lock reason when prompt isolation or tool disablement cannot be
  proved, semantic reasoning is unavailable, capacity is insufficient, or the
  exact provider/model grant is absent.

The command/UI flow must allow the user to inspect and change Compact without
changing Quick, Pro, any task route, HER effort, or provider reasoning on other
routes. Selecting a different Compact provider requires explicit confirmation
that eligible historical context will be sent to that already-granted
provider. Secrets and non-exportable data remain excluded by privacy policy.

Provider selection rules are:

1. `/provider` continues to select the main provider carrying Quick and Pro.
2. When Compact is `inherit_pro`, it follows the resulting Pro route.
3. When Compact is explicit, `/provider` preserves it and revalidates its exact
   grant; it never silently rewrites it.
4. Removing a grant locks Auto Compact and preserves the last configuration for
   inspection; it does not fall back to Quick.
5. `auto` timeout selection resolves Tier 3 only from a declared local/slow
   capability. Otherwise it resolves Tier 2. No provider/model name table lives
   in HER core.

Context-window metadata belongs to the exact model grant or a provider
capability resolver. A manually configured override must be labelled as such.
A tokenizer or conservative estimator may measure input size, but it cannot
substitute for a missing capacity denominator. Unknown target capacity disables
proactive ratio triggering; a later typed provider-capacity rejection may still
trigger recovery when Compact capacity is known. Unknown Compact capacity makes
that route ineligible until capability discovery or a labelled operator
override supplies it. The runtime must not invent a context limit from a model
name.

## 6. Context envelope and protection rules

Auto Compact must operate on typed segments rather than parsing one flat prompt
with substring or regular-expression heuristics. Each segment carries at least:

```text
segment_id
kind
authority
created_at / sequence
content_ref or inline content
compactable
token_count or estimate
source_hash
```

### 6.1 Always protected verbatim

The protected set includes:

- active system, developer, instance-global, Agent-local, safety, privacy,
  permission, workzone, and tool-schema instructions;
- the current authoritative user request and any bound `/steer`, continuation,
  confirmation, or cancellation context;
- immutable Triage classification and active goal;
- the active plan or Replan, current lifecycle state, success criteria, and
  unresolved user question;
- the current turn and a configured recent complete-dialogue guard;
- any open, running, failed-with-unknown-side-effects, or not-yet-audited tool
  transaction;
- exact side-effect receipts, idempotency keys, unresolved failure facts, and
  evidence references required for safe continuation;
- the current Memory+ card and cross-session binding records, which are already
  separately bounded;
- media/cache references needed by the active request, but never raw base64
  payloads merely to make them compactable.

If the protected set alone cannot fit the target model, HER returns a stable
capacity failure. It must not summarise authority, truncate the current request,
drop tool truth, silently switch provider/model, or pretend that compaction
succeeded.

### 6.2 Eligible material

Policy may compact:

- older completed user/assistant exchanges outside the recent guard;
- completed tool outputs whose exact raw evidence is durably addressable;
- superseded plan versions while retaining their identifiers and outcomes;
- repeated verbose telemetry and redundant provider presentation text;
- older completed sub-agent reports after their evidence references and
  limitations have been preserved.

Eligibility is decided before the model call. The compactor cannot request a
larger source range or decide that a protected segment is expendable.

### 6.3 Least-privilege compactor view

The Compact call receives only the selected eligible chunk, stable source and
evidence identifiers, the capsule schema, and a minimal explicitly marked
relevance header derived from the active goal/success criteria when provider
privacy policy permits it. It does not receive the Agent Persona, unrelated
system/developer rules, tool schemas, secrets, raw credentials, permission
tokens, open tool arguments/results, or the complete protected set merely
because those values remain in the eventual target context.

Source text is delimited as untrusted quoted history. The Compact route has no
Tool Registry and cannot request additional context. When Compact crosses to a
different provider, HASHI applies the destination provider's export/privacy
policy to both the eligible source and relevance header before invocation.

## 7. Capacity detection and trigger policy

Before a target provider request, the controller measures the exact serialized
request when the adapter exposes a tokenizer. Otherwise it records the named
conservative estimator used. Capacity accounting includes the target system
prompt, tool schemas, protected context, eligible context, and provider-required
response headroom. Response headroom is planning space, not a `max_tokens`
execution ceiling.

The policy has two configurable watermarks expressed against the target
model's declared physical capacity:

- a high watermark that triggers proactive compaction while the original still
  fits; and
- a lower post-compaction target that prevents immediate retriggering.

Recommended initial defaults are `0.80` and `0.60`. They are deployment-varying
capacity policy, not HER effort and not cumulative token budgets. Tests must use
small synthetic capacities instead of enshrining these defaults as universal
model facts.

The controller may also trigger after a typed provider context-capacity error.
That recovery is safe only when the failed request produced no new tool call or
side effect. A provider's generic HTTP 400 is not automatically a capacity
signal; the adapter must classify it from a stable provider field or a narrowly
reviewed compatibility parser.

## 8. Compaction algorithm

### 8.1 Freeze and select

1. Freeze the request-local typed context snapshot and its source digest.
2. Calculate target occupancy and required reduction.
3. Select the oldest contiguous eligible prefix sufficient to reach the lower
   watermark. Do not take protected segments merely to reduce call count.
4. Record the selected segment IDs, hashes, and raw archive reference before
   invoking a model.

### 8.2 Capacity-aware hierarchical compaction

The selected Compact model is not assumed to fit the complete source in one
call. The controller partitions the eligible prefix at semantic record
boundaries so that each request fits the Compact route's declared context:

- do not split a user/assistant exchange unnecessarily;
- preserve every tool-call/result relationship; when one older completed result
  is itself too large, page only its payload at deterministic byte/token
  boundaries while repeating the stable call ID, result ID, and evidence
  reference on every page;
- retain chronological order and stable source IDs;
- compact chunks independently, then merge their capsules;
- recursively merge only while the candidate becomes strictly smaller and all
  source coverage remains exact.

There is no fixed chunk-count or merge-round ceiling. A deterministic no-shrink
check stops an ineffective cycle: if a valid candidate does not reduce the
measured payload, the operation fails atomically instead of looping. The
configured meaningful-progress idle policy and explicit cancellation remain in
force. Only newly validated source coverage with strict size reduction is
meaningful capacity progress; telemetry, retries, and repeated/no-op work are
not.

### 8.3 Capsule schema

Each model call returns one structured capsule containing:

- exact source segment IDs and source-range digest;
- active historical goals and resolved references;
- decisions and constraints still relevant to later work;
- completed work and verification truth;
- unresolved work, questions, failures, and limitations;
- durable evidence/tool-result references;
- material user preferences or definitions needed for continuity;
- explicit omissions and uncertainty;
- no instructions, permissions, provider configuration, or terminal claim.

Critical runtime state remains outside the model-authored capsule. The capsule
cannot overwrite the current Ledger, classification, goal, plan, permissions,
or tool receipts.

### 8.4 Deterministic validation

Before commit, HASHI proves:

- schema validity and exact source-ID coverage;
- source digest equality with the frozen snapshot;
- every protected segment is present byte-for-byte in the candidate effective
  context;
- no open tool transaction was absorbed;
- all required evidence references and unresolved side-effect facts survive;
- the candidate is strictly smaller and fits the target capacity policy;
- capsule text is marked as quoted historical background;
- the persisted raw source remains readable and hash-valid.

The validator does not pretend to prove model summary quality. Semantic
quality is tested with canaries and production observations, while authority
and side-effect truth stay in deterministic protected state.

### 8.5 Atomic commit

Successful compaction writes a new immutable capsule and compare-and-swaps the
effective-prefix pointer from the frozen source digest. A concurrent new turn
or history append cannot be lost: it either lies after the compacted high-water
mark or causes the compare-and-swap to fail and retry from a new snapshot.

Raw transcript and evidence records remain append-only. `/new` or `/fresh` may
change which effective history is assembled under their existing semantics,
but Auto Compact itself never deletes raw history.

## 9. Compact-call timeout and recovery

Semantic compaction is an isolated, tool-free, side-effect-free maintenance
operation. The user has explicitly authorised a narrow absolute watchdog for
this call because a failed compactor must not occupy the foreground
indefinitely. It is separate from the ordinary HER provider recovery policy.

| Tier | Intended route | Initial attempt | One recovery attempt |
|---|---|---:|---:|
| Tier 2 | Remote reasoning-capable compactor | 190 seconds | 300 seconds |
| Tier 3 | Declared local or otherwise slow compactor | 300 seconds | 600 seconds |

These values are defaults in the dedicated Auto Compact namespace and may be
deployment-configured. The following rules are mandatory:

1. Tier 1 is invalid for semantic compaction.
2. `auto` resolves before invocation from declared route capability; it does
   not promote an ordinary healthy provider call based on elapsed time.
3. The watchdog encloses exactly one tool-free compactor model call. It never
   encloses a HER stage, a target provider call, foreground tool execution, an
   OpenRouter/DeepSeek tool loop, or the aggregate multi-chunk compaction job.
4. One fresh-connection recovery is permitted only for an eligible timeout or
   typed transient provider failure, using the same provider, model, reasoning,
   frozen input, schema, and permissions.
5. A separate `CompactionRequest` carries the tier and deadline. Generic
   `StageRequest`, provider profiles, Persona, learning, and tool requests must
   not regain `retry_tier` or `attempt_timeout` fields.
6. `/timeout` remains the user meaningful-progress idle policy and is not
   rewritten by Compact. Compact timeout values do not become backend defaults.
7. Cancellation terminates and reaps the exact ephemeral process or request.
   A timeout leaves the active context pointer unchanged.
8. A completed chunk may be cached by immutable source/input hash, but partial
   unvalidated output is never committed.

This is the sole new exception to the general HER v2 provider-attempt deadline
prohibition. It is justified by the operation's lack of tools, side effects,
workflow authority, or destructive failure mode.

## 10. Provider and tool-loop boundaries

### 10.1 Initial implementation boundary

The first implementation operates before HER-owned stage provider invocations
and on HASHI-owned cross-turn context. It may reduce old conversation or
completed stage evidence before creating an ephemeral backend. It does not
change how that backend performs its own request-local tool loop.

### 10.2 Gemini

Gemini remains stateless:

- each ordinary call receives one HASHI-assembled effective context;
- each Compact call is a separate ephemeral, tool-free invocation;
- no Gemini session ID is persisted or resumed;
- a successful capsule is stored by HASHI and may be included in later calls;
- `/new`, `/fresh`, cancellation, and adapter recreation keep their existing
  semantics.

### 10.3 OpenRouter and DeepSeek

OpenRouter and DeepSeek keep their current unbounded request-local tool loops in
the initial delivery. Auto Compact must not lower `max_loops`, turn a loop into
multiple HER Execution stages, replay completed tools, or force a provider/model
switch.

A later phase may add an adapter capability such as
`supports_safe_context_compaction_boundary`. The hook may run only:

- before the first provider request; or
- after a complete tool result has been appended and before the next provider
  request.

It may never run while a tool is executing or while a call/result pair is
incomplete. The current turn, latest tool call/result, all unverified side
effects, and active response state remain verbatim. This later phase requires
its own regression matrix and is not implied by the initial implementation.

## 11. Failure semantics

| Condition | Required result |
|---|---|
| Soft-pressure compaction fails or times out | Keep original context and continue if it still fits |
| Capsule validation fails | Keep original context; record structured validation failure |
| Compare-and-swap loses a race | Discard candidate pointer update and recompute from the new snapshot |
| Compact route is unavailable | Continue unchanged if possible; otherwise return stable capacity failure |
| Protected set alone is too large | Return `CONTEXT_PROTECTED_SET_TOO_LARGE`; never summarise protected authority |
| Target provider returns typed capacity error | Compact and retry only if the failed request had no new tool/side effect |
| Hard pressure remains after valid compaction | Return `CONTEXT_CAPACITY_EXHAUSTED` with provider/model and measured facts |
| User cancels during compaction | Stop and clean up Compact; leave active context unchanged |
| Audit persistence is unavailable | Fail closed before committing a new effective-context pointer |

Auto Compact never claims the underlying user task succeeded. A capacity
failure preserves completed execution evidence and produces the same truthful
terminal/finalisation treatment as other HER technical failures.

## 12. Audit and observability

Every operation records:

- compaction ID, request/turn reference, trigger source, and target stage/loop
  boundary;
- target and Compact provider/model/reasoning;
- declared capacity, tokenizer/estimator source, pre/post measurements, and
  watermarks;
- protected and eligible segment IDs/hashes, never secret-bearing raw text in
  the low-volume control record;
- timeout tier, attempt deadline, attempt count, typed failure, and recovery
  decision;
- raw archive reference, capsule reference/hash, source digest, and atomic
  commit outcome;
- original-context-unchanged and will-continue truth.

With `/verbose on`, one `started` and one terminal `completed`, `failed`, or
`capacity_blocked` event may be shown. `/verbose off` retains local audit only.
These are technical events, not Persona commentary and not meaningful-progress
leases.

## 13. Required test design

### 13.1 Configuration and routing

- Quick has a smaller context window than the source while an explicitly
  configured Compact model can read it; prove Quick is never invoked.
- `inherit_pro` follows Pro and an explicit cross-provider Compact route does
  not change with `/provider`.
- `/model` changes Compact independently from Quick, Pro, task routes,
  reasoning on other routes, and HER effort.
- missing/removed grants, unknown capacity, tool-disablement failure, and
  cross-provider confirmation fail safely.
- a previously unknown provider with declared capabilities is eligible without
  adding an engine-name branch.

### 13.2 Context fidelity and authority

- system policy, current request, classification, active goal/plan, recent
  guard, open tool transaction, permission, and side-effect facts remain
  byte-identical.
- older eligible context is replaced once, remains recoverable by raw reference,
  and is not injected a second time alongside its capsule.
- prompt-injection text inside history remains quoted data and cannot alter
  compactor tools, authority, schema, or target execution.
- exact canaries spanning old turns, completed work, unresolved work,
  limitations, and evidence references survive hierarchical compaction.
- an oversized source is chunked and merged without a fixed round ceiling;
  no-shrink output aborts without changing active context.

### 13.3 Capacity and failure

- soft watermark, lower target, exact-fit boundary, protected-set overflow, and
  typed provider-capacity recovery are deterministic under synthetic capacities.
- estimator provenance is recorded; an unknown model name never creates a
  fabricated context limit.
- timeout, malformed output, conflicting source IDs, missing evidence, archive
  failure, cancellation, and compare-and-swap race all preserve the original
  pointer.
- hard-pressure failure exposes stable capacity facts without false completion
  or silent provider/model switching.

### 13.4 Timeout isolation

- remote reasoning Compact resolves Tier 2; a declared local/slow Compact
  resolves Tier 3; explicit configuration overrides `auto`.
- Tier 1 is rejected.
- controlled-clock and slow-process cases cross the Tier 2/Tier 3 boundaries
  and prove exactly one compactor recovery.
- the deadline exists only on `CompactionRequest`; generic `StageRequest`,
  Persona, Meditation, Dream, sub-agents, ordinary provider calls, and tool
  loops remain deadline-free.
- a timed-out local compactor process and descendants are reaped without
  touching unrelated foreground or managed background work.

### 13.5 Provider behaviour

- Gemini receives no resume/session state before or after compaction.
- initial OpenRouter and DeepSeek delivery retains the current unbounded loop
  behavior and performs no mid-loop compaction.
- the later safe-boundary suite, when implemented, proves no tool replay,
  exactly-once call/result pairing, and unchanged loop authority.

## 14. Implementation sequence

### Phase A — typed context and capacity metadata

Introduce typed context segments, capacity profiles, tokenizer provenance,
typed capacity errors, and protected/eligible selection without invoking a
compactor. Prove that the effective prompt is unchanged below pressure.

### Phase B — `/model` Compact route

Add persisted `inherit_pro`/explicit/off configuration, provider/model/reasoning
selection, Tier 2/3 policy, cross-provider confirmation, exact-grant validation,
and read-only status rendering. Do not change ordinary routing.

### Phase C — stage-boundary Auto Compact

Add hierarchical tool-free compaction, validation, raw archive, atomic pointer
commit, failure truth, audit, cancellation, and synthetic-capacity tests. Keep
all API adapter tool loops unchanged.

### Phase D — production canaries

Run provider-neutral offline fixtures, one Gemini stateless canary, one remote
reasoning Compact canary, and, when available, one local Tier 3 canary. Source
changes must remain unloaded until the user separately authorises `/reboot`.

### Phase E — optional in-loop adapter hooks

Only after Phase C is stable, separately design and implement safe-boundary
hooks for adapters that declare them. OpenRouter and DeepSeek are not changed
merely because Phase C exists.

## 15. Acceptance criteria

Auto Compact is ready only when:

- no HER core path hard-codes Fast/Quick or a provider/model name for Compact;
- `/model` exposes and independently persists the effective Compact route;
- the compactor can be remote Tier 2 or declared local/slow Tier 3;
- normal HER provider calls and long tool loops retain no attempt deadline or
  loop ceiling;
- Gemini remains stateless and initial OpenRouter/DeepSeek loop behavior is
  unchanged;
- protected authority and current tool truth remain verbatim;
- raw source is immutable and every pointer update is atomic and audited;
- every failure either continues with unchanged usable context or returns a
  stable truthful capacity error; and
- focused, integration, concurrency, cancellation, and live-canary gates pass
  before any rollout claim.
