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
  is sent as Google AI Studio speech metadata, never spoken as text.
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

- Focused HASHI2 Python tests: 90 passed. Workbench focused Node tests: 52
  passed; its production build passed. Protected Core checker passed. These are
  offline results, not physical or paid-provider acceptance.
- The existing ignored HASHI2 `openrouter-api_key` returned HTTP 401 from
  OpenRouter's key-auth endpoint; a bounded TTS probe was rejected as
  `User not found`. No real speech audio, STT result, microphone, loudspeaker,
  or camera acceptance has been claimed. No secret value was copied to source.
- Next: owner updates the HASHI2 local secret with a working OpenRouter key.
  Recheck key auth, then bounded TTS and STT probes and actual serving-provider
  receipt. Decide whether OpenRouter Whisper with verified receipt is acceptable
  or whether a guaranteed Groq backend requires a separate direct Groq key.
- Adoption of the shared Backend API and Agent Functions requires an expressly
  approved HASHI2 operational scope and a fresh idle/queue/schedule check.
  The standing rules forbid `/reboot max` and cold HASHI2 restart. Until then,
  code and test success cannot be described as live adoption.
- Once adopted, accept with the paired Workbench branch on real microphone,
  loudspeaker and camera, with a short `/phone` regression and a measured
  rollback. Only after HASHI2 live acceptance should any other instance be
  considered.
