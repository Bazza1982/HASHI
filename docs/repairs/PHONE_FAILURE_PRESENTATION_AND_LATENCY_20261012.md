# Phone failure presentation and latency audit — 2026-10-12

Status: source repaired and offline checks passed on HASHI4 `debug-call`;
running Function adoption and physical confirmation remain user-owned

Owner: PAO for durable Phone failure state and provider classification;
Frontend Connector for Workbench presentation

Layer: replaceable Live Phone Function and external Workbench

Protected Core: unchanged

## Trigger and finding

A real HASHI4 Phone call was closed by the provider's content filter. PAO wrote
the correct `live_provider_content_filtered` call-state event, but the durable
call row and snapshot did not retain that reason. Workbench also hid every
terminal panel after 3.5 seconds. The observable result was a brief disconnect
followed by a transcript saying only that no speech was transcribed.

Provider session-creation failures had a second presentation gap: safe request
IDs and error codes were logged, but most explicit HTTP rejections were reduced
to one generic `live_provider_create_failed` result.

## Repair

- A provider content-policy closure persists `system` /
  `provider_content_filtered` on the logical call. Terminal snapshots derive
  the public `live_provider_content_filtered` code from that state.
- Workbench restores failure kind from snapshots. Failed or interrupted panels
  remain visible until the caller presses Close; normal completed hang-ups keep
  the short automatic dismissal.
- OpenAI HTTP rejections are reduced to safe public categories for
  authentication/access, quota, rate limiting, content policy, rejected
  requests, and temporary provider unavailability. Raw provider messages stay
  private.
- A terminal transport receipt writes its failure category into the displayed
  projection, closing the fast-disconnect race that could leave generic text.
- Workbench separately explains browser microphone denial and uncertain network
  outcomes. Generic fallback remains only when no reliable cause exists.

## Shirley latency audit

The successful Shirley call took about 8.31 seconds from the initial dial
attempt to observed playback. About 6.65 seconds elapsed before browser media
was ready, covering microphone/WebRTC preparation plus provider session
creation. Provider acceptance then took about 1.02 seconds and first output
followed in about 0.12 seconds.

The only fixed application wait on the successful path was the 0.25-second
opening grace after media readiness. It lets caller-first speech win instead of
being talked over, and removing it would save little while weakening barge-in
behavior. Event polling does not gate audio playback. No hidden retry,
foreground-admission stall, transcript queue, or backend text-model wait was
present in this call. Provider input fitting and provider session creation are
serial because the final context must fit before the one non-idempotent session
creation request; changing that boundary would add correctness risk without
evidence that it is the dominant delay.

## Validation

The pre-fix checks failed because snapshots omitted the content-policy reason,
provider HTTP rejections remained generic, and failed presentations shared the
normal 3.5-second auto-hide policy. After repair, focused backend tests cover
durable content-policy state and six provider rejection classes. Workbench
policy, controller, window, locale, and dismissal checks pass. Final protected-
Core, component, build, and source-status checks are recorded with the commit.

This note does not claim running Function adoption or a physical provider test.
The user retains `/reboot` and physical Phone confirmation.
