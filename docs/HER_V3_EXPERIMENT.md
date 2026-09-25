# HER v3 experimental runtime

## Scope and adoption

This experiment is isolated on HASHI3 branch `experiment/her-v3-hashi3`, based on
the current `main` at `6a36caf5`. It carries only the HER v3 engine/harness
change from `exp-herv3`; the branch's earlier JEV style experiment and unrelated
changes are excluded. HASHI1, HASHI2, HASHI4, GitHub `main`, and the running
HASHI3 Worker are unchanged. Source checks do not prove live adoption.

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

## Local testing priority

Compile/import, HER v3 contract tests, and a fake-provider fixed-Session PCM
path have passed. Existing HER v2 tests that assert removed staged routing still
need migration or replacement; they are not evidence of HER v3 behaviour.
Before merging anywhere, test real providers locally: fixed Session PCM deltas,
model/tool continuity, `/effort`, Strategy on/off, AC 5/10-minute behaviour,
commentary cadence, long-running managed processes and Habit Reflection.
