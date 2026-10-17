# Hashi Compute TTS adoption and advanced-voice evaluation (2026-10-17)

Status: implemented, compared, and accepted through an audible HASHI3 Call on
`feature-hashi-compute`. Hashi Compute Kokoro is selected for Call; expanded
live voice exposure is active, and the advanced target remains available but
unselected. GitHub publication and promotion to another instance remain
separate facts.

## Authority and ownership

The user explicitly requested that HASHI3 use Hashi Compute advanced TTS, run a
live test, and perform the work on `feature-hashi-compute`. After successful
end-to-end playback exposed substantially higher round-trip latency, the user
approved ordinary Hashi Compute TTS for video and voice Calls.
After the service's live voice exposure was expanded, the user separately
confirmed successful audible Call operation and accepted the private LAN
profile as the no-metered-external-media path.

- Functional owners: PAO Compute Service Directory and Frontend Connector Call
  TTS adapter.
- Engineering layers: replaceable Functions plus ignored HASHI3 instance
  configuration.
- Protected Core: unchanged.
- HASHI4: unchanged.

## Adopted configuration

HASHI now understands the Hashi Compute v0.2.0
`hashi-compute.manifest` document at the configured `/v1/manifest` path.
The reader validates the LAN privacy boundary, gateway and model capacity,
relative interfaces, modalities, formats, features, and served-context
unknowns before publishing an immutable catalogue.

HASHI3's revisioned, ignored `compute_services.json` declares the LAN node. Its
revisioned `call_profiles.json` retains all previous targets and the evaluated
advanced target. The selected service-generated OpenAI-compatible Call TTS
target is:

- model: `kokoro-82m`;
- default/fallback voice: `zf_001` (年轻4);
- accepted owner/Agent profile voice: `zf_072` (童声2);
- format: 24 kHz mono WAV;
- speed: `1`; and
- endpoint contract: ordinary `/v1/audio/speech`.

The default Call profile retains `zf_001`; the existing owner/Agent profile
selects `zf_072`. Both route to the same ordinary Kokoro target. Existing STT
and vision choices are unchanged. The advanced
`minicpm-o-4.5-zh-female` / `zf_001` target remains available for an explicit
future choice or comparison. Call reads the revisioned instance configuration
for the next Call; no Core restart or hot reboot was used.

The current Call adapter intentionally uses ordinary speech synthesis. Although
the service advertises `/v1/audio/speech/expressive`, Call does not send
`instructions`; expressive performance needs a separate consumer contract and
must not be implied by selecting an advanced voice model.

## Live findings and decision

The first candidate, `zf_xiaoxiao`, returned two definitive
`request_deadline_exceeded` responses at the service's advertised 20-second
deadline, including for a short utterance. It was not activated.

`zf_001` completed synthesis and the user confirmed successful end-to-end Call
playback, but also identified the advanced voice as noticeably slower than
Kokoro. Four observed Call turns put advanced first-segment synthesis between
11.5 and 18.5 seconds, with a 14.19-second median. Median time from accepted
turn to the first audio asset was 19.23 seconds, compared with 7.76 seconds in
the available Kokoro Call observations; agent transcription and generation
time was effectively unchanged.

A controlled identical short phrase isolated the provider difference. Advanced
TTS had a 5.47-second median while Hashi Compute Kokoro had a 1.37-second
median. Time-to-first-byte showed the same result, so the delay was synthesis,
not LAN transfer. The user therefore selected ordinary Hashi Compute TTS for
video and voice Calls rather than removing the advanced option.

After the switch, HASHI's actual Call adapter read the persisted default and
completed three sequential syntheses in 1.41-1.53 seconds, with a 1.49-second
median. Each response was a valid 24 kHz mono WAV. This proves discovery,
revisioned selection, provider synthesis, and HASHI format validation. The
user later selected `zf_072` from the Compute-owned catalogue.

Before expanded service exposure, the 15:51 HASHI3 opening synthesis for
`zf_072` received HTTP 400 `voice_not_found`, so no audio asset existed for
the client to play. After the service update, the same HASHI adapter and voice
returned a valid 24 kHz mono WAV containing 3.182 seconds of audio. The user
then separately confirmed successful audible end-to-end Call behavior. This
distinguishes service synthesis, HASHI delivery, and human listening evidence.

For the accepted HASHI3 profile, Call media processing is routed to the
configured private LAN Hashi Compute node rather than a metered external media
API. This is the documented meaning of the no-per-call-provider-cost result; it
does not claim that hardware, electricity, administration, or a separately
selected Engine/Model Provider has no cost.

## Voice catalogue menu

The Call settings menu now resolves the selected TTS target back to the exact
configured Compute node, model, and speech interface. It reads the manifest's
declared `speech_catalogue` resource rather than maintaining a second voice
list. The current Kokoro catalogue contains 59 voices and marks 57 callable for
live synthesis. All 59 are grouped by language and structured gender, and all
59 can be selected for Call. HASHI treats inclusion in the matched Hashi Compute
catalogue as the voice allowlist; the catalogue's `callable` value remains
provider metadata rather than a second HASHI permission gate. Missing gender is
shown as unlabelled rather than inferred from an ID or display name.

Buttons show the catalogue display name and description. Selecting any listed
voice persists only the verified voice ID and a short fallback label in the
revisioned Call profile. A matching locale preview is downloaded directly from
the sample path declared by the Compute catalogue, sent through the existing
frontend media publication path, and discarded after delivery. HASHI neither
generates a second preview nor bundles a copy of the Compute sample set.

Live HASHI3 discovery rendered all 59 entries across 10 groups and exposed all
59 as selectable. It downloaded the selected voice's Chinese WAV sample
directly. That was initially source-level and direct-service evidence. The
later external-frontend selection and audible Call establish running HASHI3
Function adoption and user acceptance; they do not establish GitHub or
production promotion.

The catalogue initially reported 53 voices as `voice_not_exposed`; a direct
probe confirmed that this was a real speech-endpoint limitation rather than a
HASHI playback fault. After service activation, 57 of 59 entries report
`callable`, including the accepted `zf_072`, and live synthesis succeeds.
The remaining `zm_009` and `af_heart` entries are explicitly
`voice_not_exposed` with `offline_preview` scope. Direct requests for both
return HTTP 400 `voice_not_found`. Their previews remain valid, but they are
not evidence of conversational synthesis.

## Focused validation

- Red evidence: both new native-manifest tests failed before implementation
  because the flat v0.2.0 model list produced no offerings.
- Green evidence: all 26 Compute directory tests pass, including privacy
  mismatch and busy-state rejection.
- Live v0.2.0 discovery resolved both the advanced and ordinary speech choices
  from the configured LAN origin.
- The active HASHI3 Call profile passed revisioned configuration validation and
  three live adapter syntheses with the selected ordinary target.
- The catalogue picker passed direct live discovery and preview download against
  the configured Hashi Compute v0.2.0 node without copying catalogue data.

- Post-activation live discovery reported 57 callable voices out of 59.
- The actual HASHI adapter produced a valid `zf_072` WAV, and the user
  separately confirmed audible end-to-end Call success.

Source review, documentation commit, GitHub publication, and promotion to
another instance must continue to be reported separately.
