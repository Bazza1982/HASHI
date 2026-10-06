# Nightly restart, frontend and Session execution repairs

## Scope and authority

The user authorized implementation and testing in HASHI3 and the test Workbench
on 2026-10-06, including their operational restarts. HASHI4 adoption is separate.
Core is immutable. PAO owns lifecycle and Session scheduling in Functions; the
Frontend Connector and external Workbench consume authoritative projections.

## Qualification work

The old qualification path parsed the same source closure for initial discovery,
the independent probe, final discovery and generation verification. Three local
baseline qualifications took 64.829, 69.026 and 61.470 seconds. These are
qualification times, not total reboot or disconnection durations.

Build each source AST once per manifest and use it for compilation, lazy-import
discovery and ordering. Discard the AST after deriving dependencies, avoiding a
process-wide cache. If the independent interpreter accepted exactly the initial
module set and digest, final verification compares every source byte and the
freshly discovered asset set. An expanded module set still rebuilds the graph.
The independent import/contract probe, Git membership/commit gates, runtime
fingerprints, immutable artifact verification and READY/rollback boundaries stay.

Focused source/asset mutation, lazy imports, compilation, asset membership and
commit-gate checks passed: 16 passed, 3 deferred source-qualified checks. Live
adoption and measured improvement are pending. No speed claim follows from
eliminating work alone.

## Delivery requirements

Reboot start messages must be readable from PAO's durable Session projection
before cutover, even while Worker command routes are fenced. All authenticated
Workbench clients discover the same owner-scoped lifecycle regardless of origin.
Visible chat presentation and banner presentation are distinct evidence.
Success requires a terminal authoritative receipt and restored connectivity,
then dismisses after four visible seconds; failure and unknown persist.
Theme colors, typography, spacing and motion use existing theme semantics.

Remote or unconfirmed shared filesystems default to uploading actual bytes.
Destination connection, instance, Agent, Session and generation remain frozen
through capability checks, attachment staging/commit and one Run admission.

Desktop startup configuration belongs to the installation state, respects an
explicit opt-out and binds an authenticated physical desktop independently of
the selected chat. Initialization grants no control lease or background capture.

Same-Agent concurrency means overlapping actual execution in two persistent
Sessions using the same engine/model, with FIFO inside each Session. Source,
focused validation, running adoption and real frontend evidence are separate.


## Candidate source checks

Three paired, independent qualification probes against exactly the same source
produced medians of 47.152 s with the original algorithm and 24.772 s with the
optimized algorithm: a 47.5% reduction. All six receipts identify the same
generation. Earlier 62.9% figures compared different source snapshots and are
superseded. Qualification, complete reboot and disconnect duration are separate.

Focused checks cover durable fenced projection, lifecycle discovery/acknowledgment,
remote upload destination changes, trusted filesystem identity, desktop
installation binding/opt-out, native frontend components, Session view selection,
queue overlap/FIFO/capacity, native adapter isolation, control-thread targeting
and HERV3 shared-service ownership. The curated gate passed 801 checks, with one
existing skip. Live acceptance and its remaining hardware boundaries are below.

## Live findings during this batch

Native Codex CLI with gpt-5.6-luna completed Session B while Session A was
waiting in a real tool call (13:47:19Z versus A completion at 13:48:41Z).
HERV3 via HASHI API exposed a separate model-switch bug: unsupported retained
reasoning `off` was sent as `none`, rejected by the GPT gateway. PAO now uses
the qualified provider/model effort view for both reload and target changes,
persists target plus repaired effort atomically, and updates only future-turn
configuration after the write succeeds. Compatible effort remains unchanged.
Focused configuration/state checks: 42 passed; live adoption/recheck remains separate.

Real Electron attachment delivery exposed a remaining primary-only admission
check for `surface=workbench`. Authenticated Session API admission now resolves
the already selected, owner/Agent-validated Session without rebinding primary;
legacy shared-primary ingress retains its stale-pointer protection. Foreign
Agent Sessions remain rejected. Fresh-context/Session execution checks: 32 passed.

## Resource and failed-intake boundaries

PAO execution opt-ins are validated before Worker READY. Agent Session capacity
defaults to 2 (1..8); instance execution capacity defaults to 8 and each engine
to 4 (1..64). `global.execution_limits.instance_sessions` and `.engines` refine
the instance budget. OS-owned lease handles span separate Function processes,
include detached completion and release on cancellation/process exit. Queue
reasons derive from that queue and its execution owners: prior Session turn,
Agent capacity, instance capacity or engine capacity. Workbench renders them
only for the selected request. Earliest eligible Session turns preserve FIFO
without a long queue in one Session blocking another Session's available slot.

API invocation capacity is a separate bounded layer, avoiding a Run waiting for
its own nested quota. Defaults: 2 per model (1..8), 4 per engine and 8 total
(1..64), configurable by the corresponding API gateway limits. Distinct model
keys no longer bypass engine/global quotas. Cancellation returns every slot.
HERV3's auxiliary phases remain inside their Run lease and gateway calls also
use invocation leases.

Tool mutation leases use canonical filesystem resources and shared ancestor
intent. Known file writes exclude writes to the same path; independent files
and reads remain parallel. Shell/patch actions exclude their authorized scope,
including nested Workzones in another Worker. Device leases, config revisions
and native Git protection continue to use their existing owners. A failed
multi-resource pass releases all partial leases before waiting; OS failures
remain errors instead of appearing as permanent contention.

The owner-checked Session attachment discard endpoint reuses the existing
atomic unbound-asset cleanup. Any bound asset rejects the entire batch. A known
pre-admission failure may clean its staged identities on the original frozen
authenticated route even after chat selection changes. Changed credentials,
endpoint or instance defer cleanup to retention. Uncertain/malformed Run
receipts retain staged assets and the original send identity; never replay or
discard a possibly accepted turn automatically.

Focused resource, intake, runtime dispatch and gateway checks: 94 passed.
Safe mutations failed for lost filesystem exclusion, missing engine/global
capacity, partial bound-batch cleanup and invalid budget acceptance. Queue
cancellation also has a pre-fix timeout proof. Running adoption and final
frontend/desktop/performance evidence remain separate.

## Completed frontend and execution observations

The same Codex model ran in two native Sessions with B finishing while A was
inside a real tool call. HERV3 through HASHI API and direct API calls using the
same model independently showed overlapping execution. Actual Simple queue
acceptance proved two occupied lanes, a third lane waiting for capacity, and
Session A2 waiting behind A1. Stopping B left A running, admitted C, and A2 read
A1's committed marker after its completion. Text/file drafts and user questions
retained their original Session during switching. These are persisted Run and
provider observations, not concurrent mocks.

All 12 ordinary themes and three Simple themes were captured in real Electron.
The final Simple test showed the stored start message in the source Agent's chat
before cutover after 1.129 s, completed max in 55.188 s and dismissed confirmed
success after 3.818 visible seconds, keeping Core PID 40696. Later performance
comparisons are recorded separately. Two ordered text files, including a Chinese
filename, were uploaded as actual bytes and admitted in one selected-Session turn.

The hardened Windows package was built with qualified Node 22.23.2 and Electron
43.1.1. Its bundled service/UI and native browser started against real HASHI3
three times, including a new deployment directory and return to the first.
Installation desktop binding and preferences persisted, the browser came from
app.asar, settings had no unrelated error, and each app quit normally. This is
relocation/return testing of the candidate; it does not claim a production upgrade.

## Native desktop diagnostic boundary

Real captures matched all three currently connected monitors and the installation
binding's physical host and Windows session. Actual pointer input remains NOT
VERIFIED: SetCursorPos returned false while Windows reported an active, visible,
unlocked input desktop, without a valid last-error code. Clearing a stale 122 left
error 0. A separate SendInput experiment was accepted by Windows but did not move
the observed cursor; that attempted replacement was discarded. No optimistic
input workaround is retained and view/control leases were released.

DesktopController now preserves typed DesktopError failures and releases its own
control before propagation. The device sidecar logs a bounded private failure
event keyed by request: type/code, genuine Win32 error and desktop-state booleans.
It never includes frames, input text, coordinates, titles or raw exception prose.
The HASHI3-only scheduled device task uses the existing --log-dir option to keep
future diagnostics in instance-owned logs. Historical inaccessible/missing logs
have not been reconstructed or counted as verified. Focused desktop/sidecar
checks passed 56 tests; real mouse/key control remains an explicit open boundary.

## Controlled complete-recovery comparison

Three old/optimized pairs used the same HASHI3 checkout, the same 11 Agents and
the same Core PID 40696. Only the qualification implementation changed; equal
no-op source markers forced fresh qualification, warm transitions were excluded,
and all qualified files were committed. The optimized source was restored after
the experiment. Full authenticated health ready plus all accepting Workers took
114.885 / 114.551 / 115.604 seconds before, and 82.378 / 82.639 / 82.760 seconds
after. Medians 114.885 to 82.639 seconds give a 28.1% complete-recovery reduction.
The same-source qualification median separately fell 47.5%; it is not the total
restart reduction. No test or validation gate was disabled.

The previous 55-second frontend result measured cutover, not full startup health:
connector readiness lagged the durable successful receipt by about 27 seconds.
Workbench now keeps a recovering state until authenticated health on that same
connection is ready, then shows success and starts its visible-time dismissal.
This fixes early presentation without rewriting the durable cutover result.
Actual final source adoption and connector observations follow this comparison.

Final observations located the remaining delay in the replaceable Functions
Telegram ingress: its first empty getUpdates used timeout=30, and connectivity
was reported only after that real poll returned. It now uses timeout=0 until a
poll succeeds, then resumes the normal 30-second long polling. Offsets, accepted
update delivery, webhook drop policy, bounded retries and failure readiness are
unchanged. A red/green check verifies first timeout 0, following timeout 30 and
delivery/offset persistence. This file is outside CORE_SOURCE_PATHS. The earlier
28.1% result predates this second optimization; final live timing is recorded
separately rather than retroactively changing that controlled comparison.

## Mixed attachment continuation repair

Native Simple sent one ordered TXT/PDF/PNG/TXT batch to a nonprimary Session.
All four retrieved bytes matched the selected files, but the real HASHI API
Agent answered that no function-call results were supplied. Transport inspection
showed the first request contained the image and original user message; the next
request contained only three tool outputs without a Gateway Session or their
assistant call pairs. Inline media intentionally excludes Gateway caching, so
that delta could not reconstruct a conversation.

HERV3/engine transport Functions now sends complete accumulated messages for
stateless tool rounds. Text-only request-local Gateway Sessions retain their
delta protocol. Transport observations mark a call incremental only when that
cache is actually available. No Core, Gateway cache or attachment writer changes
are required. A real HTTP-boundary test exercises two tool rounds with inline
image bytes and validates the original content plus each assistant/result pair;
it failed on the missing second-round history before the repair. Focused checks
and actual mixed-batch adoption are recorded separately below.

Offline red: one HTTP-boundary test failed because request two began with a
tool result rather than the original system/user messages. Green: 109 adapter,
backend catalogue and selection transaction checks passed. Text-only deltas,
reasoning settings, media fallback, failure audit and cancellation remain covered.

## Final adopted and native evidence, 2026-10-07

The executable Functions source is 80166eb4; the adopted generation is
6c2de62e37f5e0b88f151829a9defa14d9f2f9be36849ebe3ec807c7a757cd5c.
All 645 source/artifact/manifest files matched. Core PID 40696 stayed fixed,
Core source and the qualified Python 3.12 environment fingerprints matched,
and all eleven Agents were ACTIVE, accepting and online with health ready.
This final section is a documentation-only follow-up, not another adoption.

After the immediate Telegram first-poll repair, two source-adoption observations
reached complete authenticated readiness in 64.732 and 62.659 seconds. A further
external Backend API command, with Workbench only observing, reached it in
56.866 seconds using the already qualified generation. Its stored start and
pinned source-Agent notice were visible in 1.383 seconds. The durable presentation
acknowledgment preceded shared replacement, and confirmed success disappeared
after 3.942 visible seconds. These observations are distinct from the earlier
three-pair 28.1% controlled full-recovery result. No validation was removed.

The external command uses the authenticated Backend API's existing Workbench
compatibility ingress; it proves unsolicited lifecycle discovery, not a live
Telegram user send. Telegram-origin routing/mirroring has focused contract
coverage, but an actual Telegram-initiated frontend canary remains unverified.
Ordinary twelve-theme and Simple three-theme captures passed text contrast 4.5.

Actual same-model execution overlap passed native Codex, HERV3 through HASHI API
and direct API. Native Simple additionally proved Session FIFO, question retention,
targeted cancel, queued third-Session capacity release, and original Session text
and file drafts. The selected nonprimary Session's embedded-browser handoff used
a real registered Worker/tool call and painted its unique page value in that
Session; primary binding stayed unchanged. Cleanup used the managed process tool
only after the synthetic Run ended, then the owned app quit normally.

Native mixed-file delivery uncovered two Workbench follow-ups: Multer's default
Latin-1 filename parameters corrupted bare UTF-8 names, and a late compatibility
JSONL echo overwrote a canonical user turn using a completion-time timestamp.
The upload parser uses Multer's supported UTF-8 parameter option; explicit
RFC 5987 encodings and size/count limits remain authoritative. The canonical
merger now orders sequenced deltas and preserves canonical Run questions against
compatibility echoes. Multipart/merger/controller checks passed 86 before the
second-ordering repair; its 48 focused merger/feed/controller checks then passed.
The live red has sixty wrong post-completion observations. The final live green
has sixty correct sustained observations, ordered TXT/PDF/PNG/TXT bytes, intact
Chinese filenames and attachments, one user turn and Run, and actual file-tool
reads of two TXT strings, a plain-text PDF heading and the 31 by 17 PNG dimensions.
A single earlier DOM sample passed while its screenshot later showed reversed
order; it is superseded and is not used to certify final presentation.

The built, served and hardened Workbench candidate is 0df7d2e. Three real packaged
launches used distinct source builds 5e6d033 -> 0df7d2e -> 5e6d033 with one stable
installation profile. The authenticated local desktop binding and notification
preference survived candidate upgrade/rollback and directory changes; bundled
UI/API/native browser assets and physical JPEG views worked, settings had no
unrelated error, and all three apps quit normally. The existing production
launcher/service was not replaced. This is a two-build candidate transition,
not a production installer migration.

Remaining boundaries are explicit: real Windows pointer/keyboard input still
fails despite visible unlocked Default desktop, unrestricted cursor clip and
no observed job UI restrictions; its cause is not established. Three physical
monitor views passed. No optimistic SendInput replacement or privilege change
is retained. Phone's earlier generic browser_session/evaluate query has no
deterministic read receipt: arbitrary browser scripts are intentionally outside
the verified-read allowlist, so its unknown status cannot be upgraded from
model prose. Dedicated read tools are supported but a new actual Phone query
canary, mobile/hearing/other configured modes and camera interaction remain
unverified. Cross-machine INTEL/MSI attachment acceptance and HASHI1/2/4/main or
installed production Workbench publication are outside this HASHI3 batch.
Historical missing exit/authentication/Telegram logs cannot be reconstructed.

Raw red/green, live receipts and screenshots remain separate under the ignored
HASHI3 .tmp/nightly-20261006 directory. The nightly inbox is the per-item status
record; no all-items-closed claim follows from component or curated gate counts.

## Fixed Phone/Call entry correction, 2026-10-07 08:13 AEDT

The separately implemented fixed-entry frontend was omitted from the paired
nightly Workbench checkout. HASHI3 includes backend 4074df0 and the adopted
artifact matches that backend, but Workbench 0df7d2e still uses the old
route-selecting launcher and startVideo callback. The fixed frontend exists
at 88ac18e in the separate development checkout. Earlier inbox wording that
certified fixed dual-entry frontend adoption and ordinary/Simple controls
was incorrect and has been replaced with an explicit correction.

The production Workbench reports source 328e4f2, active HASHI4 connection and
Call availability disabled. Neither HASHI3 nor HASHI4 has call_profiles.json
beside its active Agent configuration. These are additional preparation gaps;
device permission alone cannot qualify a Call entry. The intended camera-shaped
entry starts Call voice with camera off; vision is optional, unconfigured
entries are hidden and capture starts only through an explicit in-call action.

Remaining work is to integrate the fixed frontend without losing the nightly
repairs, configure actual approved STT/TTS targets and optional vision on HASHI3,
and verify the four configuration combinations plus real fixed entry/media
behavior. This confirmation changes records only. No profiles, credentials,
production processes or Core have been modified or restarted.

## Phone/Call completion work, 2026-10-07

The user now authorizes immediate completion on HASHI3 and the scoped test
Workbench. Fixed-entry frontend 88ac18e has been integrated into the nightly
checkout without replacing other batch repairs. The attachment-only optimistic
row repair is included. Busy controls keep their configuration visibility and
show the existing localized busy label.

Actual provider probes using HASHI3's existing secret reference passed STT,
TTS and optional vision. The ignored instance profile selects gpt-transcribe,
gpt-4o-mini-tts with coral/WAV, and gpt-4.1-mini for vision. Existing Phone
configuration remains independently owned. No provider credential is stored
in source or browser configuration.

Native ordinary mode passed fixed controls, camera off at start, reciprocal
busy admission, browser microphone readiness, a real PAO Run and TTS response,
explicit camera sharing/stop, hangup, and real Phone WebRTC connection.
Synthetic media verifies the software path, not physical hearing or microphone
quality. The same run exposed Simple's primary-only backend scope fence.
The Function adapters now accept the owned active conversation and preserve
its canonical result binding. Fresh Session admission failed twice before
the repair; negative owner/Agent/stale/deleted/archived/activity cases passed.
Final offline checks, hot adoption and live Simple verification follow separately.

The final Call/Session/Phone context components passed 247 checks; the paired
Workbench Call/composer/Simple components passed 77. The rendered busy-label
assertion failed against the previous component and passed against the repair.
Core source protection and whitespace checks passed. Hot adoption and native
Simple media acceptance remain separate until the following live receipt.

## Phone/Call ready for user testing, 2026-10-07

The complete runtime gate passed 803 checks with one existing POSIX-mode skip
on Windows. Its first run inherited HASHI4 through PYTHONPATH and saw the other
checkout's package metadata; a scoped HASHI3 path fixed the test environment.
No dependency change, Core edit or relaxed runtime comparison was used.

Hot `/reboot max` committed Functions source 6774c175 and generation
281789c6faa4982e7b5ac4b1e2a9e754f7ea4d9d1c797698c317f00f2ca6afb3.
Core PID 40696 and Core/dependency digests stayed fixed; shared Functions changed
to PID 30856, all eleven Agents returned online and accepting, authenticated
health is ready and all 645 source/artifact/manifest files match. The observer
saw a durable start in 277 ms, full readiness in 57.015 seconds, and automatic
success dismissal after 3.944 seconds. This is an adoption observation, not a
controlled speed comparison.

Native testing exposed one additional Simple layout defect: its absolute
attachment button covered the Phone entry. The shared pair now occupies a
separate normal-flow row above the composer. Direct consumers passed 38 checks;
the live failure is retained. Built/served Workbench source 5801ff7 passed the
final native run: both ordinary entries, real Phone WebRTC, synthetic speech
through actual Call STT/PAO/TTS, explicit camera sharing/stop and hangup. Simple
started both engines in its fresh nonprimary Session. The real Call Run and
transcript remained in that selected Session; primary was unchanged. Three
Simple themes at desktop, 390 and 320 widths passed hit tests, input separation
and minimum 44px targets. No media error or unexpected HTTP error was observed.

The scoped test entry is http://127.0.0.1:5179. Provider authentication, actual
media responses, source adoption and renderer receipts are separate evidence.
Browser test devices and synthetic audio do not establish physical microphone,
hearing, camera content, real mobile or noise/latency acceptance. Those remain
for the user's test, along with other historical Phone-specific open items.
Production HASHI4 and the original Workbench were not changed. This final
documentation follow-up does not require replacing the adopted Function bytes.
