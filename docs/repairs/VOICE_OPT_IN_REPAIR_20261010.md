# Voice opt-in correction and restored Call review

Owner: Frontend Connector. Layers: Functions (voice preference, platform TTS
adapter) and the existing external browser capture. No Core migration.

## Authorization and cause

The October 10 user requests a code repair in HASHI3, review of the earlier Call
restoration, and verified synchronization to HASHI4. Existing operational rules
require explicit authorization for `/reboot`; this request does not explicitly
authorize that operation. Source, offline acceptance and live adoption must be
reported separately.

The October 9 media-default change `251c1b91` changed VoiceManager's missing-state
default from disabled to enabled. It confused feature availability with consent
to automatic narration. At diagnosis, 19 of 26 HASHI4 Agents and all 11 HASHI3
Agents lacked a voice preference and inherited automatic Windows TTS. Saved
preferences were not the root cause and must not be erased.

Windows TTS previously selected the system voice unless a name was saved. This
machine's system voice is English; an installed Chinese voice is available but
was never selected automatically. Real SAPI also reproduced a successful exit
with a zero-frame WAV for Chinese read with the English voice. This is an
additional empty-attachment path, separate from Call's duplicate narration.

## Correction

- Absent/empty/unreadable preferences leave automatic voice OFF, without writing
  a fabricated preference. `/voice` and explicit TTS/native selection remain
  available. Existing saved choices survive reloads.
- The Windows default engine remains SAPI. Unspecified voices use the existing
  shared English/Chinese/Japanese language hint. Explicit voices win; missing
  voices/languages return an error rather than silently choosing English.
- A successful SAPI process with an empty WAV is rejected before OGG conversion.
  No new provider, dependency, secret, or instance model is introduced.

## Call restoration review

The source audit identifies the approved October 5 frontend `2b61835` and optimized
backend `515b7d05`. The HASHI2-only cloud experiment replaced local acoustic
detection with an amplitude gate; that capture is unsuitable for the restored
Whisper design under continuous background sound. October 7/9 direct-OpenAI
receipts do not qualify the approved OpenRouter design.

By this review, the paired frontend already contains the October 10 restoration:
the original local detector/segmenter/PCM worklet with the later native processor
failure fence retained. The deployed source and served entry match that candidate.
This review does not roll back controller, camera, Session or desktop changes.

The approved Call declaration remains OpenRouter Whisper Large V3, Gemini 3.8
Flash-Lite TTS (Achernar with the Gemini voice/style catalogue), and Gemini 3.8
Flash vision. Sealed Call inputs suppress ordinary platform/native voice
attachments. TTS preparation/reuse, retry without Agent replay, removal of
secondary provider lookups/Telegram progress waits, Session/privacy fences and
camera controls remain covered by the focused component checks.

## Verification

Evidence is retained under the ignored instance directory
`state/voice-opt-in-repair-20261010`.

- Pre-fix preference/Windows checks: 8 failures. The separate empty-WAV boundary
  fails before its guard. An initial missing test-directory setup error was
  corrected before collecting this red evidence.
- Initial fixed voice component/platform run: 28 passed. Real Windows tests
  check chosen voice language, explicit voice preservation, missing-voice
  failure and nonempty file output; no network or speaker playback is involved.
- Actual opt-in VoiceManager -> Windows SAPI -> OGG conversion: Chinese WAV is
  3.943 seconds, OGG is nonempty. The same manager generates no audio by default.
- Call backend and main delivery pipeline: 258 passed, including the previously
  retained 13 optimization/privacy scenarios and duplicate-voice suppression.
- Restored frontend controller/media/UI scope: 39 passed, no skips.
- Repeated actual acoustic Worker comparison on the same 40.2-second sustained
  background fixture: restored speech submits at 3.328 seconds; the rejected
  amplitude implementation never submits. Background alone produces no speech.
- Independent browser acoustic/real file-microphone run: all 13 sound cases and
  four microphone cases pass (noise rejection, short word, quiet word, word with
  background). No microphone hardware or human-hearing claim is made.

Source publication, fresh real-provider Call checks, final synchronization and
operational adoption are recorded separately after completion. Historical
receipts and synthetic-file microphones do not substitute for subjective
hearing or physical microphone acceptance.
