# HERV3 typed native media — cloud implementation and local qualification

Status: **DRAFT / source changes ready for non-production validation, not production qualified**
PR: https://github.com/Bazza1982/HASHI/pull/204
Branch: `fix/herv3-typed-native-media-20261123-cloud`
Base main SHA: `e87b00db0074d4acdac322ccc2a2ce2415850960`

## Engineering rule

A plain text value is never a media payload because it resembles an image URL,
Base64 or a screenshot label. A typed media part from a trusted tool or attachment
intake is the only source of media identity. The Provider Adapter maps this part
onto the exact endpoint's supported request format, validates real bytes and
preserves Tool Call correlation.

## Implemented paths

1. `orchestrator/multimodal_contract.py`: inspect real byte objects and typed
   media fields; allow technical examples, code and captions as ordinary text.
2. `orchestrator/api_gateway.py`: do not reject/cache-evict text containing a
   media-protocol example; real inline media remains excluded from managed Sessions.
3. `orchestrator/her_v2/privacy_gate.py`: inspect natural language as text;
   keep Level 2 fail-closed for actual typed images/audio/video/files.
4. `orchestrator/provider_text_projection.py`: compute text-token estimates
   from text plus Tool schemas, excluding only typed media bytes. Wire payload
   remains untouched.
5. `adapters/openrouter_api.py` and `adapters/hli_api.py`: no arbitrary
   text-marker exemption from capacity estimation or HLI proactive Compact.
6. `adapters/deepseek_api.py`: preserve native Tool image as a typed
   `role=tool` content part and its original `tool_call_id`; remove plain text
   screenshot regex classification and fabricated user image continuation.
7. `tools/typed_media_result.py` plus screenshot producing tools:
   actual image bytes are attached as typed ToolResult content and output
   contains a small textual receipt, not serialized pixel bytes.
8. `tools/registry.py`, `tools/device_control_worker.py`,
   `tools/windows_helper/backends.py`, `tools/gateway/mcp_stdio.py`:
   route typed Tool content across Core capability and authenticated browser
   proxy boundaries without reinterpreting arbitrary text.
9. Chrome extension `browser_session`: read structured step action metadata
   from its existing JSON envelope; convert *only* true screenshot steps into
   native images. get_text/evaluate results stay text, even if they contain
   a syntactically valid data URL.

## Browser session compatibility boundary

When the structured Chrome Extension or local browser session requests a
screenshot, return structured text parts even if that screenshot fails.
Successful screenshot steps keep their image parts and normal page text remains
text in the same result.

A legacy unstructured browser_session response cannot reliably identify the
source step of its output. When such a response includes a requested screenshot,
the gateway reports an explicit unsupported legacy result and asks for the
browser worker to be updated. It does not inspect arbitrary page text for
image-shaped strings or send potentially large screenshot bytes as plain text.
Legacy single-purpose screenshot tools retain their existing compatibility
path. Text-only browser_session results continue to pass through unchanged.

Device capability test registrations are created when each test runs, so their
normal expiration remains meaningful even after a long collection or test run.

## Provider support and deliberate non-support

- DeepSeek Chat Completions: official `tool`-message `image_url`
  content shape, supported image models only.
- OpenRouter-compatible: native user `image_url` follow-up is retained
  where upstream Tool-part image support is not verified. This is a protocol
  adapter conversion of *typed* content, not fabricated user intent or a
  text-based media classifier.
- HLI / Ollama: native image delivery depends on real model + endpoint support.
  No model is assumed to have vision because of its name alone.
- Audio, video, documents: existing native input, approved STT/OCR/media-reader
  and explicit unsupported-modality routes remain intact. Unsupported typed
  Tool media must not silently become text or be claimed as perceived.
- Cross-provider output format is not universal. Do not forcibly reuse the
  DeepSeek Chat Completions schema with Anthropic/Gemini native APIs.

## Known qualification gaps

- Actual device screenshot captures, Chrome extension sessions, native
  DeepSeek API vision and HLI model context must be tested in nonproduction.
- Current ToolResult.content uses structured MCP image bytes as Base64,
  which are only moved into the Provider image field at the adapter.
  Image asset-reference persistence and lazy re-materialisation through
  interrupted Provider Tool loops are **not yet guaranteed** by this patch.
  Do not claim zero image-byte duplication across recovery checkpoints.
- Long Turn persistence (150+ tool results), repeated screenshots and
  authorized recovery require memory, token, and side-effect evidence.
- Do not weaken the real media signature, MIME, authorized roots,
  capability, total-body-size and privacy gates.

## Required local QA (non-production only)

1. Fetch the exact PR HEAD in a clean isolated checkout; record base and HEAD,
   do not cherry-pick unmerged unrelated P0 changes into this PR.
2. Run all PR cloud jobs and focused tests shown in
   `.github/workflows/herv3-typed-media.yml`; run protected Core guard.
3. For each screenshot route (direct browser, Chrome extension browser_session,
   native desktop, Windows Helper and the browser gateway), ensure captured
   pixels survive as exactly one typed image part. Text-only browser sessions
   must not produce images even with long code, JSON, or data URL examples.
4. Inspect redacted DeepSeek request JSON (part kinds, roles, Tool Call IDs,
   byte counts and hashes only). Never log raw image Base64 or credentials.
5. Test an actual vision-capable DeepSeek model and at least one real
   OpenRouter/HLI endpoint **only after obtaining network/model permission**.
   A text-only model must explicitly report unsupported modality.
6. Build an artificial 150+ Tool Call Turn, inject real screenshots and
   image-URL code examples, replay a provider interruption and compare
   tool receipt counts. No duplicate side effects or recursive media copies.
7. Verify Level 2 privacy gate, capacity estimation, HLI Compact and
   text-only Session cache behavior under fault injection.
8. Separate failures already present on base main from introduced failures.
   List every outstanding risk with its exact file/line and acceptance status.

## Merge and deployment

Remain OPEN / DRAFT for technical review. No direct change to main/HASHI4.
Only after explicit owner approval and exact-HEAD green CI, merge using the
approved method and deploy **non-production** first. Production HASHI4 requires
a separate explicit deployment decision and rollback readiness.
