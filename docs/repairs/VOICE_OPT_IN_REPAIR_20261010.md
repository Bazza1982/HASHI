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
- Final voice component/platform and ingress run: 35 passed in HASHI3 and
  35 passed against synchronized HASHI4 source. Real Windows tests
  check chosen voice language, explicit voice preservation, missing-voice
  failure and nonempty file output; no network or speaker playback is involved.
- Actual opt-in VoiceManager -> Windows SAPI -> OGG conversion: Chinese WAV is
  3.943 seconds, OGG is nonempty. The same manager generates no audio by default.
- Call backend and main delivery pipeline: 258 passed, including the previously
  retained 13 optimization/privacy scenarios and duplicate-voice suppression.
- Restored frontend controller/media/UI scope: initially 39 passed. A fresh
  HASHI4 Call then exposed a separate cold detector startup/retry failure:
  readiness arrived at 18.783 seconds, after the 15-second timeout, and explicit
  retry reused the rejected promise. The frontend now allows a bounded 45-second
  startup, discards failed Workers/promises, and fences their late messages.
  Both added failure scenarios fail before repair; the final detector/controller/
  media/UI scope is 43 passed, no skips. Prior hangup/overload tests are retained.
- Repeated actual acoustic Worker comparison on the same 40.2-second sustained
  background fixture: restored speech submits at 3.328 seconds; the rejected
  amplitude implementation never submits. Background alone produces no speech.
- Independent browser acoustic/real file-microphone run: all 13 sound cases and
  four microphone cases pass (noise rejection, short word, quiet word, word with
  background). No microphone hardware or human-hearing claim is made.

## Verified delivery and adoption boundary

The coherent voice fix was committed in HASHI3 (`0906990e`) and cherry-picked
into HASHI4 (`5f8c18ad`). The instance-specific FYI history was retained while
resolving its documentation conflict. Product/test files and the decision match
exactly; unrelated HASHI3 checkout changes were not staged.

The paired frontend's detector correction is committed as `8b037ea`. A clean
browser profile through an isolated instance-bound copy of the real frontend
completed the actual Call button path on both real instances:

- HASHI3: microphone -> recognized "What is 2 plus 5?" -> main Agent answer "7"
  -> nonempty automatic audio playback -> listening resumes -> confirmed hangup.
- HASHI4: the same question with sustained background sound -> recognized full
  question -> answer "2加5等于7。" -> nonempty automatic playback -> listening
  resumes -> confirmed hangup. The actual local detector assets are observed.
- Observed segment-to-playback times are 13.231 seconds and 15.264 seconds for
  these runs, respectively. These include recognition, Agent and synthesis work;
  the separate 3.328-second comparison measures capture endpoint only.
- Both tests use an explicitly declared synthetic file microphone, with a quiet
  lead-in and repeating fixture to allow cold readiness. They do not inject a
  Call turn directly. No camera is requested, and all local tracks end.

The verified static frontend build and detector source are published to the
existing shared daily frontend. Its actual served entry matches the candidate;
all 46 earlier hashed assets remain available to open tabs. Both Core identities
and protected Core source hashes are unchanged. New tabs/reloads use this Call
repair. Neither HASHI instance has been rebooted by this task.

Automatic `/voice` defaults and Windows voice selection live in immutable Agent
Function generations. Existing Workers still contain the previous default and
must hot-adopt the new source before this portion takes effect. No preference
files were bulk-rewritten, and the user's explicit saved choices remain intact.
An explicit hot-update authorization has been requested under the user's
standing no-reboot-without-authorization rule. This is an adoption boundary,
not a claim that the running voice behavior is already corrected.

Historical receipts and synthetic-file microphones do not substitute for
subjective hearing or physical microphone acceptance.
