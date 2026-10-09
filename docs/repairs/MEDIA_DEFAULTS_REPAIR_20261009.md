# Media defaults repair — October 9, 2026

Approval: repair and reboot/adopt HASHI3; synchronize qualified changes and
dependencies/configuration to HASHI4; user alone adopts HASHI4. Frontend
Connector, Functions/deployment; protected Core remains unchanged.

Baseline defects reproduced before repair:
- missing global audio flag hides recording despite synchronized source;
- absent Agent speech preferences resolve to off;
- absent call declaration leaves camera unavailable;
- Workbench default switch disables Call, and the local release admits only HASHI3;
- npm ignores failed media setup and omits the TTS provisioner/lock.

Focused red evidence: default config + VoiceManager: 2 failed; fresh call
configuration and malformed boundary: 2 failed; Workbench default Call: 1 failed.
These assertions exercise effective behavior rather than source-text copies.

Implementation is described in `../HASHI_MEDIA_DEFAULTS_2026-10-09.md`.
Verified implementation and execution:

- Committed media defaults/installers, selected-instance child-environment
  isolation, and HASHI API Phone qualification in HASHI3. The startup defect
  was inherited HASHI4 Python search paths contaminating HASHI3's dependency
  fingerprint; the child now resolves only its selected instance.
- Focused media regression: 279 passed, 2 skipped. Isolated npm/startup failure
  and CLI checks passed after observing nonzero-failure and selected-root red
  cases. Immutable generation/lifecycle checks: 185 passed.
- Final curated Core gate after the Phone fix: 812 passed, 1 skipped. Protected
  Core source remains identical to the before-repair snapshots in both instances.
- The real adapter Phone regression failed with
  `live_semantic_model_not_tool_free` before the declaration and passed after
  it. Adapter/cascade suite: 44 passed, 5 optional platform/provider skips.
  No alternate backend/model or tool-enabling bypass was introduced.
- HASHI4 focused source checks: 95 passed, 1 skipped, using HASHI3's isolated
  test interpreter with HASHI4 source explicitly selected. Pytest was never
  installed in HASHI4's running Core interpreter.
- A separate unchanged action harness has 4 existing failures (missing trusted
  fixture configuration and obsolete primary-Session assumptions). The same
  failures reproduced with the pre-fix adapter declaration removed in the
  test process. These tests were not changed or omitted to claim a pass.

Deployment and dependencies:

- Fresh npm full-media preparation actually created isolated STT/TTS helpers,
  prepared the default Whisper model, and verified the executable converter.
  Required setup/prerequisite failures now stop the full installation.
- HASHI3 and HASHI4 each have their own qualified CPython 3.12 helper paths,
  matching dependency locks, prepared model weights and executable FFmpeg.
  Both `--check` inspections passed; no native dependency entered either Core.
- Windows, WSL, portable Windows/macOS and container deployment templates now
  prepare the media payload. Native Windows preparation was exercised here;
  macOS image creation and WSL/container deployment were not run on this host.
- The durable production Workbench service uses a paired media release built
  from its actual previous release. Existing compiled UI/desktop/chat bytes are
  preserved. Its 55 focused checks passed; no HASHI3-only Call restriction
  remains. Public daily-entry availability reports enabled for HASHI3/HASHI4.
- HASHI4 configuration synchronization uses revision-checked JSON writes,
  retains Agent identities/preferences/secrets, initializes its Call/vision
  declaration and uses its own helper paths. No HASHI4 adoption was triggered.

HASHI3 adoption and observable media:

- HASHI3 was stopped before this task; the approved startup loaded the initial
  repair. Its subsequent `/reboot max` completed with a durable terminal
  receipt. Core PID remained unchanged; shared Functions and all 11 Agents
  adopted the Phone repair and report ready.
- Call: real Chromium microphone capture of a WAV fixture reached the actual
  STT/provider/PAO/TTS services, answered 7 and played through AudioContext.
  The video variant opened a real Chromium camera stream, showed a decoded
  preview and obtained a successful actual cloud vision observation.
- Phone: the same unchanged selected auxiliary model now receives the real
  judgment. WAV microphone audio crossed actual WebRTC, provider transcription
  and returned spoken 7. The audio element's time and received RTP/audio energy
  advanced. Hangup ended local tracks. The first rerun collided with the
  deliberate shared-service handoff; the post-ready rerun passed.
- Recording capability reports supported on the adopted HASHI3. Default
  platform speech produces valid Ogg through the real converter, and the
  Workbench player advances while unmuted. No hidden voice-off choice was
  required to obtain these results.

Ignored operation evidence lives under `state/media-defaults-repair-20261009`:
source/config sync manifests, before/final Core snapshots, shared-reboot
receipt, runtime health, sidecar checks, and `live/` JSON/PNG/audio results.
Chromium's file microphone and test camera are explicit test inputs. Physical
microphone/camera quality, human listening and a physical phone are not claimed.
HASHI4 running adoption and its post-reboot live verification belong to the
user's pending reboot, separately from the completed source/config/dependency
promotion.
