# Hashi Compute TTS adoption and advanced-voice evaluation (2026-10-17)

Status: implemented, compared, and directly live-tested on HASHI3
`feature-hashi-compute`. Hashi Compute Kokoro is selected for Call; the
advanced target remains available but unselected. Promotion to another
instance remains a separate fact.

## Authority and ownership

The user explicitly requested that HASHI3 use Hashi Compute advanced TTS, run a
live test, and perform the work on `feature-hashi-compute`. After successful
end-to-end playback exposed substantially higher round-trip latency, the user
approved ordinary Hashi Compute TTS for video and voice Calls.

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
- voice: `zf_001` (年轻4);
- format: 24 kHz mono WAV;
- speed: `1`; and
- endpoint contract: ordinary `/v1/audio/speech`.

The default Call profile and the one existing profile explicitly routed to Call
select the ordinary target. Existing STT and vision choices are unchanged. The
advanced `minicpm-o-4.5-zh-female` / `zf_001` target remains available for an
explicit future choice or comparison. Call reads the revisioned instance
configuration for the next Call; no Core restart or hot reboot was used.

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
user's next new Call remains the final audible acceptance of the selected
ordinary target.

## Focused validation

- Red evidence: both new native-manifest tests failed before implementation
  because the flat v0.2.0 model list produced no offerings.
- Green evidence: all 26 Compute directory tests pass, including privacy
  mismatch and busy-state rejection.
- Live v0.2.0 discovery resolved both the advanced and ordinary speech choices
  from the configured LAN origin.
- The active HASHI3 Call profile passed revisioned configuration validation and
  three live adapter syntheses with the selected ordinary target.

Source review, commit, runtime Function adoption, final ordinary-target listening,
GitHub publication, and promotion to another instance must continue to be
reported separately.
