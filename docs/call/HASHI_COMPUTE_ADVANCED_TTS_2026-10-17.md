# Hashi Compute advanced TTS adoption (2026-10-17)

Status: implemented and directly live-tested on HASHI3
`feature-hashi-compute`. Running Function source adoption and physical
speaker acceptance remain separate facts.

## Authority and ownership

The user explicitly requested that HASHI3 use Hashi Compute advanced TTS, run a
live test, and perform the work on `feature-hashi-compute`.

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

HASHI3's revisioned, ignored `compute_services.json` now declares the LAN
node. Its revisioned `call_profiles.json` retains all previous targets for
rollback and adds the service-generated OpenAI-compatible Call TTS target:

- model: `minicpm-o-4.5-zh-female`;
- voice: `zf_001` (年轻4);
- format: 24 kHz mono WAV;
- speed: `1`; and
- endpoint contract: ordinary `/v1/audio/speech`.

The default Call profile and the one existing profile explicitly routed to Call
select this target. Existing STT and vision choices are unchanged. Call reads
the revisioned instance configuration for the next Call; no Core restart or hot
reboot was used.

The current Call adapter intentionally uses ordinary speech synthesis. Although
the service advertises `/v1/audio/speech/expressive`, Call does not send
`instructions`; expressive performance needs a separate consumer contract and
must not be implied by selecting an advanced voice model.

## Live findings

The first candidate, `zf_xiaoxiao`, returned two definitive
`request_deadline_exceeded` responses at the service's advertised 20-second
deadline, including for a short utterance. It was not activated.

`zf_001` completed a direct synthesis within the deadline. The selected
HASHI3 profile then completed synthesis through HASHI's actual Call adapter and
returned a valid 24 kHz mono WAV. The measured test asset was 46,124 bytes and
0.96 seconds for the short test utterance.

This proves machine discovery, configuration selection, HTTP synthesis, HASHI
format validation, and audio metadata. It does not prove acceptable latency for
long Call segments, browser playback, loudspeaker output, or human hearing
quality. The 20-second service deadline remains the main operational risk;
Call must surface a timeout rather than silently substitute another voice.

Workbench was switched explicitly to HASHI3 and a Call session opened and
closed successfully. No spoken prompt was supplied during that short session,
so it is lifecycle evidence only and is not represented as browser playback or
listening acceptance.

## Focused validation

- Red evidence: both new native-manifest tests failed before implementation
  because the flat v0.2.0 model list produced no offerings.
- Green evidence: all 26 Compute directory tests pass, including privacy
  mismatch and busy-state rejection.
- Live v0.2.0 discovery resolved the exact advanced-voice reference and ordinary
  speech interface from the configured LAN origin.
- The active HASHI3 Call profile passed revisioned configuration validation and
  live adapter synthesis.

Source review, commit, runtime Function adoption, browser playback, physical
listening, GitHub publication, and promotion to another instance must continue
to be reported separately.
