# HERV3 upgrade: JEV experiment and simplified routing

## 2026-10-19 long-running execution invariant

HERV3 foreground work has no HASHI total token, message, Turn, model-call,
tool-call, elapsed-time, output, continuation-history, receipt, WIP, or task-state
ceiling. Long work ends only through normal completion, explicit cancellation,
a genuine failure, or a scoped safety boundary that is not a total-work budget.
This invariant applies equally at every privacy level; privacy classification
changes inspection and routing, never the permitted size of the model/tool loop.

Durable continuation is lossless rather than merely assigned a larger constant.
Provider messages use an ordered segmented store with per-item and aggregate
integrity verification. Receipts, task state, WIP, checkpoint evidence and
completed Session history retain every item. Large Privacy Level 2 inspection
is internally paged through the detector without rejecting the request because
of aggregate text size. Per-tool cursors and source paging may bound one
response frame, but they keep a continuation route to all authorised source
data and do not cap the Engine Turn.

The retained finite boundaries are semantically separate: explicit
cancellation; meaningful-progress idle detection; provider transport
inactivity/protocol guards; an explicitly requested timeout for one Tool call;
official external Provider media/body limits; retry/replay safety after a
failure; and small rolling observation windows used only to detect cycles or
rate-limit commentary. None expires, truncates, or counts down healthy total
work. The source repair is recorded in
[the October 19 repair decision](repairs/HERV3_LONG_RUNNING_LIMIT_REMOVAL_20261019.md).
Source verification, Worker adoption and live acceptance remain separate.

The [Ollama context pilot](HERV3_OLLAMA_CONTEXT_PILOT.md) began on 2026-10-15
as an Agent6-only `feature-ollama` experiment and is explicitly approved for
main promotion on 2026-10-18. It remains an instance opt-in: exact configured
native context, no server truncation, native protocol evidence and GPU
qualification. The current configuration guide, capacity/admission repairs,
binary reasoning and measured acceptance limits are in its owning decision. It does
not change PCM design or other providers; source and live adoption are separate.
The October 16 continuation selects a 48K baseline and 8K output reserve,
adds optional short idle residency/native timing evidence, and tests Qwen
without forced CPU offload. Agent6 subsequently adopted committed source
through a verified Agent-only hot reboot; its active profile publishes 48K/8K
and 90-second idle residency. User task testing and actual voice contention
remain separate acceptance items; the original 64K results stay historical.

The October 16 Ollama-only presentation experiment bypasses auxiliary Persona
commentary, clarification rendering and optional final-style inference when
the resolved Model Provider is `ollama`. Public configuration and status name
the Provider separately from its model; `ollama-api` is an internal adapter
compatibility ID. Registered Provider names and legacy IDs both resolve through
the registry. Neutral progress and required
questions still cross their typed delivery boundaries; the exact main-model
final answer and its PCM Persona remain intact. Other Model Providers retain
their presentation calls. This is not the downstream `/commentary` visibility
switch. See the [presentation decision](HERV3_OLLAMA_CONTEXT_PILOT.md#2026-10-16-ollama-only-pcm-presentation).
Source verification and Agent6 Worker adoption must be reported separately.

The 2026-10-04 [scoped-search Function change](HERV3_SCOPED_SEARCH.md) retains
the single model/tool loop and direct Shell use. Execution facts drive technical
verbose activity independently of commentary; AC heartbeat is not actual progress.
The owning decision separates source verification from live adoption.

Voice-message uploads use the Worker-owned local transcript in the current
Execution loop and text-only Direct route, rather than relying on retired
Triage to consume it. Safe Voice confirmation precedes the model call; an
unavailable or discarded transcript starts no model action. The explicitly
selected native-audio route retains raw audio. Other attachments remain bound
to their original identity and media route.

## Decision and scope

HERV3 is the current HASHI Engine Runtime upgrade and the canonical
human-facing name. The public machine ID remains `her-v3`. The internal
`her-v2` adapter, `her_v2` configuration key, and `HERv2*` Python names remain
temporarily as compatibility boundaries; they are not the product name.

The upgrade combines four decisions:

- one continuous main-model/tool loop owns reasoning, planning, adaptation,
  tool use, and verification;
- a bounded JEV Agent Companion experiment may judge observable liveness state
  and offer advisory intervention at a safe tool boundary;
- Strategy Cards remain optional reference context, never a routing stage or
  mandatory handoff; and
- Habit/Meditation learning remains an optional post-delivery subsystem.

HERV3 completely removes Triage, Strategy, Planning, Replanning, Review, and
stage-based Finalisation from active foreground routing and public controls.
The model may still plan or revise its approach naturally inside the one
conversation; those ordinary reasoning acts are not HASHI stages.

## Adoption history

This work originated on HASHI3 branch `experiment/her-v3-hashi3`, based on
`main` at `6a36caf5`. It carries only the HERV3 engine/harness change from
`exp-herv3`. The original transplant excluded a separate JEV style-finalisation
branch. The current HERV3 decision instead includes only the bounded JEV Agent
Companion experiment described below; unrelated experimental changes remain
excluded. The 2026-09-25 approval covered that HASHI3-only transplant and its
live acceptance.

On 2026-09-26 the user separately authorized promotion of this harness-only
commit series into HASHI1's local `main`, combined with the independently
developed standard Frontend Connector structure. HASHI3 is a read-only source
for that promotion and is not changed. HASHI2, HASHI4, and GitHub `main` are
also outside this adoption scope. Source integration, offline qualification,
and HASHI1 live adoption are recorded separately; none substitutes for another.

HERV3 removes staged cognitive orchestration from the foreground path.
The model owns reasoning, planning, adaptation and verification inside one continuous
model/tool conversation. HASHI continues to own PCM, Session continuity, tools,
permissions, recovery, audit, user commentary and learning.

Prior WIP Journal evidence remains complete, quoted context for a new turn when
canonical recovery is absent. It no longer sends a proactive recovery card
before that turn: the current user request is processed first, and all recovery
facts are available to the model without claiming authority to replay effects.

Individual Tool response frames may be paged before they re-enter the model
loop (including structured text content); native image blocks remain media.
File reads, searches and directory listings expose offsets or cursors so the
model can retrieve all authorised source data across as many calls as needed;
these frame sizes are not HERV3 execution ceilings. For OpenAI-compatible
Provider calls with a declared target capacity, a clearly oversized serialized
text request is rejected
before HTTP with the existing typed capacity code. Unknown capacities and
native-media accounting still depend on Provider rejection; the preflight
does not promise that every request will fit. A capacity rejection after tool
activity remains non-replayable without user reconciliation.

### Completed-tool checkpoint provider-call continuation

HERV3 may make a bounded transport retry of only the unfinished physical Model
Provider call after tool activity. Every executed tool must have either an exact
verified read receipt, or a completed durable HERV3 operation checkpoint binding
the invocation/attempt, Tool name, call ID, terminal status and returned output
digest. A completed write or shell checkpoint permits continuing the model call;
it neither verifies the business effect nor authorizes re-executing that tool.
An unrecorded, pending or mismatched result blocks continuation. A registry
`read_only` declaration, successful shell exit or model prose is not proof.
The effect-receipt owner maintains the observational-tool allowlist;
`verification_run` is excluded because even a successful verification argv or
recipe may execute subprocesses and modify the workspace.

The continuation reuses the already assembled Provider messages, including the
existing tool results. It does not execute a Tool again, restart the HER Turn,
or mark the whole PAO Run safe to replay. Provider text emitted by a
tool-enabled HERV3 stage remains internal until its final result, so an
incomplete draft may be discarded before this same-call continuation. The
existing no-Tool rule remains stricter: once an answer preview may have become
user-visible, an incomplete call is not retried in place.

Missing or incomplete checkpoints, mismatched identity/output or an uncertain
in-flight tool block this continuation. Terminal failures distinguish completed
operations from verified business effects, retaining reads, readback writes,
proved no-change failures and unverified effects. Async Worker queries are
awaited; a failed background query does not erase existing write receipts. Missing,
corrupt or bounded-away evidence is explicitly incomplete. Provider-call
continuations share the existing bounded local recovery budget; they never
authorize replay of a prior Tool call or user request.

Provider-context checkpoints persist the PAO producer claim that existed when
the checkpoint was written, including Run/request, attempt, attempt ID, fence
revision and input digest. Recovery verifies that stored claim and may only
issue evidence for that same producer. A current consumer claim is an access
check, not a replacement producer label: after attempt 1 writes C1, attempt 2
cannot turn C1 into attempt-2 evidence merely by reading it with claim 2.
Inherited provider context retains its original producer claim until a new
checkpoint is actually written. A missing, corrupt, relabelled or mismatched
producer claim makes the recovery evidence unavailable rather than inviting a
replay.

The meaningful-output stream inactivity limit is 300 seconds regardless of
whether fallback models are configured. It counts provider read wait, excluding
local tools. Transport diagnostics retain bounded exception causes, actual
httpcore phase timings and private wire references through the stage boundary.
One versioned PAO/public failure supplies the Engine name, counts and recovery
decision to every frontend; Workbench preserves it across transcript refreshes.
These 2026-10-06 changes are scoped to the HASHI3 development repair; qualification
and adoption receipts are recorded in [the batch journal](repairs/NIGHTLY_20261006_HASHI3.md).

### Terminal commentary fence

Progress commentary is request-local and must quiesce before a required Final
or Clarification delivery starts. `TurnServices` serializes commentary in
source order and reserves the rate-limit interval before awaiting Persona
packaging or transport, so fast Tool start/completion events cannot launch
competing deliveries. The request-local Persona commentary pipeline retains
one attempt per event ID and never replays an attempt whose transport outcome
may be ambiguous.

At the terminal boundary, `TurnServices.close()` first rejects new progress,
cancels its observers, and then closes the Persona pipeline. Closing the
pipeline cancels the real package/delivery tasks even when callers were waiting
through a shield. Pending optional commentary is discarded before the Final or
Clarification is delivered; an already-started transport is not retried.
Execution drafts and required terminal messages keep their existing typed
delivery and replacement contracts. The adapter-level close remains an
idempotent cleanup fallback, not the ordering barrier.

## Foreground path

`PCM -> main model <-> tools -> delivery`

For an active request, HERV3 emits a bounded internal route event when a
configured provider/model is selected and another after that adapter returns
a response. PAO's request-activity projection can pass these facts to the
foreground Phone without a second model call. The model name comes from the
selected adapter profile; the returned event does not independently attest
the remote provider's model identity or prove that the user's task succeeded.

Triage, Strategy, Planning, Replanning, Review and stage-based Finalisation are
not reachable foreground stages. Legacy code/config names remain temporarily
where they are stable adapter or persistence contracts; they do not select
different workflows.

## Configuration

The existing `her_v2` storage key is retained during the experiment so no outer HASHI
backend/session migration is required. New configurations may use:

```json
{
  "her_v2": {
    "main": {
      "provider": "deepseek-api",
      "model": "deepseek-flash",
      "reasoning": "high"
    },
    "v3_provider_allowlist": ["deepseek-api"],
    "strategy_cards": false,
    "agent_companion": {
      "enabled": true,
      "interval_minutes": 5,
      "model": "jev-latest"
    },
    "commentary_interval_s": 150
  }
}
```

The public Engine ID is `her-v3`. The internal `her-v2` adapter and `her_v2`
configuration key remain only as storage compatibility boundaries and are not
returned by the Backend API or command cards. `/provider` selects one configured
Provider, `/model` selects one of that Provider's real model IDs, and both changes
persist immediately for subsequent turns. Old route buttons are rejected rather
than allowed to change ignored settings.

Public Provider identity is consistent across shared, creation and per-Agent
catalogues, offline metadata and live presentation: the `providers` dictionary
key and its entry's `engine` use the same public name, such as `deepseek` or
`ollama`, as the current runtime Provider. Clients can resolve that exact
Provider/model pair without guessing adapter suffixes or adding duplicate
model options. Model and reasoning choices still derive from the effective
resolver, including instance overrides and explicit empty restrictions.
Registered `-api` adapter IDs remain accepted as legacy input; creation
normalizes them at the Provider boundary and retains canonical adapter IDs in
internal configuration and grants. No identity migration or configuration
rewrite is needed for existing Agents.

`/effort` is model reasoning only and is derived from the selected Provider/model.
DeepSeek exposes exactly `off`, `high`, and `max`; those values control the
DeepSeek request's thinking mode and never select a HASHI workflow. The Backend
catalogue exposes Provider-specific models and effort choices rather than copied
HER-wide placeholders.

HERV3 Provider menus and target validation derive model choices through
`runtime_effort_options`. An instance's `allowed_backends.available_models`
restriction also applies inside HERV3, including an empty list. Shared defaults
and Provider metadata cannot reintroduce excluded models; a Provider's default
selection must be one of its effective models.

Some Provider/model pairs use a binary reasoning option, such as `off` or
`enabled`. The selected value must reach that Provider unchanged, including
after a Worker loads persisted Agent settings. The retained internal HER effort
enum is bookkeeping only and must not reject a valid Provider option or replace
`enabled` with `high` in the Provider request or reported Run metadata.

### HASHI1 correction, 2026-09-27

HASHI1's prior `her_v2` entries had no explicit v3 `main`, so the compatibility
normalizer inherited legacy execution profiles. Lily, Sunny, Momo, Feiyan and
Testing consequently selected GPT models; a retained `off` effort produced an
HTTP 400 on a GPT model that does not support it. The HASHI1 instance configuration
now gives every HERV3 Agent an explicit `deepseek-api/deepseek-flash` default
main and auxiliary at `high`. Agent-specific opt-ins may also allow other
Provider/model pairs, including Testing's OpenRouter model. Other Engines
retain their own settings. The Function loader repairs a persisted HERV3 effort
that is unsupported by the selected Provider/model to the configured compatible
effort. Source, instance configuration, and live Worker adoption are separate;
the running HASHI1 generation was not restarted as part of this correction.

### HASHI1 OpenRouter instance opt-ins, 2026-09-27

The user requested five exact OpenRouter models for every HASHI1 Agent:
`cognitivecomputations/dolphin-mistral-24b-venice-edition`,
`thedrummer/cydonia-24b-v4.1`, `sao10k/l3.3-euryale-70b`,
`sao10k/l3.1-euryale-70b`, and `gryphe/mythomax-l2-13b`. All 18 Agent
`allowed_backends` entries opt into those models without changing defaults or
the shared model catalogue. OpenRouter's current catalogue lists no reasoning
parameter for these five, so their instance `model_efforts` entries are empty.
HERV3 omits its inherited reasoning setting for a model explicitly declared
this way; an undeclared model retains its previous Provider behavior.

Only the Llama 3.1 Euryale model currently advertises tool parameters. The
other four are suitable for conversations that do not require model tool calls.
OpenRouter returned HTTP 404 for those four when HASHI attached its tool
catalogue; the provider's routing error specifically reported no endpoint
supporting tool use. Every HASHI1 Agent's OpenRouter `allowed_backends` row now
sets `model_tool_support` to `false` for those four and `true` for Llama 3.1
Euryale. The HERV3 main request omits tool definitions and side-effect authority
for a chat-only model, and tells the model to disclose that limit for tasks
requiring tools. Compatibility-only non-main invocations reject that model
explicitly when their contract requires tools.
MythoMax advertises an 8,192-token model context, while its current top
OpenRouter endpoint reports 4,096; request planning should use the effective
endpoint limit. A focused test failed before the tool-capability correction and
passed afterward. Source and ignored instance configuration were checked
offline; running Worker adoption and live Provider use require separate
verification after an authorized Function replacement.

## Strategy Cards

Cards are optional reference context. When disabled, they are absent. When enabled,
the same main model self-selects a small relevant set and may report the selected
IDs for experiment auditing. This remains an in-loop advisory aid: it does not
produce a strategy handoff, another Provider call, or a routing stage.

## Persona Commentary

The main model may produce commentary naturally. HASHI rate-limits user-visible updates
to roughly 2-3 minutes and only forwards substantive progress. On the first real tool
operation HASHI may emit one initial acknowledgement. Commentary never becomes task
instructions and never counts as task progress.

The 2026-10-08 HASHI3 repair removes Tool-completion-authored updates. Raw
output differences, timestamps and errors still inform activity/AC observations,
but cannot claim that new task evidence or successful progress was found. The
foreground model authors findings, consequences, changed approaches, results
and blockers; its prompt explicitly excludes generic reassurance and repetition.

Request-local delivery deduplicates event IDs and normalized exact wording over
the latest 128 updates. Updates within the rate-limit window are queued and
combined at the next available window, rather than discarded. The queue retains
the newest complete updates within the existing 4,000-character bound. Persona
packaging and its one-attempt transport fence remain unchanged. Closing the
request cancels the timer and discards pending optional progress before Final
or Clarification; it never retries an ambiguous delivery. Source/offline and
running Worker/client verification are recorded separately in
[the nightly repair journal](repairs/NIGHTLY_20261008_HASHI3.md).

## Optional final style check — HASHI1 pilot, 2026-09-28

HASHI1 adds a workspace-scoped `/style on|off|status` pilot. With Style off,
the continuous main model's final text goes directly to the normal Frontend
Connector delivery boundary. With Style on, the configured HERV3 auxiliary
model receives the completed draft, current request, and the typed PCM
permanent/global/local system and Persona presentation sections. It returns a
strict `keep` or expression-only `rewrite` decision before that same FC
boundary. It has no tools, side-effect authority, routing authority, or power
to change facts and decisions. Invalid output or Provider failure preserves the
main-model draft exactly.

This is an optional presentation pass, not a return of stage-based
Finalisation and not a second task model. `/style` is declared once in the
shared command catalogue, so Telegram and external Frontend Connectors use the
same command owner. The auxiliary call is metered separately as a final style
check; meter attribution distinguishes a kept main-model answer from an answer
whose expression was rewritten. Provider output is never sent directly: both
paths return one final response through FC.

## JEV Agent Companion experiment

AC is an optional 5/10-minute liveness observer. JEV receives bounded observable runtime
state (tool names, hashes/status, activity/progress timing), not hidden reasoning. A
`continue` or `unknown` judgement is silent. A high-confidence `trouble` judgement queues
one notice at the next safe tool boundary; that one tool action is not executed and the
same main model decides whether to change direction, report a blocker, or finish.
Deterministic cycle control and user cancellation remain independent safety controls.

## Habit and Meditation retention

Habit/Meditation reflection remains a post-delivery background learning path.
It remains separate from the JEV Agent Companion experiment. HERV3 does not
turn Habits into routing stages and does not require a Strategy, Planning, or
Replanning handoff before applying advisory Habit context.

## Local verification and next testing

The HER adapter, model catalogue, public Frontend projections, commands,
locales, token usage, and Function qualification now cover the single
main-model/tool loop. The final full offline product suite completed with
711 passed and 1 skipped; the Protected Core check also passed. Live Worker
and real-provider evidence remains recorded separately below.

HERV3 model/provider buttons use one shared callback contract for both card
generation and Frontend dispatch. Acceptance must exercise generated button
callback data through the registered callback handler; successful text-form
`/model` or `/provider` commands do not prove that Telegram buttons work.

## HASHI3 live verification (2026-09-25)

The approval covered this HASHI3-only experiment, all-Agent HERV3 availability,
and live switching/usage acceptance. The HASHI3 root checkout runs local branch
`experiment/her-v3-live-hashi3`; `main` remains at `6a36caf5`, GitHub `main` was
not changed, and Protected Core was not edited.

All nine HASHI3 Agents now advertise public Engine `her-v3` with Provider
`deepseek-api`, models `deepseek-flash` and `deepseek-v4-pro`, and model-reasoning
efforts `off`, `high`, and `max`. The shared catalogue derives the same
Provider-specific choices. `/backend`, `/provider`, `/model`, and `/effort`
show and persist those values; `/herv2` is absent from the public command set.
`/metre` works as the declared alias of `/meter`, and `/token` normalizes the
internal compatibility Engine to the public `her-v3` label.

The first all-Agent adoption exposed a startup dependency on removed legacy
profiles. The replacement did not commit; the registered HASHI3 recovery task
restored service. A real-manager regression reproduced the failure, the HERV3
provider target now bootstraps directly, and later shared replacements
succeeded with all nine Workers online. A subsequent live red Run exposed the
internal compatibility Engine in a Session assistant-message source; the
public projection was fixed and the rerun stored `source=her-v3`. Final live
acceptance also found the same compatibility name in request-activity origins;
that public stream now projects `her-v3` while internal storage remains intact.

Live acceptance used the formal Session API and durable usage records:

- `req-phd_1-2026-09-25_221320-0001` ran `deepseek-flash` at effort `off`,
  returned exactly `HER_V3_FLASH_PUBLIC_SOURCE_OK`, recorded one Provider call,
  zero reasoning tokens, and public Session source `her-v3`.
- `req-phd_1-2026-09-25_221320-0002` ran `deepseek-v4-pro` at effort `max`,
  returned the correct `7^222 mod 1000 = 049`, recorded one Provider call and
  547 reasoning tokens, and public Session source `her-v3`.
- `req-phd_1-2026-09-25_224626-0001` ran on the final deployed generation,
  returned exactly `HER_V3_PUBLIC_ORIGIN_OK`, stored Session source `her-v3`,
  and exposed only `her-v3`, `her-v3:deepseek-api`, and `her-v3:runtime` in
  its public request-activity stream.
- `worker-1` switched from `codex-cli / gpt-5.6-luna` to HERV3 through
  `/backend`; `req-worker-1-2026-09-25_221315-0001` then returned exactly
  `HER_V3_BACKEND_SWITCH_OK` through `deepseek-v4-pro`. The Agent was restored
  to its original Codex model and `max` effort afterward.
- `/metre summary` reported the Pro Run as `deepseek-api /
  deepseek-v4-pro`, one call, 17,801 tokens, and US$0.003296. `/token` showed
  the HERV3 public label and no public HERV2 bucket. `/usage` presents
  pre-attribution history as `historical-unattributed` / `历史未归属` instead
  of exposing the retired storage placeholder `role-configured`.

The final retained `phd_1` setting is `deepseek-api / deepseek-v4-pro` at
effort `high`. These checks establish HASHI3 Backend API selection, persistence,
real DeepSeek execution, provider reasoning, Session presentation, token
accounting, and meter reporting. They do not authorize a merge or deployment to
another instance, and do not qualify optional Strategy Cards, Agent Companion,
long-running processes, or Habit Reflection.

### Model picker callback repair (2026-09-26)

The visible HERV3 model buttons originally emitted `herv3_*` callback data,
but the runtime callback registry did not route that namespace. Text-form
`/model` acceptance therefore passed while Telegram button presses were ignored.
The card generator and callback registry now share one HERV3 callback contract,
with a regression that checks every generated Provider/model button resolves to
exactly one runtime handler.

HASHI3 adopted the repair through an authorized `/reboot max`: all nine Workers
returned online on the new Function generation. Live command-menu acceptance
opened the real `phd_1` `/model` card, invoked the generated
`deepseek-flash` button, verified the live and persisted target changed, then
opened a fresh card and invoked `deepseek-v4-pro` to restore the retained target.
The retained effort is `max`. This exercises the same callback registry used by
Telegram registration; a device-side Telegram tap remains an operator observation,
not a synthesized test result.

The October 8/9 HASHI3-only nightly repair removes observer-generated generic
Tool-completion commentary, coalesces substantive findings and cancels queued
updates before Final. The actual main-model contract carries the finding
guidance; Persona and uncertain-delivery fences remain. A real daily Workbench
canary displays one grounded model-authored finding before Final without generic
duplicates. Luna did not produce a finding in its earlier canaries, so prompt
guidance is not claimed to force every model to report progress. The temporary
QA target was restored afterward. See
[current nightly delivery](repairs/NIGHTLY_20261008_HASHI3.md).

## HASHI3 offline repair, 2026-10-16: safe stream-timeout continuation, consented fallback, truthful failure reporting

Branch `debug-herv3` on HASHI3 (base `main` = `52181d1e`).  Source and offline
evidence only; adoption and live verification have not happened.

- A typed `PROVIDER_STREAM_IDLE_TIMEOUT` / `PROVIDER_RESPONSE_START_TIMEOUT`
  may now use the existing bounded current-call continuation when
  `safe_current_call_continuation` is already proven by checkpoint evidence.
  The generic `hashi_disable_inplace_retry` guard still blocks every blind
  retry; the no-Tool and partial-answer fences are unchanged; the shared
  three-request local recovery budget is used, not duplicated.
- Ordinary general-model fallback is now a user-consented policy (default
  OFF) with an ordered allowlist, per-source rules, privacy narrowing, and an
  explicit additional-call budget.  HERV3 consumes the resolved decision for
  same-model Provider continuation; the HER stage layer consumes it for the
  pre-effect HERV3/Codex CLI switch.  See
  [model fallback policy](HASHI_MODEL_FALLBACK_POLICY.md).
- A consent/cost entry authorises one fallback route handover; it does not cap
  the selected target at one physical Provider request. Once selected, the
  target runs the normal unbounded HERV3 model/tool continuation and ordinary
  scoped transport recovery. Completed Tool IDs reuse their durable result
  binding and changed/concurrent repeats fail closed. The target inherits the
  source request's privacy, Tool, invocation, reasoning, parameter, and
  stream-inactivity contract. ASK confirmation, both source directions, route
  ordering, revision-safe saves, and configured finite cost estimates are
  wired through the same policy.
- Terminal public failure reporting separates same-target retries, the
  interaction recovery used/limit, the Turn-wide cumulative count, and the
  fallback disposition, and says "already retried, but still failed" in the
  selected language.
