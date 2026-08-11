# HASHI Privacy Framework — Level 2 Design and Implementation Plan

**Status:** design, initial WSL/Windows hardware validation, and Level 0/1
control foundation complete  
**Scope of this delivery:** six-level framework and Level 2 implementation plan  
**Default behaviour:** Level 1 (`Provider Trust`)

## 1. Decision

HASHI privacy is a user-selected trade-off between model capability, trust in
the model provider, and how much information may leave the local environment.

The long-term product now reserves six states:

- **Level 0 — Privacy Off:** the privacy framework, compatibility enforcement,
  and local PII filtering are disabled.
- **Level 1 — Provider Trust:** current behaviour. Online API backends and
  online agent harnesses may receive unredacted content.
- **Level 2 — Basic Redaction:** only API backends whose complete outbound
  payload is constructed by HASHI may run. Each outbound payload must pass one
  local PII filter before network transmission.

Level 1 remains the default. Level 0 is an explicit opt-out for users who do
not want the privacy framework active at all. Both modes send unredacted
content, but Level 1 retains an explicit provider-trust posture while Level 0
disables privacy-policy controls.

Levels 2–5 appear in the menu so users can understand the complete framework,
but they remain non-activatable until HASHI can enforce their promised
boundaries. In particular, the Level 2 capability metadata is a compatibility
declaration, not proof that the outbound filter is already installed.

## 2. Level 2 Security Contract

Level 2 is a real data-boundary guarantee, not a prompt-cleaning feature.

At Level 2:

1. `openrouter-api`, `deepseek-api`, `xai-api`, and other HASHI-controlled API
   adapters may run after integration with the outbound privacy gate.
2. `codex-cli`, `grok-cli`, `claude-cli`, `gemini-cli`, `her`, and similar
   online agent harnesses are prohibited.
3. The initial user message, system instructions, restored history, attachment
   text, image/OCR text, tool arguments, tool results, retry messages, and each
   subsequent tool-loop payload must be filtered.
4. The filter runs immediately before every network request. Filtering only
   the initial Telegram prompt does not satisfy Level 2.
5. A filter exception, unavailable model, timeout, malformed result, or
   unsupported payload type blocks the request. HASHI must never fall back to
   an unfiltered request.
6. A backend not carrying an explicit Level 2 capability declaration is
   treated as Level 1 only.
7. HASHI and its agents may not lower the privacy level. Moving from Level 2
   to Level 1 requires explicit user confirmation.
8. Audit records may contain entity types, counts, timing, policy decisions,
   and hashes, but never the original sensitive values.

Level 2 reduces risk; it is not a promise that every possible identifier will
be found. The interface must say **basic local redaction**, not anonymous,
de-identified, or guaranteed safe.

## 3. Why CLI Harnesses Are Excluded

An online coding harness can read files, databases, logs, Git history, command
output, and tool results after the first prompt. It then builds its own online
requests outside HASHI's API adapter. Redacting only the starting prompt would
leave those later paths uncontrolled and create a false sense of protection.

Therefore a Level 2 backend switch behaves as follows:

```text
Level 2 active
    |
    +-- HASHI-controlled API backend -> allowed through privacy gate
    |
    +-- online CLI/agent harness ------> blocked
                                         |
                                         +-- remain on current backend, or
                                         +-- user explicitly confirms Level 1
```

## 4. Filter Choice for the First Implementation

The Level 2 filter is one local pipeline built around Microsoft Presidio:

- Presidio Analyzer provides the orchestration, confidence scores, allow
  lists, overlap handling, and deterministic recognizers.
- Australian recognizers are explicitly registered for TFN, Medicare, ABN,
  and ACN. Current Presidio releases do not enable all country-specific
  recognizers automatically.
- HASHI adds recognizers for secrets and identifiers that the default bundle
  does not reliably cover, including API keys, access tokens, passwords, and
  account-specific patterns.
- A local named-entity component may be mounted inside this one Level 2
  pipeline only after it passes the WSL and Windows acceptance test.

Candidate named-entity component:

- `urchade/gliner_multi_pii-v1` for lightweight CPU inference and flexible PII
  labels. Its language coverage is European-language focused and must not be
  represented as Chinese protection.

Alternatives remain experiments, not Level 2 dependencies:

- `openai/privacy-filter` is primarily English and larger. A quantized runtime
  may be evaluated after the first integration.
- `OpenMed/privacy-filter-multilingual` includes Chinese but is substantially
  heavier and its reported language performance is uneven.

## 5. Redaction Semantics

HASHI should replace sensitive spans with stable typed placeholders rather
than delete them:

```text
Amelia Hart -> [PERSON_1]
amelia@example.com -> [EMAIL_1]
123 456 782 -> [AU_TFN_1]
```

Requirements:

- the same value maps to the same token inside one task/session;
- tokens from different sessions are not linkable;
- overlapping detector results resolve to one strongest span;
- secrets use non-reversible `[SECRET_n]` replacement;
- reversible PII mappings stay local, are encrypted at rest, and expire;
- logs never contain the mapping or raw values;
- model responses are not automatically rehydrated into an external sink.

For the first Level 2 release, reversible rehydration should be opt-in. The
safer default is to show placeholders and let the local UI render a protected
view when needed.

## 6. HASHI Integration Points

### 6.1 Policy and capability layer

Current foundation:

- `orchestrator/privacy_levels.py` defines executable Levels 0 and 1, reserves
  Level 2 in the policy model, and provides backend compatibility and
  downgrade checks.
- `orchestrator/flexible_backend_registry.py` declares API adapters as
  potentially supporting Levels 0/1/2 and online CLI harnesses as Levels 0/1
  only. Level 2 cannot activate until the release gate passes.
- Unknown backends fail closed at Level 2.

Next:

- persist a global default and per-agent override;
- capture the effective level when a request enters the queue;
- keep that level immutable for the lifetime of the request;
- reject a backend switch before shutting down the working backend when the
  target is incompatible.

### 6.2 Local filter service

Add a process-local service with a narrow interface:

```python
class OutboundPrivacyGate:
    async def sanitize(
        self,
        payload: object,
        *,
        request_id: str,
        session_id: str,
        backend: str,
        level: int,
    ) -> SanitizedPayload:
        ...
```

`SanitizedPayload` carries:

- the filtered payload;
- entity counts by type;
- whether any unsupported content was found;
- elapsed time and filter version;
- a local mapping handle, never the mapping itself.

The service loads models lazily, performs a startup self-test, enforces a
bounded processing time, and exposes readiness separately from general HASHI
health.

### 6.3 API adapter boundary

The gate belongs at the last common point before HTTP transmission.

For `OpenRouterAdapter`, filtering must occur inside every tool-loop iteration
after `_build_payload(messages, ...)` and before `_call_api_once(...)` or
`_stream_api_once(...)`. The final request after a tool-loop limit must use the
same gate. DeepSeek, xAI, and other API adapters must call the same shared
helper rather than implement their own privacy behaviour.

The preferred extraction is:

```text
API adapter builds complete payload
            |
            v
Base/shared API outbound guard
            |
            +-- Level 1 -> unchanged payload
            |
            +-- Level 2 -> local sanitizer -> validated payload
                                      |
                                      +-- failure -> no HTTP request
```

Filtering at the Telegram handler, runtime queue, or initial `prompt` argument
is insufficient.

### 6.4 User controls

The initial user surface exposes the whole framework:

- Level 0 — Privacy Off
- Level 1 — Provider Trust
- Level 2 — Basic Redaction
- Level 3 — Strict Redaction
- Level 4 — Private Controlled
- Level 5 — Local Sovereign

Only Levels 0 and 1 are currently activatable. Levels 2–5 are visibly marked
as planned so the product model is clear without claiming protection that
HASHI cannot yet enforce.

Selecting Level 2 should show:

```text
✓ Local basic PII filtering before every API request
✓ Filter failures block transmission
✗ Codex, Grok, Claude, Gemini and HASHI Engine Runtime (HER) harnesses unavailable
△ Some names, identifiers, code, and tool output may be altered
△ Automated detection cannot guarantee complete removal
```

Selecting an incompatible harness must not silently lower privacy. HASHI
offers two choices: cancel the switch, or explicitly confirm Level 1.

## 7. Delivery Sequence

### L2.0 — Policy foundation

- backend capability declarations;
- Level 0/1/2 parser and transition policy;
- tests proving every online CLI harness rejects Level 2;
- no user-visible Level 2 activation yet.

### L2.1 — Detector acceptance

- reproducible synthetic test corpus;
- Presidio with explicitly enabled Australian recognizers;
- WSL and native Windows CPU tests;
- measure load time, per-message latency, memory, recall, and known false hits;
- decide whether GLiNER is included in the first pipeline.

### L2.2 — Outbound gate

- local filter service and stable placeholders;
- shared payload walker for text in nested JSON payloads;
- adapter integration before every HTTP call and tool-loop call;
- fail-closed error path;
- raw-value-free audit events.

### L2.3 — State and controls (foundation partially landed)

- persisted per-agent setting with Level 1 default;
- explicit Level 0 privacy-off state;
- Telegram `/privacy` details menu and `/privacy x` quick setting;
- downgrade confirmation, including Level 1 to Level 0;
- reserved Level 2–5 menu details without false activation;
- request-level privacy snapshot;
- Workbench slider;
- compatible-backend filtering in menus and switch handlers.

### L2.4 — Acceptance

- prove no HTTP call occurs when the filter fails;
- prove tool output is redacted on the second API loop;
- prove all CLI harnesses are blocked;
- prove unknown adapters are blocked;
- test text, nested tool JSON, attachments, OCR text, retries, and streaming;
- restart and recovery test with Level 2 persisted;
- document residual risks and performance cost.

## 8. Model Probe

The reproducible probe is:

```bash
python tools/pii_model_probe.py --engine presidio
python tools/pii_model_probe.py --engine gliner
```

Fixtures live in `tests/fixtures/pii_probe_cases.json` and contain synthetic
data only. The probe performs no online inference. A model repository may be
contacted once to download local weights.

Acceptance for a candidate used by Level 2:

- runs on both WSL2 and native Windows without a GPU;
- remains inside the available memory budget;
- detects the Australian structured fixture;
- does not corrupt text when spans overlap;
- has a documented language boundary;
- does not make any inference request after local model loading;
- uses a licence compatible with HASHI distribution.

The small fixture is a platform and suitability probe, not a production
accuracy benchmark. A larger, independently reviewed corpus is required before
Level 2 can be called ready.

### 8.1 Test environment

- **WSL:** WSL2, Python 3.10.12, 4 visible CPU threads, 7.8 GiB RAM, no
  available NVIDIA device.
- **Windows:** Windows 11, native Python 3.12.13, 24 visible CPU threads,
  29.65 GiB RAM, CPU inference.
- **Pinned model stack:** GLiNER 0.2.28 and Transformers 4.57.6 for parity.
- **Windows packaging note:** the machine's default Python is 3.14.5. The
  successful test used an isolated Python 3.12 environment because that is the
  supported packaging target for the current Presidio stack. HASHI must ship
  or manage this runtime instead of depending on the user's default Python.
- **Console note:** Windows PowerShell 5.1 required explicit UTF-8 output for
  the Chinese fixture. The probe now configures UTF-8 itself.

### 8.2 Results on the synthetic fixture

The fixture contains 19 expected sensitive values plus known benign values.
Recall below means “the value was covered by a detected span”; it does not
judge whether the detector chose the ideal entity label.

| Runtime | Detector | Threshold | Cached load | Median inference | Peak observed RSS | Fixture recall | Known false hits |
|---|---|---:|---:|---:|---:|---:|---:|
| WSL2 | Presidio + spaCy + AU rules | 0.35 | 4.78 s | 8.00 ms | 910 MB | 14/19 (73.68%) | 1 |
| Windows | Presidio + spaCy + AU rules | 0.35 | 7.64 s | 12.53 ms | 451 MB | 14/19 (73.68%) | 1 |
| WSL2 | GLiNER Multi PII | 0.35 | 11.98 s | 258.63 ms | 2.50 GB | 17/19 (89.47%) | 1 |
| Windows | GLiNER Multi PII | 0.35 | 16.56 s | 136.39 ms | 2.04 GB | 17/19 (89.47%) | 1 |
| WSL2 | GLiNER Multi PII | 0.80 | 11.98 s | 244.55 ms | 2.14 GB | 15/19 (78.95%) | 0 |
| Windows | GLiNER Multi PII | 0.80 | 16.56 s | 136.27 ms | 2.02 GB | 15/19 (78.95%) | 0 |

The first GLiNER run also downloaded about 1.1 GiB of weights, so its initial
end-to-end load was 90.39 seconds on WSL and 83.35 seconds on Windows. Cached
loads are the relevant steady-state figures in the table.

Observed behaviour:

- Both candidates run successfully on WSL and native Windows without a GPU.
- Presidio correctly detected all tested Australian structured identifiers:
  TFN, Medicare, ABN, and ACN.
- Default Presidio missed the synthetic username and API key, and missed the
  Chinese name, Chinese address, and passport. It also treated port `18801` as
  a date/time span. HASHI-specific secret patterns and allow lists are required.
- GLiNER at 0.35 detected the English natural-language and secret fixture but
  missed the Chinese name and address. It treated `commit da48df5` as an API
  key.
- Raising GLiNER to 0.80 removed that false hit but missed the low-confidence
  TFN and ACN spans. This makes a single global threshold unsuitable for
  Australian structured identifiers.
- Cross-platform outputs and fixture recall were identical, which is strong
  evidence that both runtimes execute the same model behaviour.
- Cached GLiNER completed with `HF_HUB_OFFLINE=1` and
  `TRANSFORMERS_OFFLINE=1` on both WSL and Windows, confirming that inference
  does not require an online model service after installation.
- The pinned GLiNER stack emitted an upstream mDeBERTa tokenizer-regex warning
  during the forced-offline run. It did not change the cross-platform fixture
  result, but it must be resolved or pinned away before GLiNER could become a
  production dependency.

### 8.3 Level 2 selection

Use **Presidio as the first Level 2 filter**.

Reasons:

- much lower steady-state latency and memory;
- deterministic, checksum-aware Australian recognizers;
- easier allow lists and HASHI-specific secret recognizers;
- identical WSL/Windows results;
- a clearer “basic redaction” boundary than a broad semantic model.

Before production activation, add and test HASHI recognizers for credentials,
API keys, tokens, usernames, passports, file paths, and common code/log
patterns. Add allow lists for ports, release numbers, commit hashes, and other
developer text.

GLiNER is technically runnable on both platforms, but it is **not selected for
the first Level 2 release**. Its extra English recall is useful, but the
approximately 2 GiB memory cost, higher latency, threshold trade-off, and lack
of Chinese name/address coverage do not justify making it mandatory here. It
remains an optional candidate only after a larger benchmark.

`openai/privacy-filter` and the OpenMed multilingual model were not added to
the Level 2 dependency set in this pass. They are larger experimental
alternatives, while the requested first implementation now has a
cross-platform candidate that meets the hardware requirement. They should be
benchmarked separately before any architecture depends on them.

## 9. Release Gate

Level 2 remains unavailable in production until all of these are true:

- the selected local filter passes WSL and Windows;
- every supported API adapter uses the shared outbound gate;
- tool-loop and attachment tests pass;
- failure is demonstrably closed;
- CLI harnesses cannot be selected under Level 2;
- downgrade confirmation works;
- no audit or error log contains raw fixture values;
- the UI describes limitations without claiming complete anonymization.
