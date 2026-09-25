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

`/model` and `/provider` show the effective `main` target, rather than the
retired Quick/Pro route editor. This experimental menu is read-only: change the
main target in local configuration. The Frontend presentation status reports
`her_v3.main`, while the backend ID remains `her-v2` for stored compatibility.
Old route buttons are rejected rather than allowed to change ignored settings.

`/effort` is model reasoning only. The accepted HER wire values are `none`, `low`,
`medium`, `high`, `xhigh`, and `max`; provider adapters map them to the actual API
capability. For example DeepSeek currently reduces intermediate values to its supported
thinking levels rather than changing HASHI workflow.

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

The HASHI3 root checkout now runs local branch `experiment/her-v3-live-hashi3`;
the adopted Worker source was `ba5eab45`. `main` remains at `6a36caf5`, and
GitHub `main` was not changed.
After all nine HASHI3 Agents were observed idle, only `phd_1` received an
Agent-scoped hot `/reboot min` (receipt `22df43de1dc042b9834390c29705448a`,
status `succeeded`). The adopted Worker presents `her_v3.main` as
`hashi-api / gpt-6-astra`; `/model` and `/effort` show the single main target and
six reasoning choices. Its pre-existing `low` effort was not changed.

Two real-provider Runs through the HASHI3 Backend API completed without
Telegram mirroring. Request `req-phd_1-2026-09-25_175648-0001` returned the
requested `HER_V3_LIVE_OK`; its final activity had
`provenance=her_v3_single_loop` and `planner=false; replanner=false;
reviewer=false`. Request `req-phd_1-2026-09-25_175648-0003` called the
read-only PowerShell UTC-time tool, received `status=success`, and then
answered using that result with the same HER v3 provenance. An intervening
tool Run failed because Windows PowerShell did not support `Get-Date -AsUTC`;
the Agent reported that error honestly, and the corrected command succeeded.

Later on 2026-09-25 the `email-agent-1` instance configuration was given an
explicit HER v3 `main` target, `deepseek-api / deepseek-v4-pro`, replacing its
implicit fallback to the legacy premium profile. Its pre-existing DeepSeek
credential was present; other Agents were not reconfigured. Agent-scoped hot
adoption receipt `aad9494d2f99491484ab1587acb63bf0` succeeded. The
Agent-owned `/effort high` command also persisted `high` in its workspace
state, while its declared main profile uses DeepSeek reasoning `high`.
Request `req-email-agent-1-2026-09-25_190631-0001` returned
`DEEPSEEK_HERV3_LIVE_OK`. Requests
`req-email-agent-1-2026-09-25_190631-0002` and
`req-email-agent-1-2026-09-25_190631-0003` each executed a
read-only UTC-time shell Tool; their activity origin was
`her_v2:deepseek-api`, the Tool returned `status=success`, and the final answer
carried `provenance=her_v3_single_loop`. The last request ran after effort was
set to `high` and included DeepSeek thinking deltas. All three Runs disabled
Telegram mirroring and performed no mailbox actions.

This proves the real main-model/tool loop with HASHI API and DeepSeek on two
HASHI3 Agents. It is not the full external-frontend acceptance suite, nor proof
for all providers, effort levels, Strategy Cards, Agent Companion, long-running
processes, or Habit Reflection. The live shared `/api/backends/catalogue` now
serves `HER v3 (experiment)` and all six reasoning choices. A separate earlier
HASHI3 `/reboot max` receipt (`66cba3bb4a74471e91445ae1429135c7`) records a
committed shared-Function replacement; the Agent-scoped DeepSeek reboot above
did not perform that replacement. The shared catalogue still uses
`role-configured` as its model placeholder, not a selectable DeepSeek model
list. Inspect the Agent-specific `/model` or runtime status for its actual
`deepseek-api / deepseek-v4-pro` main target. This does not establish full
Workbench model-selection acceptance.

Before merging or adopting elsewhere, test more real-provider cases on this
HASHI3 experiment: fixed Session PCM deltas, model/tool continuity,
`/effort`, Strategy on/off, AC 5/10-minute behaviour, commentary cadence,
long-running managed processes, and Habit Reflection. An Agent-scoped hot
reboot adopts source from the HASHI3 root checkout, not a separate worktree.
