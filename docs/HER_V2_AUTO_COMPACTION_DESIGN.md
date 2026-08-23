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

Unknown target-model capacity does not disable automatic maintenance. HASHI
uses an explicitly named absolute maintenance threshold instead of fabricating a model
capacity: pre-turn and post-turn compaction trigger at 64,000 estimated tokens
and aim below 48,000. These values are HASHI product budgets, not provider
metadata or a permission to stop work. If the protected set itself reaches the
threshold, or compaction cannot bring the effective prompt below it, HASHI
keeps the protected/current context intact, emits a mandatory user-visible
warning, and still invokes the selected model with the best safely assembled
context. The provider may then accept or reject that request under its actual
capacity rules.

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
permissions, side-effect truth, latest protected exchange guard, and system
policy remain verbatim. Only an older eligible historical prefix may be
replaced by a validated continuity capsule.

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

Automatic paths use the same route resolver and transaction:

- pre-turn soft-watermark compaction for declared capacities, or the absolute
  64,000→48,000 HASHI maintenance threshold when target capacity is unknown;
- post-turn scheduling using the same resolved threshold;
- one safe retry after a typed target context-capacity rejection when no new
  tool, external side effect, or delivery occurred.

Automatic Compact is never a pre-HER gate. A failed, locked, timed-out,
retry-exhausted, unavailable, or non-shrinking compaction leaves the current
request runnable. HASHI uses an already committed smaller capsule when one
helps; otherwise it preserves the original prompt. It records the condition,
surfaces a mandatory warning independently of `/verbose`, and continues the
same model request. This rule also applies when an estimate reaches or exceeds
120,000 tokens: the threshold triggers maintenance and warning behaviour, not
a new execution stop condition.

## 7. Audit and user-visible status

Audit events record:

- compaction ID and request reference;
- trigger source;
- actual provider and Quick/Light model;
- fixed HER effort;
- mapped provider reasoning;
- tool, external-side-effect, and sub-agent authority as disabled;
- capacity metadata when known;
- the resolved trigger budget and provenance, including the named
  unknown-capacity maintenance threshold;
- source IDs/hashes and protected hashes;
- timeout tier and attempt;
- result, failure code, and atomic commit outcome; and
- whether original context remained unchanged;
- the explicit `will_continue` decision; and
- mandatory warning scheduling/delivery outcome when automatic maintenance
  does not complete.

`/compact` and `/compact status` show the effective provider/model, HER
effort, provider reasoning, trigger/result, and pointer information.

## 8. Verification requirements

Release verification must cover:

- active-provider + Quick/Light selection;
- HASHI API `gpt-5.6-luna` at high effort;
- providers with enable-only reasoning;
- missing capability declarations and missing Compact capacity without route
  lock;
- unknown target capacity automatically compacting a Sunny-sized historical
  prompt before HER, plus warning-and-continue behaviour when that compaction
  fails;
- a 120,000-token policy threshold with both retryable Compact attempts
  exhausted, proving the selected model still receives the original request;
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
