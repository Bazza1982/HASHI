# Turn-based call media connector — implementation and local qualification

Status: first implementation for the `feature-call` branch. Not a production
release, and not evidence of live device/provider qualification. See the PRD
alongside this file. This document resolves implementation choices left open in
that design without changing `/phone`.

## Ownership and delivered behavior

The new `orchestrator/frontend_call` package is a Frontend Connector in replaceable
Functions. It captures no devices itself and owns no Engine, Memory, Message,
Run, database, or listening port. The existing authenticated Backend API registers
`POST /api/v1/call/operation`; existing HASHI Remote/HMAC can relay it. PAO receives
one accepted Message/Run per finalized speech turn via the current Agent's
`enqueue_request`. Final answers come from the canonical SessionStore.

Delivered: utterance STT, optional question-aware JPEG observation, normal Agent
execution, bounded sequential TTS, owner/session/generation fencing, idempotent
turn admission, profile revision checks, expiring media leases, and a reciprocal
foreground-call interlock with `/phone`. Ending media does not cancel an accepted
Agent task. The ordinary Agent approval and tool boundaries remain in force.

Native image-to-Engine delivery, streaming STT, word-aligned captions, continuous
video reasoning, provider voice-directory discovery, and voice-cloning are **not**
in this first implementation. Each is an optional capability, not simulated by
this implementation. The UI's camera is a real **user-side preview**, not generated
video of the Agent; the Agent uses its existing static avatar.

## Instance configuration (disabled by default)

1. Copy `examples/call_profiles.example.json` to `call_profiles.json` **beside the
   active instance's Agent configuration file** (`api.config_path`). This file is
   ignored by Git. Do not put credentials, machine URLs or live profiles in source.
2. Set `enabled` to `true` only on the intended test instance. Change model IDs,
   voice IDs and base URLs to the **actual deployed endpoints**. Example model/voice
   names are placeholders, not installed models.
3. Each target supplies `id`, `kind` (`stt`, `tts`, `vision`), `adapter`, `model`,
   `base_url`, `location`, optional `credential_ref` or `credential_env`, and supported `options`.
   This release ships the `openai_compatible` protocol adapter. It works with
   independently selected conforming hosted APIs or managed local sidecars;
   it is not a dependency on OpenAI's hosting. Unsupported protocols need a real
   adapter, not a renamed base URL.
4. Local targets must be explicit loopback HTTP/HTTPS services on the **HASHI
   host**, not the device displaying the client. Provision native dependencies
   in isolated sidecars; do not install them in a running Core interpreter.
   Cloud targets require HTTPS. Browser-submitted URLs, keys, headers, model IDs
   or owner IDs are never accepted. Credentials use the existing secret resolver
   (`secrets://name` or `env://NAME`), or the older `credential_env` field,
   and are never returned to the client.
5. STT is `POST {base_url}/audio/transcriptions` (multipart WAV, `model`, JSON
   response containing `text`). TTS is `POST {base_url}/audio/speech` (JSON
   `model`, `input`, `voice`, `response_format`), returning MP3 or WAV. Vision is
   `POST {base_url}/chat/completions`, a standard text + `image_url` request.
   Set the base URL to the provider's API prefix, e.g. `/v1`, not an entire endpoint.
6. For TTS set `voices` to the installed provider's actual IDs; optional
   `voice_styles` labels describe those voices without changing their IDs. Model options
   are a bounded schema (`string`, `number`, `boolean`, optional `enum`). Only
   declare options the endpoint supports. Do not copy one provider's emotional
   tags into another model. `instructions` is suitable only where supported.
   For OpenRouter Gemini 3.8 TTS, the allow-listed `style` option is sent as
   `provider.options.google-ai-studio.speech_metadata.style`; it is not
   prepended to the text that the model speaks. The HASHI2 trial profile is
   `examples/call_profiles.openrouter.example.json`.
7. Use the client's settings to select target IDs and supported options. Writes
   update only the current owner/Agent profile, using the existing revisioned
   configuration primitive. A conflict requires reload; no blind overwrites.

A selected cloud target requires explicit per-call consent and a current Agent
privacy projection of level 0 or 1. Level 2 and unknown/old Worker projections
**block cloud media**, because this connector has not qualified the raw-media
PII-redaction boundary. Local media remains available; the main Agent's own
privacy protections are unchanged. Adopt the Agent Function update as well as
the shared API Functions so the privacy-level metadata is current.

## Protocol and safety boundaries

Common fields: `operation`, `client_id`, `agent_id`, `session_id`,
`context_generation`. `context` returns protocol `1`, the Function generation,
configuration revision, safe target catalogue and selected profile. It also
reports an existing foreground media call.

`start` adds `generation`, `revision`, a new random `call_id`, and `allow_cloud`.
Subsequent `turn`, `snapshot`, `speech`, and `end` operations carry the same
scope and generation. `save_profile` accepts only the revision and a selection
of known targets/options; it is refused while this owner's call is active.

`turn` adds a monotonically increasing `sequence`, random `turn_id`, `audio_b64`,
and optionally `image_b64` + `captured_at`. Repeating exactly the latest upload
is idempotent; changing it under the same sequence is a conflict. Older sequence
replays cannot create another Run. Process replacement changes `generation`;
an old call must not be recreated or replayed automatically. Inspect the normal
Session when admission has an unknown outcome.

`snapshot` renews the 45-second lease and returns bounded recent captions and the
current turn's status and canonical IDs. A maximum eight-hour media session and
16 retained active/recent sessions prevent uncontrolled memory growth. Completed
raw inputs are released; this package writes no audio/video recording files.
Canonical text and labelled visual observations follow ordinary Session retention.

Audio is validated mono 16-bit PCM WAV at 16 kHz, 0.1–60 seconds. Optional pictures
are JPEG, at most 512 KiB, with validated dimensions up to 2048 per side. The
standard browser client sends a 1024-pixel long edge. JSON body limit is 4 MiB.
Provider response bytes and timeouts are bounded; redirects and automatic retries
are disabled. Only trusted instance endpoints can be contacted.
OpenRouter Whisper Large V3 can be served by several providers. Its speech
endpoint currently ignores `provider.only/order/ignore`; selecting that model
does not prove Groq handled a request. A provider receipt is recorded when
OpenRouter exposes a generation ID and the lookup confirms the serving provider;
otherwise it remains explicitly unverified.

`speech` requests one stable final-answer segment by turn ID/index. The response
is pending until its task finishes, then carries a private MP3/WAV asset. Failed
speech can be explicitly retried once without rerunning the Agent. The last two
segments are cached; evicted segments are not silently regenerated. Long answers
are capped at 12 × 600-character segments; code blocks, large tables and URLs are
not read aloud. The full canonical answer remains in text. No extra rewriting LLM
is called. Provider usage/currency metering is not implemented; use provider
billing during initial qualification, and never infer a dollar total from duration.

## Files and preservation boundary

New modules: `contract.py`, `config.py`, `adapters.py`, `ports.py`, `service.py`,
`routes.py`. `workbench_api.py` only registers the route. The existing
`frontend_live_voice/manager.py` gets a read-only foreground query and a default-noop
external busy guard; no Realtime transport, provider API or phone settings change.
`flexible_agent_runtime.py` projects existing privacy level through metadata.
Protected Core, SessionStore schema, dependency locks and model catalogue are
unchanged. No normal feature belongs in Core.

## Focused offline validation

```bash
python -m pytest -q tests/frontend_call tests/test_live_voice_integration.py
python scripts/check_protected_core_changes.py
git diff --check
```

New tests use real WAV/JPEG parsing, revisioned file writes, real loopback HTTP
multipart/audio boundaries, real SessionStore admissions/results, and controlled
Agent/provider ports. No paid provider, external camera, actual microphone,
production restart, or irreversible business tool operation is used.

## Local adoption and release gate

Use the paired client branch and this branch in clean worktrees. Read repository
AGENTS/architecture/testing rules first. Identify the instance and back up its
ignored configuration. Do not cold-restart or migrate Core for this feature.
The shared Backend API and selected Agent Functions both need adoption via the
existing supported Function-generation process; obtain explicit approval for any
broad `/reboot max` operation. Do not silently reboot other instances or Agents.

Verify speech in English and Chinese, mixed technical terms, camera permissions,
manual pinned snapshots, TTS failure/retry, end during reasoning, provider/local
switching, expired calls, changed context, and `/phone` regression. Record exact
provider/model/voice, host, latency and user-visible outcomes. Gate main merges
on local qualification; code delivery and live adoption are separate facts.
