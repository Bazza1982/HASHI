# HASHI3 nightly repair and delivery — 2026-10-08

## 2026-10-09 urgent `/call` follow-up

The current user reports HASHI3 Call unusable and requests an immediate repair
before personal testing. The daily frontend incorrectly projected Call readiness
and rejected startup according to a process-wide instance selection rather than
the page's bound HASHI3 connection. Frontend Connector fixes this in the paired
external frontend release; existing authentication, opt-in and Session fences
remain. HASHI3 Core/Functions and HASHI4 source/lifecycle are unchanged.

Two existing scenarios demonstrate red/green failures. The focused frontend
command `node --test src/features/call/call.test.js src/features/call/callReact.test.js src/features/call/callLauncher.test.js src/features/call/callMedia.test.js`
passes 52 tests, with 0 failed/skipped/cancelled and no selection filter, using
the installed Node 22 runtime. The production build passes; the daily web
service adopts the changed source/assets without closing user applications.

Actual daily-page button/capture/STT/Agent reply/WebAudio playback/confirmed
hangup pass on HASHI3 with a file microphone. The original 5179 page also passes
that path. Connection selection is real and restored after the daily canary;
the scoped readiness/context now also work when the unrelated selection differs.
Physical microphone, camera and hearing remain the user's personal acceptance.
Private receipts remain under `.tmp/call-repair-20261009`; the daily receipt's
late collector page-close exception is retained as a cleanup observation.
The [Call decision](../HASHI_CALL_INTERACTION_2026-10-04.md) records this change.

The current user requests six named nightly items, confined to HASHI3. HASHI4
code/runtime are excluded. Existing desktop/Workbench changes and all of the
user's open applications must be preserved. Test source, deployed bytes, actual
client observations and physical-device/hearing feedback are distinct facts.

Functional ownership: HERV3 owns commentary in Functions; PAO owns Session
concurrency and Remote discovery; Frontend Connector owns the terminal speech
path and external client transport/presentation. Client packaging and installed
feature choices remain platform/instance configuration. No Core migration is
authorized or implemented.

## HN-20261008-001: substantive progress and bounded delivery

The observer previously treated any changed Tool output as a progress milestone,
even a timestamp-only result or a failed operation. It generated the same generic
sentence, asked the Persona packager to rewrite it, and consumed the delivery
interval before the main model could publish a real finding.

Tool completion now records technical activity only. The model owns progress
content. Authored updates are deduplicated and combined at the next available
window, within the existing text bound. Terminal close cancels pending delivery;
Persona and uncertain-transport fences remain intact.

Focused red: three behavioral failures reproduced automatic timestamp/error
reports and two discarded findings. The test timer seam alone was introduced
before that red run; no fixed behavior was used in the red implementation.

Focused green command:

```text
.venv\Scripts\python.exe -m pytest -q tests/test_her_v2_commentary.py tests/test_her_v3_runtime.py tests/test_her_v2_execution_commentary.py tests/test_search_activity.py --basetemp=.tmp/nightly-20261008/pytest-commentary-green
```

Result: 44 passed, 0 failed/skipped/deselected. This covers actual typed Persona
delivery, source ordering, coalescing, repetition, cancellation before Final,
provider commentary ingress and independent technical activity. Runtime
qualification/adoption and the actual external client canary follow separately.

The actual daily-entry canary exposed two additional boundaries. HERV3 uses
`v3_prompt.compile_main_prompt`, so the main-model finding guidance is now also
in that actual prompt instead of relying on the legacy Direct prompt. The
HASHI API auxiliary profile's `default` effort now resolves to the concrete
provider's configured effort instead of being sent as an invalid wire value.
Its focused red reproduced the real Persona failure; the 74-check provider,
HERV3 contract/commentary/runtime matrix is green after the repair.

Both canary shell operations have durable `SUCCESS` receipts and exit code 0.
The client had counted `"error": null` in output previews as a failure, ahead of
the receipt-derived status. Explicit success/failure status now takes priority;
two focused red cases turn green in the ten-check presentation suite. The
temporary QA Agent's invalid Telegram token remains a separate mirror failure;
no other Agent token or identity is copied to repair that test fixture.

The existing public activity `completed` status describes termination, not an
unambiguous Tool outcome. PAO now adds a bounded receipt-derived `outcome` field
while retaining that compatibility status. Missing evidence stays `unknown`,
and partial/unavailable search remains partial/unavailable. The client consumes
that field ahead of prose. Three focused projection cases fail before the change;
all 29 request-activity, search and HERV3 frontend checks pass after it. Both
actual client outcome cases also pass. Private result metadata is not published.

Private evidence is retained in `.tmp/nightly-20261008/`. Earlier files and
uncommitted changes were snapshotted before editing and are not attributed to
this repair.

## Workbench pairing and preservation

The actual Windows service is port 5176; the existing Electron opens that service
from an independent source checkout. The previous default chat connection was
HASHI4. This batch selects HASHI3 through the existing connection writer and
permits Call only there.

A separate release worktree preserves every pre-existing modified/new source
file from the actual service checkout, then integrates the previously verified
Phone/Call and Session/native-component implementation. Merge resolution keeps
newer desktop input/mobile recovery and idle-polling repairs. Existing source
checkouts and user/connection/installation state are preserved. Client release,
service adoption, native acceptance and remaining physical-device evidence are
recorded independently below when observed.

HASHI3 adopted the commentary source through hot `/reboot max`; all 11 Workers
are ready on one qualified Function generation. Core PID and Core/dependency
digests were retained; 646 artifact/source entries matched. No Core cold restart
was used.

The actual Workbench service (5176) now uses the clean paired source. The open
user Electron process was retained. The signed-HMAC HASHI3 selection and Call
availability pass. Actual hardened-package UI checks using a file microphone
pass both full Call capture/STT/PAO/TTS/playback/resume/end and Phone
WebRTC/input/output transcript/playback/end. These are synthetic microphone
inputs through real transports/providers, not human ears or physical phones.

## HN-20260910-014: real local speech dependencies

The real TUI speech request failed because `ffmpeg` was absent. An independent
real Ogg playback probe also failed in the Windows `-Command` argument binding.
The private instance platform configuration now selects a checksummed portable
FFmpeg/FFplay distribution outside the Core interpreter. Functions resolve
conversion through that configuration; TUI passes its explicit launch home to
playback, so another instance's inherited environment cannot choose its player.
The Windows fallback binds filenames as literals in an encoded script.

No dependency is installed in a running interpreter. Configuration publication
uses the absent-file revision from `orchestrator.config_json`, without a blind
retry. Existing voice profiles are retained. Focused TUI checks pass 55/56; the
one command-preview timing failure passes its unchanged focused rerun. Voice
provider/isolation/configuration checks pass 24/24. Real generation/playback and
human listening are recorded separately after the new source is adopted.

## HN-20260913-001: current Remote discovery

The HASHI3 Remote reports actual LAN advertising and browsing ready, three
trusted peers and no static-seed fallback. Authenticated health, peer/protocol
status and Backend API readiness were read from HASHI3. Its discovery source
matches the current checkout; no Remote source change is needed. There is no
`instances.json` seed file in HASHI3, so there is no temporary seed to remove.
The two skipped focused discovery checks are POSIX permission-mode contracts,
not native Windows acceptance. Cross-machine user interaction is separate.

## HN-20261006-005: actual native read-only handoff

The installed qualified client opens its shared browser at the actual daily
service. An actual model/Worker handoff exposed that URL-bearing `get_text` and
`screenshot` calls asked the live browser to navigate before reading, which the
read-only grant correctly denied. Functions' embedded browser adapter now checks
the supplied URL against the same authenticated tab and removes that navigation
instruction for those two observations. A different URL is refused; mutating
and unbound calls retain their existing permission checks. No proprietary
component or Core permission gate is changed.

Focused red reproduces three failures: two live reads attempted navigation,
and a mismatched page was not refused. Focused green and actual installed-client
read-only handoff verification are recorded separately in the nightly receipts.

## Current delivery and live evidence — 2026-10-09 AEDT

Approval is the current six-item HASHI3-only request. Implementation is on the
HASHI3 development branch and an isolated paired Workbench release; original
Workbench source and all 28 pre-existing files are unchanged. The eleven other
HASHI3 pre-existing source files retain their original bytes; the existing FYI
text is preserved with the owning notes appended. Current source and the live
qualification receipt are recorded in the private final invariant, independently
of the earlier checkpoints above.

- HERV3: the actual daily Simple page displays one model-authored finding before
  Final, with two successful shell receipts and no duplicate generic observer
  reports. The separate failed attachment-publication attempt remains a failure.
  The actual main model is DeepSeek in this canary; Luna did not author a concrete
  finding in its earlier attempts. The QA provider/model/effort is restored to its
  original HASHI API/Luna/low configuration after the canary.
- Phone/Call: the daily Windows service uses the paired qualified client source
  and has HASHI3 Call enabled. Installed-package UI checks pass real-provider
  audio input, transcription, reply, playback and hangup; file microphone input
  is explicit. Physical microphones, cameras, phones and human hearing are not
  certified by those checks.
- Native client: the installed package's actual daily-entry browser button,
  authenticated same-tab Worker/model read and selected-Session reply pass.
  The page's unknown value is read without reload and its unsent form remains
  intact. The packaged companion displays its image and presence. The pre-existing
  user Electron remains open; its native main/preload adopts the installed
  release on a later normal launch.
- Sessions: actual daily desktop, mobile-viewport and second-browser views use
  two persistent Sessions of one Agent/provider/model. B completes while A is
  executing; Final and progress stay in their corresponding Sessions. These are
  separate browser contexts on this Windows machine, not physical phones or a
  cross-machine acceptance result.
- Remote: authenticated HASHI3 health and actual Zeroconf LAN observation pass
  advertising/browsing, trusted peers and bounded TXT values. The formal HASHI3
  Remote is already running the matching discovery implementation. No HASHI3
  static seed file is present, so no temporary discovery file is removed. Other
  machines' adoption/cleanup is outside this request.
- TUI `/say`: the real mounted terminal command reads an actual Agent Final,
  generates speech through the configured provider and completes native local
  playback. Player completion is observed; human listening feedback is pending.

Screenshots, provider/Tool receipts, package manifests and live observations are
retained separately under the ignored nightly directory. Neither an unanswered
human question nor mobile emulation is recorded as physical-device/hearing PASS.
HASHI4 is untouched. Only owned test clients quit; user applications remain open.
