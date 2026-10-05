# HASHI2 /call experiment status — 2026-10-04

Owner: Frontend Connector Functions and Workbench UI. PAO SessionStore remains
the canonical conversation record. This experiment is isolated to the HASHI2
feature worktrees. It is not merged to `main` or adopted by running workers.

## What is ready offline

- The qualified `/call` source is integrated with the current HASHI2 and
  Workbench development baselines in isolated worktrees. Existing `/phone`
  Realtime transport and its settings remain independent.
- A disabled OpenRouter profile selects `openai/whisper-large-v3` for STT and
  `google/gemini-3.8-flash-lite-tts` for speech. It lists the Gemini voices and
  their descriptors, plus a separate bounded speaking-style option. The style
  is sent as Google AI Studio speech metadata, never spoken as text. Gemini
  requires PCM output; the Function packages its validated raw PCM as WAV for
  the existing browser player.
- The backend resolves its OpenRouter key through the existing instance secret
  resolver. Workbench receives only the safe catalogue. The front-end flag
  hides the `/call` entry and card as well as blocking new proxy operations.
- OpenRouter may route Whisper Large V3 to several providers and its speech API
  currently ignores provider-routing preferences. A response is labelled Groq
  only when a generation receipt verifies that actual provider; otherwise the
  provider remains unverified. The model name alone cannot prove Groq service.
- Transcriptions and replies use PAO's existing SessionStore. The earlier
  handover's `conversation_log.jsonl` filename does not describe the current
  HASHI2 authoritative store; no second memory or call database is added.

## Evidence and remaining gates

- Focused HASHI2 Python tests: 92 passed. Workbench focused Node tests: 52
  passed; its production build passed. Protected Core checker passed. These are
  offline results, not physical or paid-provider acceptance.
- At first the ignored HASHI2 key returned HTTP 401. The user authorized a
  narrow copy from HASHI1; its `openrouter_key` passed authentication and is now
  the HASHI2 `openrouter-api_key`. Other HASHI2 secret fields and the original
  root ownership, mode, and immutable protection were verified unchanged.
- The first authenticated Gemini request failed because `mp3` is unsupported
  for this model. A corrected raw PCM request returned HTTP 200 with a non-silent
  24 kHz mono stream. The corrected Function adapter then returned a valid
  1.64-second WAV for voice `Sulafat` and a separate speaking style.
- That WAV was resampled to the normal 16 kHz call input. OpenRouter Whisper
  transcribed the expected Chinese phrase as `你好,测试`. This verifies the real
  TTS-to-STT provider path, not browser playback or PAO turn projection.
- OpenRouter returned generation IDs for both speech calls but its generation
  lookup returned 404, including after the calls had finished. Actual serving
  providers therefore remain unverified. The user accepts a non-Groq provider;
  no Groq-specific claim is needed for this experiment.
- Next: validate the corrected source and Core gate, then adopt the paired
  Functions and Workbench generation in an approved HASHI2 window before real
  microphone, loudspeaker, camera, PAO projection, and `/phone` acceptance.
- Adoption of the shared Backend API and Agent Functions requires an expressly
  approved HASHI2 operational scope and a fresh idle/queue/schedule check.
  The specific hot `/reboot max` trial was approved later on 2026-10-04; cold
  HASHI2 restart remains outside scope. Code and test success cannot be
  described as live adoption.
- Once adopted, accept with the paired Workbench branch on real microphone,
  loudspeaker and camera, with a short `/phone` regression and a measured
  rollback. Only after HASHI2 live acceptance should any other instance be
  considered.

## 2026-10-04 live adoption preflight

- The user explicitly approved the previously proposed HASHI2-only hot
  `/reboot max` trial and its necessary rollback. This does not approve a Core
  change, a cold restart, another instance, or a merge to `main`.
- Before changing the active HASHI2 source, the live Backend API showed five
  active Agents, none generating or holding queued requests, and no managed
  background jobs. All five authoritative Agent Scheduler lists had no enabled
  jobs.
- Three nightly QA Agents still reported active cross-instance transfers to
  HASHI3. The target recorded one transfer as `received` only, one as
  `accepted_but_chat_offline`, and one as `accepted`. The `received` transfer
  has no queued target request, so its outcome remains unresolved. Treat these
  transfer states as live-work admission blockers until reconciled.
- HASHI2 therefore remains on its original root branch and running Function
  generation. No `/reboot max`, Workbench cutover, physical-device acceptance,
  or `/phone` regression has occurred. Recheck the transfer outcome and the
  complete idle/queue/background/schedule gate immediately before adoption.
