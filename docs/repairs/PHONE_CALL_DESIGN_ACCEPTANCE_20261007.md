# Phone/Call design acceptance — HASHI3, 2026-10-07

## Scope, approval and current readiness

The user requested working Phone/Call entries and design review, limited this
continuation to HASHI3, and had already authorized its hot Function adoption and
the associated isolated test frontend's restart. Frontend Connector owns the
Functions service/instance configuration and the paired external UI. Core is
immutable. This is a ready-for-user-testing checkpoint, not production adoption,
main publication, every provider's certification or physical-device acceptance.

Current Function source is 6774c175, with generation
`281789c6faa4982e7b5ac4b1e2a9e754f7ea4d9d1c797698c317f00f2ca6afb3`.
The paired client is built and served at 9edaa03. Authenticated health is ready,
all eleven Agents are online, and 645 source/artifact/manifest files match.
Core PID 40696, Core source and dependency digests remain unchanged. The scoped
test entry is `http://127.0.0.1:5179`, with only the HASHI3 connection configured.
QA and agent1 expose configured Phone, Call and optional camera, with no residual
Call reservation after cleanup.

The independent entries and visual requirements are governed by
[the current interaction decision](../HASHI_CALL_INTERACTION_2026-10-04.md).
The external frontend's owning design is
`docs/design/call-ui-ux-design-2026-10-04.md`; historical PRD route selectors and
header-video upgrades are superseded by the approved fixed-entry update.

## Requirement-to-evidence review

| Requirement | Verified behavior and evidence |
| --- | --- |
| Fixed independent entries | Phone icon starts/returns to Phone; camera-shaped icon starts/returns to Call with camera off. Native ordinary/Simple starts and configuration matrix passed. No next-call selector or activate/restore write is used. |
| Configuration drives visibility | Both configured, Phone only, Call only and neither configured were exercised through actual revision-aware settings. Unconfigured entries disappeared; configured busy entries remained. Configuration and original absent QA Phone file were restored. |
| Vision is optional | Call with STT/TTS and no vision opened its microphone; in-call camera was unavailable. This did not hide the voice entry. |
| Camera requires explicit action | Entry startup, return and refresh do not enable capture. Explicit in-call camera produced fresh real provider observations, and confirmed off released video tracks. |
| Camera permission refusal | Native browser permission was denied and getUserMedia returned NotAllowedError. After confirmed camera-off, a second real microphone acquisition resumed voice and localized guidance stayed visible. Granting browser permission and manually retrying succeeded. |
| Visible, themed recovery | The pre-fix notice lay below the scroll clip and failed native geometry. The repaired notice is outside the scrolling body, above fixed controls. All twelve themes at desktop, 390 and 320 widths kept the complete notice and three 44px-or-larger controls visible and hit-testable. Screenshots were inspected. |
| Bound conversation survives reading | Actual React hook and native Simple red failed on navigation-induced hangup. Green preserved the original Call while selecting a new conversation; return did not redial. Backend poll/media validation retains frozen Session ownership and generation. |
| Correct conversation results | The selected owned active nonprimary Simple conversation admitted both engines without changing primary. A real Call Run and persisted transcript stayed in that selected conversation. Owner/Agent/stale/archived/deleted/activity negative fences remain covered. |
| Busy admission and cleanup | A separate client held a real reservation. Both configured entries showed the busy label and rejected new starts. Reservation release was confirmed; refresh did not automatically redial or open camera. |
| Complete Call voice path | Native file microphone, AudioWorklet and volume segmentation sent actual audio through configured STT, PAO and TTS. The requested number was answered, WebAudio playback completed, listening resumed and local tracks ended. No turn was directly injected into this canary. |
| Complete Phone voice path | Native file microphone crossed actual provider WebRTC. Input/output transcript fragments included the request and answer; remote audio time advanced unpaused/unmuted and inbound audio energy was positive. Hangup ended local tracks; no camera was requested. |
| Acoustic filtering boundary | Real configured STT rejected synthetic silence, white noise and a pure tone as no speech. These probes created no PAO turn and do not certify all physical noise or quiet short words. |
| Existing menu/locale/failure contracts | Prior Function/command checks plus current Call/controller/React/media/Phone direct consumers passed. Stable permission guidance exists in the four active UI locales. The final focused run passed 79 cases; the complete client build passed. |
| Adoption is an independent fact | The final source/artifact/runtime and frontend-source receipt passed separately from browser rendering and provider responses. Follow-up HASHI3 changes are documentation only and do not replace executable Function bytes. |

## Evidence and genuine failure proof

Private receipts, logs, scripts and screenshots are kept in the ignored
`state/call-entries-20261006/readiness-20261007/followup-20261007-evening/`
directory, with an artifact hash manifest. The earlier ready checkpoint remains
separate rather than overwritten.

- `call-session-hook-red.log`, `call-session-hook-green.log`, and native
  `call-session-navigation-native-red.json` / `-green.json` identify the
  navigation hangup defect and repaired pinned binding.
- `call-camera-permission-red.log` / `-green.log` protect actual controller
  denial recovery; the first focused red was 33 passed and one failed.
- `call-camera-denial-native-notice-red.json` and
  `call-camera-denial-notice-red.png` identify the
  rendered clipping defect. `call-camera-denial-native-green.json` and desktop/
  mobile notice screenshots prove visible guidance, actual native refusal,
  microphone resumption, permission grant, vision, off, refresh and hangup.
- `call-config-matrix-native-r2.json`, `call-remote-busy-native-r2.json`,
  `call-microphone-pipeline.json`, `phone-microphone-pipeline-r3.json`, and
  `call-cloud-noise-audit.json` are separate live boundary receipts.
- `call-notice-focused-green.log`, `call-notice-final-build.log` / `.exit`,
  `call-ready-contexts.json`, `call-ready-adoption-state.json`, and
  `call-final-core-guard.log` record validation, readiness and the final invariant.

Initial harness failures are preserved separately. Fake-UI permission bypass,
headless-shell microphone limitations, permission assignment to the wrong
browser context, early response observation and the test-server startup race
are not counted as product regressions or successful acceptance. The final
permission test uses native browser permission state and real getUserMedia;
it does not substitute a synthetic permission error.

## User device acceptance

On the scoped test page, select the intended Agent. The Phone entry begins
Phone; the camera-shaped entry begins Call voice. Permit the microphone and
check a spoken request and reply. Inside Call, explicitly enable camera, ask
about a visible object, switch it off, minimize/return and hang up. Reading
another Agent or conversation must preserve the original foreground Call.

Browser fixtures prove the software path. Physical microphone, audible speaker
output, camera content, a real mobile browser, noisy-room/quiet-word accuracy
and acceptable conversation latency still require the user's devices. Buffered
telemetry timing is not a controlled end-of-speech latency benchmark. No
additional provider mode is certified by the selected OpenAI canaries. The
older generic browser_session/evaluate query is deliberately not promoted from
model prose to a deterministic read; arbitrary scripts are outside the
verified-read allowlist. Production/main rollout and the rest of the nightly
batch remain separately scoped.
