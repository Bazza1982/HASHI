# Call entry, action, and voice-cost HASHI4 source load — 2026-10-11

## Scope and authority

The user directed the verified HASHI3 `debug-call` candidate to be loaded into
HASHI4 and reserved the HASHI4 reboot for the user. The Agent did not reboot or
restart HASHI4. Functional ownership remains with the Frontend Connector for
entry/rendering and the PAO Call/Phone Functions for readiness and voice-media
billing. Protected Core was not changed.

## Loaded candidate

- Workbench `debug-call`: `a396e12123a57a9b1576c45f728292f425518f57`
- HASHI3 `debug-call`: `c8cd6ecb`
- HASHI4 `debug-call`: `912d9daf`

The Call/Phone Function source and focused tests loaded into HASHI4 matched the
independently verified HASHI3 candidate byte for byte. No branch or worktree was
created. The retired Workbench per-connection Call allowlist is no longer an
availability gate. Phone and Call derive readiness independently and cannot
launch or fall back to each other.

## Offline validation

- 53 focused Workbench launcher, browser-layout, controller, and billing tests
  passed.
- The Workbench production build passed.
- 249 Call and Phone Function tests passed against the HASHI4 source.
- The Protected Core check passed.
- Phone projects its qualified GPT Live session rate. Call aggregates STT and
  TTS voice-media cost, preferring provider-reported cost and otherwise marking
  catalogue calculations as estimates. Backend text remains in `/metre`; Call
  vision is excluded.

## Workbench adoption and live observation

The managed Workbench service reported the exact Workbench commit above and a
new runtime run. The existing Electron window was reloaded and created a fresh
renderer session against that run.

A direct HASHI4 video Call was then opened through the video entry. It acquired a
live Microsoft LifeCam HD-3000 track at 1024x576, applied the horizontal mirror
transform, retained the Agent avatar and identity, rendered one rolling
transcript, and kept the three bottom controls visible.

During real Agent playback, the Stop speaking control had a non-zero rectangle
fully inside the viewport, accepted the pointer hit-test, and was clicked. The
visible stage changed from speaking to listening, the control retired, and
telemetry recorded `call.playback.stopped` with reason `user_stopped`. Hangup
was confirmed and both camera and microphone were released. No Call surface or
active camera track remained.

## Remaining adoption boundary

The Workbench client is running the loaded candidate. The HASHI4 Function source
is loaded but its running generation remains the pre-load generation until the
user performs the reserved reboot. Live billing projection therefore remains
pending that reboot and subsequent user acceptance. Source load, client
adoption, Function adoption, and live billing acceptance are separate facts.
