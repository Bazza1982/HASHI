# Call language and latency audit — 2026-10-10

## Scope and decision

Frontend Connector owns the replaceable Functions Call service and the paired
external Workbench interface. The user reports indistinguishable mobile call
entries, Chinese being recognized as English, and unnecessary response latency.
The user wants automatic language choice, not a forced Chinese setting.

Implement the bounded observation-wait correction in HASHI3. Keep the approved
capture path, provider configuration, freshness rules and camera opt-in.
No Core migration, HASHI4 code change, model switch or runtime reboot is included.
The external UI correction is documented in Workbench's
`docs/call/CALL_MOBILE_LANGUAGE_LATENCY_2026-10-10.md`.

## Findings

The phone and camera-shaped Call buttons occupied the exact same mobile grid
cell. The earlier full-app question fixture advertised neither voice capability,
so it did not exercise the two real entry buttons. The strengthened fixture
exposed one unreachable 44-pixel target. Workbench now groups both independent
entries and reserves space for both. Phone still starts /phone; the camera-shaped
entry starts /call with its camera off.

Current Call recognition uses OpenRouter's `openai/whisper-large-v3` transcription
endpoint without a language override or conversation hints. No English language
override or translation endpoint was found. Audio alone therefore drives
automatic language detection, including short ambiguous utterances.

OpenRouter's STT documentation explicitly says the ordinary `prompt` field is
accepted but ignored. Provider-specific `provider.options` supports some hints,
but this does not establish a portable soft-language hint across the current
DeepInfra, Together and Groq endpoints. Do not claim a prompt-only fix works.
Reference: https://openrouter.ai/docs/guides/overview/multimodal/stt.md

A small actual-provider comparison used four locally synthesized samples, the
same audio bytes per model, the current multipart adapter, and automatic language
choice for both models. No user audio was uploaded or retained for these probes.

| Known synthetic sample | Whisper large v3 | Scribe v2 |
| --- | --- | --- |
| 谢谢 | Xie xie. (1922 ms) | 谢谢。 (1687 ms) |
| 我们再聊聊白头发的事情吧 | Correct Chinese (1328 ms) | Correct Chinese (1141 ms) |
| 嗯 | Hello. (703 ms) | 嗯。 (906 ms) |
| Thanks, let us continue in English. | Correct English (1500 ms) | Correct English (891 ms) |

This supports a bounded real-user A/B evaluation of `elevenlabs/scribe-v2`,
not a guaranteed accuracy claim or a production switch. One request per sample
does not establish a latency distribution. No original mistranscribed recording
was available in diagnostics, so that exact user's utterance is not reproduced.

## Observed latency

Content-free diagnostic events for two completed turns in the reported call:

| Stage | First turn | Second turn |
| --- | ---: | ---: |
| STT | 3250 ms | 750 ms |
| Camera observation join | 2579 ms | Not needed |
| Run admission | 217 ms | 234 ms |
| Agent result wait | 2843 ms | 2813 ms |
| Server turn receipt to final text | 8891 ms | 3813 ms |
| Whole reply audio synthesis | 5078 ms | 7593 ms |

The totals plus TTS are about 14.0 and 11.4 seconds before transport/playback.
These are server stage measurements, not physical end-of-speech-to-audible tests.
The two replies contained 52 and 83 characters. The current TTS target is
`google/gemini-3.8-flash-lite-tts`; the client waits for the final Agent answer,
then requests complete reply audio. The 600-character splitting threshold does
not divide these short replies. Changing STT alone cannot remove this TTS cost.

Additional bounded contributors: 800 ms local speech-end quiet detection,
700 ms backend Run polling, 900 ms browser Call snapshot polling, and 350 ms
speech-readiness polling. Their residual waits depend on phase alignment; they
must not all be summed as a fixed delay. No new fast polling is introduced.

## Fixed unnecessary observation wait

The observer processes a continuously replaced latest frame in one task.
Waiting for that task to finish can hold a turn for the whole 2.5-second deadline
even after its first usable result was published.

A per-call observation-change event now wakes the turn on publication, observer
completion/failure, or camera stop. The turn rechecks the existing freshness and
camera/phase rules and retains the original 2.5-second bound. It does not cancel
the ongoing observer or introduce polling. A slow first inference may still use
the bound; no claim of saving 2.5 seconds on every turn is made.

Focused regression: hold a second camera inference open, publish the first
observation, and require actual Run admission using that observation within
0.5 seconds. The old service timed out; the repaired service passed while the
second inference remained active. The latency, correction and service modules
passed together: 52 tests, including recovery after a failed observation.
Broader physical latency remains user acceptance.

## Adoption boundary and follow-up

The Functions change is source-tested in HASHI3 only. HASHI4 has not adopted it;
no runtime was restarted. Preserve this distinction when preparing later
authorized Function adoption.

The next substantial latency improvement should target time to first reply
audio: evaluate the configured TTS service and incremental answer/audio delivery.
This audit does not introduce speculative speech, concatenate incomplete answers,
force a language, or publish an unvalidated alternate capture pipeline.

Private bounded receipts are under the ignored
`state/call-audit-20261010-1215` directory. They include the red/green test runs,
synthetic audio/provider results and browser geometry/screenshots. Do not commit
raw user transcripts, credentials or generated recordings.
