# HERV3 Privacy Level 2 Feasibility Pilot

**Decision date:** 2026-10-02

**Scope:** HASHI2 `feature-privacy` source branch; no running Worker adoption

**Owner:** HERV3 Function, with later PAO policy and Frontend Connector controls

## Decision and boundary

The approved next privacy level is HERV3-only. Direct API engines and other
backends must be unavailable while Level 2 is active. HERV3 may use a model
provider only when HASHI controls and checks its complete outbound request.
It must not fall back to an uninspected provider or silently lower the level.

This checkpoint proves the critical text-path premise with DeepSeek. It does
**not** activate `/privacy 2`: `require_level_available` still reserves it,
and the existing Level 0/1 behaviour stays unchanged. The earlier
[Level 2 plan](HASHI_PRIVACY_LEVEL_2_PLAN.md) selected direct API engines;
that engine-eligibility decision is superseded by this HERV3-only decision.
Other historical measurements and threat-boundary requirements remain useful.

## Pilot implementation

- HERV3 marks its model-provider adapters as HERV3-scoped at the main,
  fallback, and Persona invocation paths. A direct adapter invocation at
  Level 2 refuses to call the provider.
- The common API adapter builds each complete request, then applies the local
  gate before provider-wire evidence or HTTP transmission. This runs again
  after every tool result and on streaming requests and retries.
- The gate inspects nested text, system/history content, tool schemas,
  arguments, and results. It replaces detected spans with typed placeholders
  whose identities remain stable within one invocation. It rejects sensitive
  JSON keys, unsupported media, malformed model results, missing model,
  timeout, and payloads above the pilot size limit. It does not log the
  source-to-placeholder mapping.
- Presidio, spaCy, and the English model run in a separately configured Python
  sidecar. The running HASHI interpreter receives none of those dependencies.
  The sidecar returns labels and offsets, never source text.

## Evidence

The focused tests were red before implementation because the privacy gate did
not exist. After implementation, they demonstrate initial-request filtering,
second-call tool-output filtering, streaming filtering, fail-closed errors,
and direct-backend refusal. An isolated local-model test detects synthetic
`Jordan Lee` and `jordan@example.com` and retains the two `$50` amounts.

An opt-in live DeepSeek canary used those fabricated values only. The guard
at the actual HTTP method checked that both raw strings were absent and both
placeholders were present before the request proceeded. DeepSeek returned the
correct total of 100. This establishes that the model can still perform that
reasoning task on redacted input; it does not measure performance on all tasks.

The existing 19-item synthetic fixture yielded 14/19 detected values with
Presidio/spaCy. Chinese name, address, and passport examples were missed.
This detector is **not sufficient for general Level 2 activation**. A live
canary proves its exact request only, not every future outbound path.

## Release gates after this pilot

1. Make HERV3 the only compatible outer backend at Level 2, and enforce a
   qualified HERV3 model-provider allowlist. Reject uninspected CLI,
   multimodal, fallback, and auxiliary call paths before sending anything.
2. Improve and independently evaluate the local detector, including Chinese
   PII, secrets, and attachment/OCR text. Package and monitor the isolated
   runtime on both WSL and Windows.
3. Cover concurrent requests, restoration, all retries, cross-provider
   switches, file and media inputs, and recovery with failure-boundary tests.
4. Only then enable the `/privacy 2` control and validate a full HASHI2 live
   Session through its configured frontend. Keep downgrade confirmation and
   request-level privacy snapshot semantics.

No reboot, merge into the running HASHI2 checkout, or live `/privacy` setting
change is part of this pilot.
