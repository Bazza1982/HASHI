# HERv2J: optional final style pass

Branch: `exp-herv2j`. Base: `6073915a8694b5b9e5ed83f17f43e2cbe1498e2b`.
Owner: HER v2. Engineering layer: Functions, with Agent-local opt-in configuration.
Status: experimental implementation; real JEV/Quick inference and local adoption remain to be tested.

## The one change

After a normal text answer is ready, optionally ask TypeSafe/JEV whether its
**wording** follows this turn's system/style/Persona instructions. A clear
mismatch triggers **one** silent rewrite using the configured HER **Quick/light**
provider and model, with reasoning off and no tools. A keep/uncertain result
publishes the original. There is no second check, retry loop, Pro fallback,
review, replanning, fact check, evidence check, completion decision, or new work.
Execution commentary is unchanged; neither added call publishes commentary.

Disabled is the default. Direct, Strategic and Planned retain their existing
execution paths and model choices. Direct still has one primary execution
invocation; enabling the experiment adds separate presentation calls, not a
second execution agent. The legacy full `Stage.FINALISATION` is not activated.

## Configure and try locally

1. Pull this branch into a clean test checkout, preferably a separate worktree.
2. Set `TYPESAFE_API_KEY` in the environment used to launch the test HASHI
   instance/Functions. Do not put the API key into chat, `/model`, tracked files,
   screenshots, or logs. No TypeSafe SDK or skill installation is required.
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
    "check_timeout_s": 5.0,
    "rewrite_timeout_s": 20.0,
    "rewrite_probability": 0.7
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
current user request is kept separately. History, memories and the answer draft
are not promoted into system policy. The same immutable snapshot is used for both
calls; changes made while execution runs do not change the editor's instructions.
For non-fixed compatibility calls, sources are read once from configured PCM and
active `/sys` managers. A failed snapshot skips the optional editor.

The original execution record remains unchanged. The selected final text is used
for **both delivery and TurnResult**, so the conversation does not remember a
response different from the one displayed. Input over 64,000 characters is not
silently truncated; the optional pass is skipped. Exact output formats and
substantive content must be preserved by the prompts, but this experiment does
not add a factual or semantic-verification gate.

Covered exits: `_run_direct()`, normal Strategic/Planned work completion, and the
existing post-triage Direct route when Immediate was skipped. Not covered:
provisionally delivered parallel Immediate answers, progress commentary, native
voice/rich output, clarification/error notices, and the dormant high-effort full
review/finalisation flow. Their scheduling and delivery remain unchanged.

## Observability and basic tests

Internal events: `style_checked`, `style_rewritten`, `style_degraded`,
`style_skipped`, with instruction snapshot hash and selected rewrite target.
Per-call usage stays in the existing meter under `style_check` and `style_rewrite`.
Unavailable usage/pricing is not reported as free. API credentials and response
bodies are not added to these style-decision events.

Focused offline checks:

```bash
python -m pytest -q tests/test_her_v2_final_style.py
python scripts/check_protected_core_changes.py
```

The local sandbox passed 18 non-UI cases (two UI cases require the repository's
Telegram dependency). They cover the disabled path, keep/uncertain decisions,
one rewrite, failure fallback, cancellation/audit failure, normal final delivery
in all three modes, unchanged Immediate behavior, once-only/rich-output guard,
configuration roundtrip, typed instruction capture, mocked TypeSafe transport,
Quick routing, and the actual silent tool-free provider helper. UI cases exercise
saved settings, Hybrid staging/apply, localized labels and escaping. A temporary
mutation that bypassed the final-style hook made the Direct integration case
fail; restoring the hook passed. The mutation is not committed.

No real JEV/Quick calls, production restart, live frontend acceptance, broad
regression suite, or measured quality/latency claims are part of this change.
For a small local comparison, reuse identical prompts with the option off/on,
then inspect the actual wording, additional delay, and call receipts.

Isolated Python 3.12 runner, full focused module (including UI): `20 passed in 1.36s`.
