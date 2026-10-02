# HERV3 Privacy Level 2 Trial Decision

**Decision date:** 2026-10-02

**Scope:** HASHI2 source implementation developed on `feature-privacy`.
`feature/privacy-level2-merge` is the privacy-only candidate for `main`.
Running adoption and live verification are separate facts, recorded below.

**Owner:** HERV3 Function for the outbound model boundary; PAO Function for
Level 2 eligibility, persistence, and `/privacy` controls.

## Decision and boundary

The approved Level 2 trial proactively uses a replaceable **local** PII detector
to mask detected values before an online model call. The current detector is
Presidio/spaCy with an English model in a separate Python runtime. A trusted
local script can replace it through `HASHI_PRIVACY_FILTER_SCRIPT`; the script
returns validated spans through a local JSON pipe. The interpreter is selected
with `HASHI_PRIVACY_FILTER_PYTHON` or an isolated root `.venv-privacy` runtime.
Detected values receive typed placeholders scoped to one invocation. This is
masking, not encryption or anonymity; no reversible mapping is persisted.

No detector can guarantee 100% recall, especially across languages. The known
misses are an inherent limitation of this level, disclosed before activation.
The user may accept the residual risk, choose a stronger future level, or
avoid processing sensitive information with online AI. A detector outage,
malformed result, unsupported media, or programmatic bypass is a different
failure: the request must be blocked, never sent unfiltered.

Only HERV3 is a Level 2 outer backend. Only its qualified DeepSeek provider is
enabled in this trial. Direct API engines, CLI backends, HERV3 Hashi/OpenRouter
providers, Agent Companion, and uninspected media paths are disabled. They
cannot silently lower the privacy level. The older
[Level 2 plan](HASHI_PRIVACY_LEVEL_2_PLAN.md) proposed direct API eligibility;
that proposal is superseded.

## Pilot implementation

- The manager creates a qualified HERV3 provider only after provider policy
  checks. Main, fallback, Persona, and compaction paths use this factory.
  Direct ephemeral or adapter calls at Level 2 refuse provider transport.
- The common API adapter builds each complete request, then applies the local
  gate before provider-wire evidence or HTTP transmission. This runs again
  after every tool result and on streaming requests and retries.
- The gate inspects nested text, system/history content, tool schemas,
  arguments, and results. It replaces detected spans with typed placeholders
  whose identities remain stable within one invocation. It rejects sensitive
  JSON keys, unsupported media, malformed model results, missing model,
  timeout, and payloads above the pilot size limit. It does not log the
  source-to-placeholder mapping.
- Presidio, spaCy, and the English model run in an isolated sidecar. The HASHI
  interpreter receives none of those dependencies. The sidecar returns labels
  and offsets, never source text. A model replacement must obey the same span
  contract and must remain local.
- `/privacy 2` presents the known risk and requires a deliberate acceptance.
  Activation checks a local detector canary, the active HERV3 target, every
  configured provider profile, and persistent state. Downgrades require
  separate confirmation. Incompatible switches fail before provider use.

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
The warning makes these misses visible; it does not imply they are fixed.
A live canary proves its exact request only, not every future outbound path.

## Approval, implementation, and adoption

- **Merge preparation (2026-10-03):** The candidate was rebuilt from the
  then-current `origin/main` with only the Level 2 commits and documentation;
  the separate Phone commits were excluded. Focused privacy, configuration,
  deployment, and UI checks passed (156 passed, 1 opt-in live canary skipped).
  Provider and package checks passed (189), as did the curated shared-runtime
  gate (736). The existing isolated detector passed its synthetic readiness
  check. This is source validation, not a merge into `main` or running Worker
  adoption.

- **Deployment preparation (2026-10-03):** `requirements-privacy.txt` now
  declares the separate detector profile, including a hash-pinned English
  model wheel. A source provisioner performs a real synthetic readiness check.
  The npm post-install flow and enterprise image prepare the sidecar outside
  Core. The size-limited Portable Windows image includes an on-target installer
  instead of bundling the large model. Its installation still needs network
  access and Windows acceptance testing. These changes do not enable an
  Agent's Level 2 setting or adopt a running Worker.

- **Approval:** The user approved this limited, replaceable-model Level 2
  definition and implementation on HASHI2 on 2026-10-02.
- **Implementation:** The `feature-privacy` source was fast-forward merged into
  HASHI2's `feat/phone-local-cascade` checkout. Focused privacy checks passed
  (41 passed, 1 opt-in live canary skipped); HERV3 adapter, compaction, and
  DeepSeek regression checks passed (229 total). Backend state/catalogue checks
  passed (84). The isolated `.venv-privacy` runtime was installed in HASHI2,
  and its local readiness probe passed without an environment override.
  Existing native-audio and cognitive-control failures reproduce on the
  untouched baseline and are outside this change.
- **Live verification:** The running HASHI2 Worker has not adopted the merged
  source, and no Agent's Level 2 setting has been persisted. The earlier
  synthetic DeepSeek canary proves the direct provider request only. Worker
  adoption, active Agent level, and a full frontend Session remain unverified.
