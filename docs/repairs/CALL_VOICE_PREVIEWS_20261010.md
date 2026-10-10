# Call voice previews and first-audio latency — 2026-10-10

## Approval and ownership

The current user approved correcting Call defects and latency, trying
`x-ai/grok-stt-1.0`, and adding localized gender labels and prerecorded previews
like /voice. Recognition stays automatic; no Chinese language override or
context-based rewrite was introduced.

Owner: Frontend Connector. Engineering layers: Functions (Call settings,
speech segmentation and the existing preview publication helper), the external
Workbench client, and private instance Call configuration. Core is unchanged.
The standing prohibition on changing HASHI4 code/restarting it remains in force.

## Resulting behavior

- Voice names retain provider identifiers; styles and gender use the selected
  menu locale. One catalog supplies defaults, rendering and preview generation.
  Gender evidence: https://docs.cloud.google.com/text-to-speech/docs/gemini-tts
  Style evidence: https://ai.google.dev/gemini-api/docs/speech-generation
  Unknown/custom model voices do not inherit Gemini metadata.
- Selecting a voice saves through CallConfig's existing revision fence, then
  publishes its prerecorded sample through /voice's FC notification path.
  A replay control is read-only. Samples are not Agent input or history.
  Publication identity includes the callback identity, so a new replay can
  create a new card while the same callback is deduplicated.
- Samples use the configured Gemini 3.8 Flash-Lite TTS voice, default style,
  and fixed synthetic text in Chinese and English. They do not claim to
  preview a user's custom advanced style. All 30 voices have both languages.
  Workbench gets MP3; Telegram retains OGG. Missing/corrupt or mismatched
  provider/model assets produce an unavailable notice, never a substitute.
- Bundle validation retains /voice's OGG-only default and explicitly opts
  Call into validated MP3 entries. Generation is offline, bounded and
  resumable; credentials and machine paths stay outside the bundle.
- Speech segmentation prioritizes a short natural opening phrase (120-character
  ceiling), then the previous 600-character/12-segment bound. Only final text
  is spoken. Workbench starts one following segment while current audio plays;
  it does not continuously poll that segment during playback. Cancellation,
  order, explicit retries and the existing polling intervals are preserved.
- The earlier observation-ready join correction remains in the previous
  HASHI3 commit, rather than waiting for the continuous camera task.

## Evidence

Red: three first-segment tests failed against whole-answer synthesis; two
controller cases failed because next-segment generation waited for playback.
Chinese voice labels also failed before adding metadata/localization.

Green: 143 focused Python cases (Call suite and /voice menu), 30 client
controller cases, language catalog checks and Vite production build passed.
All 120 files (60 OGG and 60 MP3) passed digest/container validation and ffmpeg
decoding. Call selection was tested through real SessionStore media
publication, including payload, MIME type, caption, non-history status,
read-only replay, callback deduplication and stale-revision rejection.

Built-app browser fixtures covered 320px and 390px phones, 834px iPad portrait
and 1194px iPad landscape. All had 44px voice buttons, no horizontal overflow,
and audio currentTime advancing after activation. Windows Playwright WebKit
reports no OGG/Opus support, so browser previews use MP3. Its known native
controls Temporal.Duration exception still occurs with MP3; playback advances
and it is reported separately from product errors. Media must be served by
real HTTP in this harness because this WebKit media path bypasses page routing.
These fixtures are not physical iOS device acceptance.

Same synthetic 65-character Chinese reply, same Gemini target/voice:
whole reply synthesis 5344/5781 ms; first 24-character phrase 3187/3188 ms.
Network/provider variance remains. These numbers exclude STT, Agent execution,
endpoint detection, upload and physical playback; no total-call claim is made.

Automatic-language Grok probes through the real adapter:

| Synthetic input | Output | STT duration |
| --- | --- | --- |
| 谢谢 | 谢谢。 | 1047 ms |
| 我们再聊聊白头发的事情吧 | 我们再聊聊白头发的事情吧。 | 703 ms |
| 嗯 | 文 | 656 ms |
| Thanks, let us continue in English. | same sentence | 2078 ms |

The very short Chinese input still fails. Synthetic SAPI voices do not predict
real-user accuracy; user testing with the selected model remains necessary.

Private scripts, receipts and screenshots are retained under the ignored
`state/call-voice-20261010-1258` audit directory.

## Source, configuration and adoption

The instance Grok target was added using the existing validated adapter and
revision-carrying config_json writer. No shared model catalogue was changed.
Existing Whisper remains selectable. HASHI3 and HASHI4 had no per-Agent
overrides at the change boundary; only their default STT choice changed.
The running HASHI4 context endpoint returned the same configuration revision
as the verified Grok declaration, with call_ready=true and busy=false.
Existing calls retain frozen configuration; new calls use the new choice.

HASHI3 contains the backend implementation and packaged samples. On the user's
later explicit instruction, the same verified source and all 121 package files
were synchronized to HASHI4 as commit `e6a06ae9`; its 131 focused Call checks
pass against that tree. The running HASHI4 shared Functions have not been
replaced. The shared Call service requires the supported shared Functions
handoff (`/reboot max`) to adopt this source generation. A targeted Agent reboot
does not replace that owner; no lifecycle action was used as a test shortcut.

External Workbench static client publication is separately checked and recorded
in its Call playback decision. It can enable segment prefetch with the existing
protocol before the shorter first-segment backend change is adopted.

## Verified close-out

Backend source is committed as `767aa553`, following the earlier camera join
correction `21bd2c7b`. The deterministic Core gate initially had 809 passes,
one skip and three commit-qualification failures while this source was
uncommitted. After the commit, those exact three probes all passed, including
isolated full-product preparation without live services. The generated Functions
manifest includes all 121 Call preview files (120 audio files plus manifest).
Protected Core checks pass; no protected source was modified.

Workbench commit `b9bc3f3` is published at the existing service.
Both local HTTP and authenticated public-browser reads match the built entry
and asset hashes; the connected desktop page was reloaded. Public verification
used existing browser authentication after unauthenticated HTTP returned 403;
the publication was not repeated.

The earlier prepared binary backend patch was superseded by the user's approved
source synchronization. Publication receipts remain in the ignored audit
directory. Remaining acceptance is separately authorized production Functions
adoption, followed by the user's real phone speech, pauses, Bluetooth and
perceived-latency test.
