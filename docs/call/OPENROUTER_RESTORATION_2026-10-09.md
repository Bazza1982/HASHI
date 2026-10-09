# Restore the approved Call provider and playback design

Owner: Frontend Connector. Layers: Functions and instance configuration.
Core migration: none.

## Approval and design

On October 9 the user explicitly rejected the direct-OpenAI substitution and
instructed Mimi to restore only the initially approved OpenRouter models.
Earlier permission to repair features was not approval to change providers.
HASHI3 repair/adoption and HASHI4 source/configuration synchronization are in
scope. HASHI4 reboot remains the user's operation. Credential changes are
recorded separately from model approval.

The unchanged design is microphone -> OpenRouter Whisper -> the same Agent's
text answer -> OpenRouter Gemini synthesis -> automatic audio playback in the
call window, then listening resumes. Camera observations use the approved
OpenRouter vision model. Whisper transcribes; it does not speak the reply.

The three approved models are `openai/whisper-large-v3`,
`google/gemini-3.8-flash-lite-tts`, and `google/gemini-3.8-flash`. Every media
request uses `https://openrouter.ai/api/v1`. The `openai_compatible` adapter
name denotes the protocol, not permission to use the direct OpenAI service.
Gemini PCM is wrapped as 24 kHz mono WAV for the existing browser playback
contract; the default Achernar voice and approved voice choices are retained.
No request failure silently substitutes a different model or provider.

## Implementation and verification

The defaults owner now exports this approved catalogue. Both sample declarations
are generated from that owner. Only absent declarations are initialized;
explicit off choices, extension fields and saved preferences remain protected.
The two existing unauthorized instance profiles were replaced by a revision-
checked owner write using each instance's own declared OpenRouter credential.

The main delivery pipeline derives Call presentation from PAO's sealed current-
message snapshot. It suppresses ordinary platform/native voice attachments for
those inputs while preserving text delivery and the Call service's synthesis.
Unsealed caller hints cannot change this presentation rule. This corrects the
extra empty chat audio attachment observed during the failed user Call test.

Focused pre-fix evidence: 2 failed, 2 passed. The failures reproduce the wrong
default provider and duplicate ordinary voice synthesis. Post-fix Call component
and delivery checks: 258 passed. Live provider, browser playback, source/Core
invariants and running adoption are separate receipts under the ignored
`state/call-openrouter-repair-20261009` directory. These checks never stand in
for the user's hearing or physical microphone acceptance.

Initial provider probes: HASHI3 approved Whisper and Chinese Gemini speech
succeeded; the speech contains 4 seconds of nonempty audio. HASHI4's existing
OpenRouter key was rejected with HTTP 401, `User not found.` No different
provider, model or credential was silently substituted. Further live results
and any user-approved credential update are added after verification.

Final verified source and live scope:

- HASHI3 curated Core gate: 812 passed, 1 skipped. The first pre-commit gate
  correctly rejected the two unpublished Function files; after committing the
  repair, the complete gate passed without changing that protection or tests.
- HASHI3 was already stopped. Its authorized managed startup adopted the new
  immutable generation; all 11 configured Agents and local services are ready.
  No running Core was cold-restarted to adopt these Function changes.
- All three original OpenRouter models succeeded using HASHI3's existing key.
  Chinese synthesis returned nonempty 24 kHz WAV; vision recognized the probe.
- The actual deployed frontend captured a native Chromium microphone fixture,
  transcribed the request for seven, obtained the main Agent's answer `7`,
  synthesized it with the approved Gemini target and automatically played it.
  The decoded speech has nonzero audio energy through a running AudioContext.
  Playback completed, listening resumed and hangup ended local tracks.
- Correlated backend events confirm HTTP 200 from the approved Whisper and
  Gemini TTS requests. The committed main reply is text; no ordinary voice file
  was generated for this Run. Test inputs are explicit fixtures, not physical
  microphone or human-hearing acceptance.
- HASHI4's promoted source passed the same 258 focused checks using HASHI3's
  isolated test interpreter; no dependency was installed in production Core.
  The source and both instance profiles now contain the approved targets.
- Both instances' protected Core files match the before-repair snapshots.
  HASHI4's live Core/shared processes remain unchanged. Its previous known
  unauthorized Call was already absent (`call_not_found`); a new Call reads the
  restored configuration. Adopting the attachment suppression remains the
  user's HASHI4 reboot.
- HASHI4's OpenRouter credential remains blocked by HTTP 401. The user was
  offered reuse of HASHI3's working credential or self-replacement. No secret
  value was changed or copied while that decision remains unanswered.
