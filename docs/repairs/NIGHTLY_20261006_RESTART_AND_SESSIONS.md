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
