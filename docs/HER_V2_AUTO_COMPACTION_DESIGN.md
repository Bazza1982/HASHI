# HER v2 Context Compaction

| Field | Value |
|---|---|
| Status | Implemented, merged, and offline-verified; live threshold-trigger acceptance pending |
| Revised | 2026-08-24 |
| Scope | HASHI-owned context capacity management for HER v2 |
| Decision | Compact follows the initiating Agent's active provider and Quick/Light model at high HER effort |

This revision supersedes the earlier independent Compact-route design. The
previous `inherit_pro`, explicit provider/model/reasoning, cross-provider
confirmation, and capability-declaration lock rules are retired.

## 1. Approved default

For every manual or automatic Compact operation, HASHI resolves the route from
the initiating Agent's current persisted HER v2 configuration:

1. provider = active HER v2 Quick/Fast provider;
2. model = active HER v2 Quick/Fast model (the lightweight profile);
3. HER effort = `high`;
4. provider reasoning = the provider's supported high-effort mapping, or an
   enable-only control when the provider does not expose granular effort;
5. timeout tier = Tier 2 by default, or Tier 3 for a declared local/slow
   provider.

Compact does not have an independent provider/model configuration path and
does not silently fall back to Pro, a global default model, retired HER, or a
different provider.

Legacy persisted `inherit_pro` and explicit Compact route records are read as
`inherit_quick` without changing the active provider/model configuration.
A persisted `off` remains off.

## 2. Configuration errors

The route is rejected only when a real configuration requirement is missing:

- HER v2 is not active;
- the active HER v2 provider cannot be resolved;
- the active Quick/Light model cannot be resolved;
- the exact provider/model is absent from the Agent's grants;
- the configured timeout tier is invalid; or
- Compact was explicitly turned off.

The following metadata may still appear in diagnostics, but its absence is not
an eligibility lock:

- prompt-isolation declaration;
- tool-disablement declaration;
- semantic-reasoning declaration; and
- Compact model context-capacity declaration.

Unknown Compact capacity uses a conservative maintenance-call partition
budget. The default unknown-capacity partition is 32,000 estimated tokens; this
keeps the serialized maintenance request below the observed provider edge
without claiming that 32,000 is the model's context capacity. A real provider
capacity rejection remains an execution error and must not change the active
context pointer.

Target-model capacity metadata does not move the Compact thresholds. HASHI
uses one fixed product window for known and unknown targets:

- below 64,000 effective tokens, manual `/compact` reports that compaction is
  not yet useful and gives the exact current count;
- from 64,000 through 128,000 effective tokens, manual `/compact` executes;
- above 128,000 effective tokens, the first main HER v2 Execution invocation
  starts automatic Compact as a detached background task; and
- successful automatic maintenance targets 64,000 effective tokens.

The effective count includes the assembled prompt and serialized target tool
schemas. The boundary is strict: exactly 128,000 tokens does not trigger
automatic Compact. Provider capacity remains diagnostic information and the
32,000-token maintenance-call partition remains an implementation budget; neither
changes the 64,000–128,000 operating window.

## 3. Authority and safety boundary

Compact is maintenance, not an HER execution stage. Every Compact request
enforces these request-local constraints:

- tool registry disabled;
- tools not authorised;
- external side effects not authorised;
- sub-agents not authorised;
- dedicated maintenance system prompt installed after adapter initialisation;
- source transcript treated as quoted, untrusted history;
- no user contact or terminal task decision; and
- no inheritance of Agent Persona as the Compact instruction.

These constraints are enforced by the invocation boundary. They do not depend
on optional adapter capability declarations.

HER effort and provider reasoning are separate values. `high` HER effort
selects compaction depth. Provider reasoning is only a transport mapping; lack
of provider effort levels does not block Compact.

## 4. Protected and eligible context

The current request, current execution state, open tool transactions,
permissions, side-effect truth, and system policy remain verbatim. Automatic
Compact also preserves the latest protected exchange guard. An explicit manual
`/compact` at or above 64,000 tokens removes that recent-exchange eligibility
guard so the command is not rejected merely because the history is recent;
only historical conversation content may still be replaced by a validated
continuity capsule.

Raw transcript rows are append-only and are never deleted by Compact.

## 5. Transactional commit

Compact uses the following transaction:

1. snapshot the active generation and source hashes;
2. select an eligible historical prefix;
3. write and hash an immutable raw archive;
4. invoke the tool-free Compact model;
5. validate schema, coverage, source digest, evidence references, and strict
   size reduction;
6. revalidate frozen source and protected recent turns;
7. write and hash the immutable capsule record; and
8. compare-and-swap the active pointer.

Failure, timeout, cancellation, validation failure, or a lost compare-and-swap
race leaves the active pointer unchanged. Only a complete successful operation
advances the generation.

## 6. Manual and automatic paths

Manual controls:

- `/compact` — compact the eligible prefix now;
- `/compact status` — inspect effective route and pointer;
- `/compact cancel` — cancel an active operation;
- `/model compact inherit_quick [tier]` — enable the approved default;
- `/model compact off [tier]` — disable Compact;
- `/model compact tier <auto|tier_2|tier_3>` — select the watchdog tier.

Manual `/compact` is accepted at every effective size of 64,000 tokens or
greater. Below that floor it performs no model call and reports the exact
threshold comparison. At or above the floor it invokes the initiating Agent's
Quick/Light backend immediately and reports the selected-history reduction.

Threshold-triggered automatic Compact has exactly one scheduling boundary:
the first main HER v2 Execution provider invocation, and only when the effective
context is above 128,000 tokens. Prompt assembly, Planning, and post-turn
observers do not invoke or wait for Compact. Execution schedules a detached
maintenance task and immediately continues its own provider call with the
already assembled prompt. A successful background commit affects later prompt
assembly, never the in-flight request.

Automatic Compact is forever non-blocking. Failure, lock contention, timeout,
retry exhaustion, an unavailable route, invalid output, or a
non-shrinking result cannot fail, pause, retry, or change the current HER task.
Every unsuccessful automatic outcome records the condition and emits a
mandatory user-visible warning independently of `/verbose`; warning delivery
itself also runs outside the foreground task. The active pointer advances only
after a complete validated commit.

A typed rejection from the selected target provider is a separate reactive
recovery path: when the rejected call provably performed no tool call, external
side effect, or delivery, HASHI may compact and retry that rejected request
once. This does not alter the threshold-triggered Execution-stage scheduling
contract above.

## 7. Audit and user-visible status

Audit events record:

- compaction ID and request reference;
- trigger source;
- actual provider and Quick/Light model;
- fixed HER effort;
- mapped provider reasoning;
- tool, external-side-effect, and sub-agent authority as disabled;
- capacity metadata when known;
- the fixed 64,000-token manual floor, strict above-128,000 automatic trigger,
  64,000-token target, and their product-policy provenance;
- source IDs/hashes and protected hashes;
- timeout tier and attempt;
- result, failure code, and atomic commit outcome; and
- whether original context remained unchanged;
- the explicit `will_continue` decision; and
- mandatory warning scheduling/delivery outcome when automatic maintenance
  does not complete.

`/compact` reports the current count and thresholds before work, then the
selected-history reduction or an exact not-needed reason. Internal compaction
IDs and route diagnostics are omitted from ordinary results. `/compact status`
retains route, effort, pointer, capacity, current-count, and window diagnostics
for troubleshooting.

## 8. Verification requirements

Release verification must cover:

- active-provider + Quick/Light selection;
- HASHI API `gpt-5.6-luna` at high effort;
- providers with enable-only reasoning;
- missing capability declarations and missing Compact capacity without route
  lock;
- manual boundary behaviour at 63,999, 64,000, and 128,000 tokens;
- automatic boundary behaviour at exactly 128,000 and 128,001 tokens for both
  known and unknown target capacity;
- no automatic Compact call during prompt assembly or post-turn handling;
- one detached trigger at the first main Execution invocation, excluding
  sub-agent Execution and later Execution retries;
- both retryable Compact attempts exhausted while the selected model still
  receives the original request and `/verbose off` still receives the warning;
- missing active Quick grant with no fallback;
- manual and automatic triggers;
- model failure, malformed output, timeout, and cancellation;
- raw transcript retention;
- immutable archive/capsule validation;
- compare-and-swap atomicity;
- audit fields; and
- the user-visible `/compact` command path.

The integrated source has passed general Agent runtime reload adoption. Auto
Compact must not be described as live threshold-trigger accepted until a real
user-visible threshold cycle has also passed.
