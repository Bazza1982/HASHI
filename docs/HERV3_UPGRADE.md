# HERV3 upgrade: JEV experiment and simplified routing

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

Prior WIP Journal evidence remains bounded, quoted context for a new turn when
canonical recovery is absent. It no longer sends a proactive recovery card
before that turn: the current user request is processed first, and recovery
facts are available to the model without claiming authority to replay effects.

HASHI3 source now bounds ordinary text Tool results before they re-enter the
model loop (including structured text content); native image blocks remain
media, not truncated text. File reads and directory listings also have local
read/enumeration limits. For OpenAI-compatible Provider calls with a declared
target capacity, a clearly oversized serialized text request is rejected
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

`/effort` is model reasoning only and is derived from the selected Provider/model.
DeepSeek exposes exactly `off`, `high`, and `max`; those values control the
DeepSeek request's thinking mode and never select a HASHI workflow. The Backend
catalogue exposes Provider-specific models and effort choices rather than copied
HER-wide placeholders.

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
