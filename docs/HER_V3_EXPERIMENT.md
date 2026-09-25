# HER v3 experimental runtime

## Scope and adoption

This experiment is isolated on HASHI3 branch `experiment/her-v3-hashi3`, based on
the current `main` at `6a36caf5`. It carries only the HER v3 engine/harness
change from `exp-herv3`; the branch's earlier JEV style experiment and unrelated
changes are excluded. HASHI1, HASHI2, HASHI4, GitHub `main`, and the running
HASHI3 Worker were unchanged when this branch was staged. Source checks alone
do not prove live adoption; the later scoped adoption is recorded below.

The 2026-09-25 approval covered this HASHI3-only transplant and continued
testing through a live HER v3 run, not a merge into `main`. The implementation
is local to this experimental branch. Offline and live verification are
recorded separately below.

HER v3 deliberately removes mandatory cognitive orchestration from the foreground path.
The model owns reasoning, planning, adaptation and verification inside one continuous
model/tool conversation. HASHI continues to own PCM, Session continuity, tools,
permissions, recovery, audit, user commentary and learning.

## Foreground path

`PCM -> main model <-> tools -> delivery`

Triage, Planning, Replanning and Review are no longer reachable foreground stages.
Legacy code/config names remain temporarily where they are part of stable adapter or
persistence contracts; they do not select different workflows.

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

## Strategy Cards

Cards are optional reference context. When disabled, they are absent. When enabled,
the same main model may use useful guidance but does not have to select cards, produce a
strategy handoff, or report card IDs.

## Persona Commentary

The main model may produce commentary naturally. HASHI rate-limits user-visible updates
to roughly 2-3 minutes and only forwards substantive progress. On the first real tool
operation HASHI may emit one initial acknowledgement. Commentary never becomes task
instructions and never counts as task progress.

## Agent Companion

AC is an optional 5/10-minute liveness observer. JEV receives bounded observable runtime
state (tool names, hashes/status, activity/progress timing), not hidden reasoning. A
`continue` or `unknown` judgement is silent. A high-confidence `trouble` judgement queues
one notice at the next safe tool boundary; that one tool action is not executed and the
same main model decides whether to change direction, report a blocker, or finish.
Deterministic cycle control and user cancellation remain independent safety controls.

## Habit Reflection

Habit/Meditation reflection remains a post-delivery background learning path.
The separate JEV style-finalisation experiment from `exp-herv2j` is not part
of this HASHI3 engine transplant.

## Local verification and next testing

The HER adapter, core gate, and model-catalogue assertions were updated for
the single main-model/tool loop. A fake-provider fixed-Session PCM path and
HER v3 contract tests pass. The earlier `python -I -m pytest -q --tb=short`
result (708 passed, 1 skipped) was the **curated Core gate**, not the full
offline product suite. Explicit old HER v2 runtime tests still fail because
they expect removed Triage/Planning/Quick/Pro stages; they are not evidence of
HER v3 correctness. Focused HER v3 presentation, Frontend, command, locale,
adapter, and contract tests pass. These source checks alone do not establish a
live Worker or real-provider run.

## HASHI3 live verification (2026-09-25)

The approval covered this HASHI3-only experiment, all-Agent HER v3 availability,
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
restored service. A real-manager regression reproduced the failure, the HER v3
provider target now bootstraps directly, and later shared replacements
succeeded with all nine Workers online. A subsequent live red Run exposed the
internal compatibility Engine in a Session assistant-message source; the
public projection was fixed and the rerun stored `source=her-v3`.

Live acceptance used the formal Session API and durable usage records:

- `req-phd_1-2026-09-25_221320-0001` ran `deepseek-flash` at effort `off`,
  returned exactly `HER_V3_FLASH_PUBLIC_SOURCE_OK`, recorded one Provider call,
  zero reasoning tokens, and public Session source `her-v3`.
- `req-phd_1-2026-09-25_221320-0002` ran `deepseek-v4-pro` at effort `max`,
  returned the correct `7^222 mod 1000 = 049`, recorded one Provider call and
  547 reasoning tokens, and public Session source `her-v3`.
- `worker-1` switched from `codex-cli / gpt-5.6-luna` to HER v3 through
  `/backend`; `req-worker-1-2026-09-25_221315-0001` then returned exactly
  `HER_V3_BACKEND_SWITCH_OK` through `deepseek-v4-pro`. The Agent was restored
  to its original Codex model and `max` effort afterward.
- `/metre summary` reported the Pro Run as `deepseek-api /
  deepseek-v4-pro`, one call, 17,801 tokens, and US$0.003296. `/token` showed
  the HER v3 public label and no public HER v2 bucket. `/usage` presents
  pre-attribution history as `historical-unattributed` / `历史未归属` instead
  of exposing the retired storage placeholder `role-configured`.

The final retained `phd_1` setting is `deepseek-api / deepseek-v4-pro` at
effort `high`. These checks establish HASHI3 Backend API selection, persistence,
real DeepSeek execution, provider reasoning, Session presentation, token
accounting, and meter reporting. They do not authorize a merge or deployment to
another instance, and do not qualify optional Strategy Cards, Agent Companion,
long-running processes, or Habit Reflection.
