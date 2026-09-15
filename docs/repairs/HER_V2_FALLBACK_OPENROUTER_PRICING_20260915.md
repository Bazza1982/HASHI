# HER v2 fallback and OpenRouter-only pricing repair — 2026-09-15

## Approval

- The current user approved the pricing repair and HER v2 fallback deployment
  on HASHI1.
- The follow-up steer fixed the long-term policy: OpenRouter's public model
  price list is the sole automatic schedule source. No official Provider
  pricing API or copied Provider time-band schedule is permitted.
- Operational authority was limited to one `/reboot min` for Lily. No Core
  change, instance restart, shared Functions replacement, or other Agent reboot
  was authorized.

## Implementation

- Owners: PAO for derived price facts and partial-cost rendering; HER v2 for
  request-observed fallback and physical-call attribution; runtime renderers
  for localized command, menu, and warning delivery.
- Engineering layer: replaceable Functions and runtime language catalogues.
- Native `deepseek-flash` and `deepseek-v4-pro` identities map to the shared
  OpenRouter-managed aliases `~deepseek/deepseek-flash-latest` and
  `~deepseek/deepseek-pro-latest`. Missing exact evidence remains unknown; no
  historical static DeepSeek row may replace it.
- HER v2 retains its existing single safe same-target recovery, then may use
  same-Provider Level 1 and configured cross-Provider Level 2. Light may
  upgrade to Pro; Pro never downgrades. A required localized warning precedes
  each switch, and replay remains blocked after uncertain effects.
- The response guard is limited to time awaiting meaningful SSE lines for one
  physical request. It excludes local parsing and Tool execution and never
  wraps a complete Provider invocation, stage, or Turn.
- Every physical primary, retry, and fallback request is metered separately;
  an unreceipted failed request remains unknown while known calls retain their
  subtotal.
- Source branch: `deploy/aligned-20260914-HASHI1`. The implementation commit is
  the commit containing this record.

## Offline validation

- Pricing, metering, and exact DeepSeek/OpenRouter mapping: 91 passed.
- DeepSeek/OpenAI-compatible streaming guard and typed StageProvider wiring:
  69 passed.
- HER v2 fallback configuration, bilingual UI, and command binding: 90 passed.
- Fallback/retry/replay-safety runtime matrix: 4 passed.
- Ruff, Python compilation, locale JSON parsing, `git diff --check`, and the
  protected-Core checker passed.
- An isolated Function Worker reached READY on generation
  `sha256:7ecf27b6ff85b7e8c5233c49b9a7ff170547fcf8cd12c76b92129074ecc367db`.
- One broader pre-existing recovery test still expects a candidate Worker to
  mark a live leased execution as ERROR. It is unrelated to this change and
  was not rewritten to undo the published lease protection.

## HASHI1 live verification

- Agent: Lily only.
- `/reboot min` receipt: `d69cd76c29a74d7196e5a28c86e35c92`, succeeded.
- Authoritative topology records Lily on generation
  `sha256:7ecf27b6ff85b7e8c5233c49b9a7ff170547fcf8cd12c76b92129074ecc367db`.
- Core PID 16361 and shared Functions PID 16380 were unchanged. Shared
  generation `sha256:c799eabb5dac93097ef0c639b31a43c7c5235663dd76120ccbf508db319515fa`
  was not replaced.
- `/fallback` became available after adoption and persisted these routes:
  Level 1 Pro `deepseek-api/deepseek-v4-pro`; Level 2 Light
  `openrouter-api/deepseek/deepseek-v4-flash`; Level 2 Pro
  `openrouter-api/deepseek/deepseek-v4-pro`.
- Lily's primary targets remained `deepseek-api/deepseek-flash` and
  `deepseek-api/deepseek-v4-pro`.
- A Backend API smoke completed in 9.167 seconds. Two real Provider requests
  produced `cost_known=true`, an OpenRouter-reference estimate of
  US$0.019915, and the frozen OpenRouter evidence revision
  `openrouter:sha256:681041f527703d6a86a4730a0eb8a7530f55f03a39d37710feb5a435f897cef3`.
- The primary model was healthy, so no artificial outage was introduced and a
  live fallback transition was not claimed. The guarded failure transition is
  covered by the focused tests above.
- No other HASHI1 Agent was rebooted. Sunny and the other running Agents remain
  on their previous generation; live adoption is not claimed for them.
