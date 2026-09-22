# HERv2J: optional final style pass

Branch: `exp-herv2j`. Base: `6073915a8694b5b9e5ed83f17f43e2cbe1498e2b`.
Owner: HER v2. Engineering layer: Functions, with Agent-local opt-in configuration.
Status: experimental; HASHI2 Arale canary adopted and checked on 2026-09-22,
with broader rollout and comparative quality evaluation still pending.

## The one change

After a normal text answer is ready, optionally ask TypeSafe/JEV whether its
**wording** follows this turn's system/style/Persona instructions. A clear
mismatch triggers **one** silent rewrite using the configured HER **Quick/light**
provider and model, with reasoning off and no tools. A keep/uncertain result
publishes the original. There is no second check, retry loop, Pro fallback,
review, replanning, fact check, evidence check, completion decision, or new work.
The selected Choice is the application decision: `rewrite` always invokes the
editor, while `keep` and `uncertain` publish the original. Probability metadata
does not override the selected Choice.
Execution commentary is unchanged; neither added call publishes commentary.

Disabled is the default. Direct, Strategic and Planned retain their existing
execution paths and model choices. Direct still has one primary execution
invocation; enabling the experiment adds separate presentation calls, not a
second execution agent. The legacy full `Stage.FINALISATION` is not activated.

## Configure and try locally

1. Pull this branch into a clean test checkout, preferably a separate worktree.
2. Set `TYPESAFE_API_KEY` in the environment used to launch the test HASHI
   instance/Functions, or store it as `typesafe_api_key` in the instance-local,
   ignored `secrets.json`. The environment takes precedence. Do not put the API
   key into chat, `/model`, tracked files, screenshots, or logs. No TypeSafe SDK
   or skill installation is required.
3. Select HER v2, open `/model`, then **Style finalisation**. Choose **ON**.
   The equivalent text command is `/model style on`; inspect with `/model style`.
4. In Hybrid mode (or while another configuration draft is open), use the
   existing **Apply draft** button or `/model apply`. Until Apply, the effective
   configuration remains unchanged. Single-provider settings save immediately.
5. The rewrite model is the normal **Quick** selection in `/model`. It is not
   hard-coded to DeepSeek or to a particular Flash model. Changing Quick changes
   this editor for future turns as well.
6. Disable with `/model style off` (and Apply when editing a draft).

The setting is persisted using the existing HER model-configuration transaction.
It affects future turns; an active turn keeps its captured option and target.
Enabling explicitly permits the draft and applicable instructions to be sent to
TypeSafe, and mismatching drafts to the configured Quick provider. Missing keys,
service errors, malformed answers, or editing timeouts retain the original text
and emit an internal degradation record, not an extra user-facing message.
Stop/cancel and mandatory audit-persistence failures keep their existing meaning.

Advanced non-secret parameters may be supplied under the existing `her_v2`
configuration; `/model` persists only the ON/OFF preference:

```json
{
  "style_finalisation": {
    "enabled": false,
    "model": "jev-latest",
    "api_key_env": "TYPESAFE_API_KEY",
    "api_key_secret": "typesafe_api_key",
    "check_timeout_s": 5.0,
    "rewrite_timeout_s": 20.0
  }
}
```

These are experimental defaults, not measured latency promises or calibrated
accuracy guarantees. The endpoint is fixed to `https://api.typesafe.ai/v1/systemone`
with TLS verification and no redirects. The request/response contract was checked
against the official `typesafe-ai/typesafe-sdk-python` source at
`0ffd094c72ed9445223060b24ffd7a56aa781fb4` (constants, endpoints, and Choice answer).
No new dependency is added to Core or to the repository's runtime lock files.

## Instruction sources and publication

The fixed HER session supplies typed, accepted PCM sections: permanent `[sys]`,
instance-global `/sys`, Agent-local `/sys`, and presentation Persona. The actual
current user request is kept separately and is read from either the fixed
`initial_turn` or `turn` envelope. History, memories and the answer draft are not
promoted into system policy. The same immutable snapshot is used for both calls;
changes made while execution runs do not change the editor's instructions. For
non-fixed compatibility calls, sources are read once from configured PCM and
active `/sys` managers. A failed snapshot preserves the original answer and
publishes a `style_degraded` receipt instead of disappearing into a warning.

The original execution record remains unchanged. The selected final text is used
for **both delivery and TurnResult**, so the conversation does not remember a
response different from the one displayed. Input over 64,000 characters is not
silently truncated; the optional pass is skipped. Exact output formats and
substantive content must be preserved by the prompts, but this experiment does
not add a factual or semantic-verification gate.

Covered exits: `_run_direct()`, post-triage Direct, parallel Immediate answers
once Triage confirms they are final, normal Strategic/Planned work completion,
required clarifications, and the high-effort Review/Finalisation report. The
pass still runs at most once per turn. Provisional acknowledgements and progress
commentary are not terminal text and are not edited. Native voice/rich output,
deterministic technical errors, stop notices, and runtime-authored safety
fallbacks remain exact and are intentionally excluded. When the feature is
enabled, a rich terminal result records `style_skipped` rather than silently
bypassing the gate.

## Observability and basic tests

Internal events: `style_checked`, `style_rewritten`, `style_degraded`,
`style_skipped`; once initialisation has a snapshot, events also carry its hash
and selected rewrite target.
Every eligible terminal text on an enabled Turn must publish at least one terminal
style outcome: checked, skipped, or degraded. Initialisation failure is a degraded
outcome; oversized, empty, and rich terminal output are explicit skipped outcomes.
Per-call usage stays in the existing meter under `style_check` and `style_rewrite`.
Unavailable usage/pricing is not reported as free. API credentials and response
bodies are not added to these style-decision events.

Focused offline checks:

```bash
python -m pytest -q tests/test_her_v2_final_style.py
python scripts/check_protected_core_changes.py
```

The focused suite covers the disabled path, keep/uncertain decisions, one
rewrite, failure fallback, cancellation/audit failure, every eligible terminal
text exit, once-only/rich-output guards, exact runtime error fallbacks,
configuration roundtrip, typed instruction capture, mocked TypeSafe transport,
Quick routing, and the actual silent tool-free provider helper. UI cases
exercise saved settings, Hybrid staging/apply, localized labels and escaping.

No broad rollout, formal frontend acceptance, or measured quality/latency claim
is part of this experiment. For a small local comparison, reuse identical
prompts with the option off/on, then inspect the actual wording, additional
delay, and call receipts.

Isolated Python 3.12 runner, full focused module (including UI): `21 passed`.

## HASHI2 canary record — 2026-09-22

- **Approval:** the current user explicitly requested that branch `exp-herv2j`,
  including expected feature commit `f048106`, be loaded and tested on HASHI2
  Arale. That approval covered one Agent-scoped hot reboot, not a Core restart
  or wider rollout.
- **Implementation:** commit `cff725ed` adds a local HASHI-secret fallback while
  retaining environment-variable precedence. The API key remains only in
  ignored, protected instance configuration.
- **Offline validation:** the focused final-style and HER configuration suites
  passed 54 tests; the protected-Core gate and whitespace check passed.
- **Live adoption:** `/reboot min` receipt
  `fec3e1909bfa47f2afc452b6dbbda180` succeeded for target `arale`. Core PID 882
  remained unchanged while Arale moved from Function generation `9c291aaa` to
  `184a94e3`. `/model` then exposed the independent Style finalisation toggle
  and the configured Quick target `deepseek-api / deepseek-flash`.
- **Live check:** a temporary Direct-mode canary produced a completed
  `typesafe-api` `style_check` receipt using `jev-1.13.0`; the user's saved
  Strategic (`low`) mode and Style finalisation `OFF` state were restored after
  the test. This canary confirmed live JEV decision traffic. The rewrite branch
  was separately exercised with the same real JEV and Quick provider before
  adoption; it was not forced when the live canary already met its requested
  style.

## HASHI2 bypass and recovery correction — 2026-09-22

- **Approval:** the current user approved correcting the observed recovery bug
  and all model-authored terminal exits that bypassed Style finalisation on the
  HASHI2 experiment branch.
- **Observed red evidence:** Arale produced JEV receipts for a normal final
  answer, but recent Direct and clarification turns had no `style_check`
  receipt. A normal clarification also left a 23-record WIP shadow, and
  `/compact` reported the 64,000-token history threshold instead of clearing
  that stale shadow because a settled canonical recovery row was mistaken for
  an active recovery.
- **Implementation:** Direct answers, clarifications, and high-effort
  Finalisation reports now use the same once-only style pass. Settled
  `PENDING_USER_INPUT` boundaries clear their WIP shadow. `/compact` ignores an
  explicitly settled canonical recovery row, while unknown or genuinely
  unsettled canonical recovery remains fail-closed and visibly retained.
- **Boundary:** commentary, acknowledgements, rich output, stop notices, and
  deterministic technical/safety text remain outside the style pass.
- **Offline validation:** 44 focused Style/WIP/Compact regressions passed; the
  extended HER v2 runtime/adapter/recovery suite passed 311 tests with one
  intentional skip. Changed Python files compiled, locale catalogues validated,
  and the protected-Core gate passed.
- **Adoption:** source and offline verification are separate from the running
  HASHI2 Function generation. Live adoption requires separately authorised
  Function replacement.

## HASHI2 final-style receipt hardening — 2026-09-22

- **Approval:** the current user approved the `exp-herv2j` correction for both
  fixed-envelope shapes and mandatory Style outcome evidence on HASHI2.
- **Implementation:** fixed-session capture accepts `initial_turn` and `turn`;
  initialisation failure records `style_degraded`; every enabled eligible final
  records checked, skipped, or degraded, including an explicit rich-output skip.
- **Offline verification:** focused Style, Workzone, adapter, and fixed-session
  regressions pass. Protected-Core and broader runtime gates are recorded at the
  implementation checkpoint.
- **Live verification:** not yet performed. Source completion does not authorize
  or prove Function-generation adoption on running Arale.

## HASHI2 authoritative Choice correction — 2026-09-22

- **Approval:** the current user directed HER v2 to remove the probability gate
  and treat JEV's selected Choice as the deterministic style decision.
- **Implementation:** `rewrite` now always invokes the configured Quick/light
  editor against the captured system/style snapshot; `keep` and `uncertain`
  retain the original. Choice probabilities are no longer parsed or configured.
- **Red evidence:** before the correction, a selected `rewrite` with probability
  `0.40` incorrectly published the original because of the `0.70` gate.
- **Offline verification:** the focused HER v2 Style/runtime/adapter/configuration
  component run passed 272 tests with one intentional skip. Python compilation,
  diff hygiene, and the protected-Core check passed. The separate Agent FYI
  budget test remains a pre-existing branch failure because tracked `HEAD`
  already exceeds its 12,000-character loader limit; this change does not hide
  or raise that limit.
- **Live verification:** not yet performed. Source and offline checks do not
  authorize or prove adoption by Arale's running Function Worker.
