# HASHI model fallback policy (user-consented, HERV3/Codex CLI)

Status: current decision for ordinary general-model fallback.  Owner: PAO,
implemented in `orchestrator/model_fallback_policy.py` (Functions layer plus
the existing user-scoped configuration persistence boundary).  HERV3 and
Codex CLI expose typed capabilities and consume a resolved decision; the
Frontend Connector renders and edits the same policy.  Protected Core is not
involved.

## Scope and default

Fallback applies only to ordinary general-model requests.  The default is
`off` for every existing and newly migrated user.  A configured API key,
installed CLI, available model, or catalogue entry proves capability only --
never consent to spend money or send data to another destination.

`/phone`, `/call`, and `/voice` requests are excluded at the policy evaluator
itself (not merely hidden in UI), using authoritative request facts, so
aliases, scheduler entry, Frontend Connector entry, and future renderers
cannot bypass the exclusion.

Fallback is distinct from:

- same-target local recovery, which re-opens the unfinished physical call to
  the already selected Provider/model under the existing bounded recovery
  budget;
- whole-Turn replay, which remains forbidden after possible effects.

## Policy surface

Persisted per user through `orchestrator.config_json` transaction rules in
`state/model_fallback_policy.json` (actor-scoped, versioned, revision
checked, never storing credentials, model catalogues, or resolved secrets):

- global mode: `off` | `ask` | `auto`;
- an explicit ordered allowlist of exact, typed routes;
- independent rules for a HERV3 source and a Codex CLI source, including
  independent permission to use HERV3 or Codex CLI as a target;
- per-existing-privacy-policy restrictions that can only narrow the global
  policy;
- a maximum number of additional paid calls (default 1 once automatic
  fallback is enabled);
- an optional extra-cost ceiling and an explicit `allow_unknown_cost` choice
  (default `false`).

Route kinds:

- `herv3_model`: one exact Provider and model.  A route marked `same_model`
  is eligible only when the authoritative compatibility registry
  (`orchestrator/flexible_backend_registry.same_model_target`) proves that the
  target serves the same canonical model as the failing source; string
  similarity never does.
- `codex_cli`: the Codex CLI engine.  This switch is pre-effect only.

## Effective resolution order (first blocking reason recorded)

1. hard request-scope exclusion, including phone/call/voice;
2. current privacy policy (narrowing only);
3. global mode;
4. source rule;
5. exact allowed target and its order;
6. configured credential/runtime availability;
7. model, Tool, context, and message-contract compatibility;
8. checkpoint/current-call safety;
9. remaining additional-call and cost budget.

`ask` mode produces a one-time action bound to the exact failed request,
target, privacy decision, and remaining budget.  It expires; it is not a
"send the request again" button.  Background/scheduler work cannot wait for an
answer, so `ask` behaves as blocked and reports that automatic fallback was
not authorised.  Consuming the additional-call budget invalidates the old
action, so a duplicate or delayed confirmation cannot issue another paid
call.

The interactive Telegram consumer presents that bound action during the
active request and resumes only after the authorised user accepts it.  The
same `/fallback` menu can express HERV3 and Codex CLI sources independently,
order exact routes, set the call/cost budget, and preserves the configuration
revision read by each callback.  A concurrent save therefore produces a
conflict instead of overwriting newer policy state.

## Attempt order and safety

1. finish the existing bounded same-target local recovery decision;
2. if it still fails and the failure class is recoverable -- 429, retryable
   5xx, or the typed stream-inactivity timeout -- evaluate the effective
   user fallback policy; the stage consumer uses the shared
   `FALLBACK_ELIGIBLE_FAILURE_CODES` and the adapter consumer uses the same
   transient-or-typed-timeout test, so a permanent failure (400, auth,
   configuration, contract) never consults the policy and never spends an
   additional paid call;
3. select the first eligible target in the user's order;
4. consume exactly one additional-call budget entry before issuing the paid
   call;
5. cap each selected target at one physical Provider request, so it never
   receives a hidden fresh retry chain; then continue to the next eligible
   route only while the user's total additional-call budget remains.

After Tool activity, an allowed HERV3 target continues the exact assembled
Provider messages and completed Tool results under the existing checkpoint
contract: no Tool is re-executed, call IDs and result bindings are preserved,
an incomplete internal draft is discarded, `tools_replayed` stays `false`,
and the HER Turn, PAO Run, and user request are not restarted.  Cross-engine
fallback may run only before any Tool execution, possible side effect, or
user-visible partial answer; afterwards it is reported as blocked.  The failed
primary call may still be billable; that fact is reported without claiming an
exact charge when usage is unknown.

Completed Tool call IDs are checked before execution.  An exact re-emission
reuses the already-bound result; a changed call with the same ID and a
concurrent duplicate both fail closed.  Fallback continuation also carries a
typed request contract containing the source privacy level, Tool registry,
invocation context, reasoning/request parameters, and HERV3 stream-inactivity
timeout.

The hard enforceable default is the maximum count of additional calls.  A
known configured ceiling is checked before the call with the best available
bounded estimate; an unknown price requires `allow_unknown_cost=true` or an
interactive confirmation.

Provider options may expose an explicit
`estimated_fallback_call_cost_usd`.  Only finite, non-negative configured
values become known route costs; missing or malformed values remain unknown
and cannot silently bypass the unknown-cost rule.  Invalid privacy facts also
fail closed instead of being substituted with another privacy policy.

## Reporting

The public failure keeps these concepts separate and labelled:

- same-target transport retry count;
- invalid-Tool repair count;
- Turn-wide cumulative local recovery count;
- the active interaction's recovery used/limit;
- fallback target-switch count and configured limit;
- final fallback disposition: disabled, ask-required, blocked, unavailable,
  attempted-success, or attempted-failed.

When same-target recovery ran and the request still failed, the localized
public message says `Retried N times, but the request still failed.` /
`已重试 N 次，仍然失败。` (locale catalog entries).  The report then states
whether fallback was attempted and, when it was not, the first concrete
reason.  A Turn-wide cumulative value is never displayed as a ratio against a
per-interaction limit.

## Consumers and tests

- `adapters/openrouter_api.py` (and its DeepSeek/xAI/Ollama siblings) expose
  `set_provider_fallback_port` / `continue_generation`; the PAO-owned HER
  bridge supplies the resolved decision and creates the authorised target.
- `orchestrator/her_v2/runtime_invocation.py` resolves only pre-effect stage
  fallback (HERV3 model target or Codex CLI), never restarting the Turn.
- `orchestrator/runtime_her_fallback.py` renders `/fallback` from this one
  policy; locked choices show a plain reason.
- Focused tests: `tests/test_model_fallback_policy.py`,
  `tests/test_herv3_fallback_consumers.py`,
  `tests/test_runtime_her_fallback.py`, plus the reporting tests in
  `tests/test_runtime_delivery.py`.

Source, offline verification, adoption, and live verification are separate
facts.  This document records source and offline status only.
