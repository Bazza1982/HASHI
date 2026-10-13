# Nightly inbox repairs on HASHI3, 2026-10-06

The user authorized reviewing every unresolved inbox item, repairing/testing on
HASHI3, modifying/reloading Workbench and restarting HASHI3. This does not authorize
production HASHI4 adoption or a Core major migration. The existing development
consolidation contains prior PAO/PCM/HERV3/Frontend Connector repairs; historical
inbox states are not evidence that those implementations are still absent.

Owners: PAO owns transcript projection, capability facts and creation policy;
PCM owns context; Frontend Connector owns Workbench presentation/transport.
All code changes remain Functions or the independent Workbench repository.

## Current changes

- HN-20261006-001: canonical messages retain their source order; log-only rows
  are interleaved between shared anchors by UTC instant before pagination.
  The returned cursor acknowledges only visible canonical records. Combined
  overflow correctly reports incomplete history. Eight scheduled results after
  sixty old log rows are retained, deduplicated and not replayed by the next poll.
- HN-20261004-011: reaching the top of the rendered window reveals already
  retained history even when the server has no more pages. The viewport anchor
  remains stable. History requests and rendering are fenced by connection,
  Session/context and request lifetime; stale completion cannot mutate a new view.
- HN-20261004-001: system/custom avatar preferences use the authenticated Remote
  instance identity instead of a connection alias/address. Browser endpoint
  headers still fence stale requests. Authentication failure supplies no fallback
  identity; failure is not cached. The currently verified route carries its legacy
  preferences once, retaining originals and never overriding a newer stable scope.
- HN-20261004-003: saved selections, first-load selection, panel ordering and new
  selections retain all Agents. There is no hidden nine-Agent truncation.
- HN-20261004-002: Workbench renders PAO pending questions in their actual Session
  and submits an authenticated answer to that question, without creating a Run.
  Choices are not preselected. A lost response preserves the exact answer/key for
  explicit retry; foreign/stale questions are excluded. Four UI locales derive
  from one renderer catalogue. Backend scope/expiry/idempotency remain PAO-owned.
- HN-20261003-001: voice preflight reads the actual instance voice-transcription
  contract before microphone access. Typed `voice_message` audio follows the PAO
  transcription/Safe Voice path even when the selected model accepts no native
  audio; ordinary audio retains the model capability gate. Workbench preserves
  the audio type instead of accidentally submitting it as a generic attachment.
- HN-20261004-005/006: the already implemented PAO template contract is enabled
  explicitly in ignored HASHI3 configuration using configured `agent1` as the
  general-purpose policy source. This grants no template identity, tools, secrets
  or workspace permissions. Restricted creation remains supported. Native audio
  is enabled on HASHI3 only; a declaration alone is not a transcription pass.
- An additional reproduced PCM reference defect truncated the shipped FYI at
  12,000 characters, hiding later guidance. Historical approvals/receipts are
  archived intact; the active reference retains all owner/security rules and
  fits the existing budget without changing the loader or widening context.

## Evidence

The ignored `.tmp/nightly-20261006/evidence` directory records actual commands,
exit codes, output and red mutation receipts. The history regression failed on
the initial source and the complete owner module passed after repair. Avatar,
selection and chronology regressions failed before repair. Reversible actual
renderer/owner mutations independently rejected frozen-history, foreign-question,
premature microphone and address-bound avatar behavior; bytes were restored.

Focused backend history: 33 passed. Existing creation/Phone/questions/wiki/Telegram
component baseline: 113 passed. External question HTTP/card checks preserve the
exact idempotent payload and confirm no Run submission. Active FYI: red truncation
reproduced, then 3 passed after documentation repair. Full offline and frontend
checks, committed source qualification, runtime adoption and native frontend
observations are recorded separately below when complete.

## Boundaries still requiring evidence

External credentials/provider retirement cannot be repaired by replaying old
business requests. Physical phone/audio and
the dual-browser matrix cannot be inferred from synthetic/component checks.
Other-instance adoption and main merges stay outside this turn's HASHI3 scope.

## HN-20261006-002 and native process protection

The newly appended HERV3 interruption report is included in this authorized batch.
The real async Worker job facade reproduced loss of confirmed write evidence;
the owner now awaits it and preserves existing proof if job inspection fails.
No-change failures, completed operations and verified effects have distinct counts.
Missing/corrupt records and byte/action caps are incomplete, not zero uncertainty.

Only the unfinished model call may continue when every previous tool has an exact
verified read receipt or a completed durable operation checkpoint binding returned
output. Whole Run/stage/tool replay remains prohibited. An actual file-write
fixture writes once, survives an interrupted model call and completes; missing,
pending, modified-output and foreign-tool proofs refuse recovery.

The stream-read inactivity limit applies without fallback configuration. Private
diagnostics retain exception causes, actual network phase timings and wire refs.
One persisted public failure supplies canonical `her-v3`, counts and a recovery
decision to Telegram and Workbench. Unknown error codes remain visible; refreshing
history preserves failures, and no failed Run becomes a successful final answer.

Focused recovery, native-audio and persistence checks: **472 passed, 1 skipped**.
The actual React/controller error scenario reproduced removal on refresh, then
passed across refresh, view changes, Agent switches and a fresh mount; **96 passed**
including feed/transcript owners. Full Workbench UI checks: **706 passed**.

HN-20260918-001: actual Codex CLI 0.160 supports a vetted, command-scoped native
hook when configured as valid TOML overrides. An ignored user profile does not
load hooks. The Function adapter now supplies only the owned hook, installs no
persistent global profile and verifies a fresh startup receipt. Normal coding and
inspection remain available; process termination uses managed tools, which refuse
runtime ancestry. The real adapter/CLI denied a termination; its sacrificial test
process survived and the model returned `NATIVE_PROCESS_PROTECTED`. This is an
ordinary command guardrail, not a claim of complete OS enforcement or coverage
of arbitrary programs. The old Core outage's cause is still a separate question.

The pre-extension whole HASHI3 offline suite passed **6,156 tests, 33 skips,
199 deselections**. Current final-source qualification and adoption are recorded
separately below; the earlier pass does not cover code written afterward.

### Voice live boundary, 2026-10-06

The first recording after final-source adoption was still rejected in HERV3
Execution while its isolated STT finished afterward. This exposed a real
remaining boundary: only retired Triage awaited the transcript. The current
Execution and text Direct routes now consume the confirmed transcript before
the model call, keep sibling attachments, and refuse discarded/unavailable STT.
The original regression failed for Execution and Direct and passed for Triage;
all three are covered. This recording was terminally failed, not blindly replayed.
Subsequent independent voice acceptance and source adoption are recorded separately.

### Final public failure and qualification checks

Public error codes accept bounded typed identifiers, never arbitrary exception
text. A regression with a malformed secret-bearing code reproduced its inclusion
in the human-readable failure; the shared normalizer now runs before formatting
as well as metadata projection. Public failure, delivery and reboot consumers:
**76 passed**. The missing start-notice acknowledgement reason now has English
and Chinese renderer text.

The expanded whole offline run recorded **6,184 passed, 33 skipped, 199 deselected,
11 subtests passed and two failures**. One was the old error-code whitelist
expectation, covered by the corrected conformance check above. The other correctly
rejected a generation while its source was being edited during the long run;
the configured-observer module is rerun against committed, unchanged source.
The voice-source qualification gate independently passed **791 tests, 1 skip**.
Full, focused, committed-source and running-generation results remain separate.

### HERV3 confirmed voice projection

Workbench recovers a pending transcript from the exact existing Run after refresh;
slow STT retains its accepted receipt, and confirmation remains available while
that native Run is waiting. Real q4 recovered its original recording, confirmed
the same Run and returned Blue River 7. It also exposed another HERV3 boundary:
the compiler preferred PCM's old current request over the transcript-bearing
fallback goal, causing redundant transcription tools. This is not counted as
proof of the repaired model input.

The provider now derives PCM's current user request from the released transcript,
preserving trusted sections, history, and sibling media. The actual HERV3 compiler
regression failed in both Direct cases before repair. Native voice, admission,
confirmation, multimodal and adapter consumers: **165 passed, 1 skip** afterward.
The complete committed-source qualification preceding this repair passed
**791 tests, 1 skip**; the final generation is qualified and adopted separately.

### Qualification scanning boundary

Committed-source rechecks exposed a separate reproducible timeout: the commit
gate enumerated all untracked checkout files, including unrelated retained test
trees. HEAD membership already rejects every unpublished manifest path; staged
or working-tree changes remain independently rejected. The redundant scan is
removed without increasing timeouts or weakening that gate. A real temporary
Git repository accepts unchanged published source when that irrelevant scan is
unavailable, and still rejects a newly added uncommitted Function module.

The max reboot with operation f04394b2d2c24dd8b50c2c41a576e9fa replaced Workers
successfully but retained the previous bd525f9f generation after qualification
fell back. It is not counted as adoption of the subsequent transcript projection.
The final accepted generation must match the intended committed Function bytes.

### After-work adoption and real device checks

The authorized max reboot with operation 9cadf64488814cdf838ef2af1632cadf adopted
committed generation 15a06d974894c9d55bd86e2e03830a1c6808190f93775d35f121c248acddfe46
from 42b58ce9. All eleven Agent Workers matched; Core PID 39952 remained live.
The real Antigravity replacement returned a fresh marker through the temporary
QA Agent. A separate LocalSystem task launched the qualified Antigravity route
under the active interactive user's identity and returned a fresh marker;
the task was removed after completion. This proves that route's H3 identity
boundary, not the health of the retired Gemini client or every Google account.

Independent public Desktop control acquired its lease, typed Unicode, dragged
the test element and released control. The earlier busy result was the test's
own nested desktop-lock conflict. Chrome and Edge then performed real H3
HERV3 browser actions against their separate registered Workers. Native host
startup had exposed a new buffering defect: small frames were flushed only at
EOF. The Windows installer now generates a launcher that flushes each chunk;
the compiled real-process regression failed before repair and passed afterward
(4 owning checks), with 60 consumer checks passed and 7 platform skips.

A real LifeCam microphone/OpenAI Phone call transcribed an acoustic test phrase,
returned it in cloud speech, and confirmed provider shutdown after UI hang-up.
Its two delegated Agent actions failed before Provider work because trusted
origin normalization dropped their frozen context IDs. They also remained
visibly running after the queue caught the prompt exception. Both defects have
focused red/green proof (2 failed / 20 passed before, 22 passed after), and all
234 direct-consumer checks passed. No action is automatically replayed.

The initial expanded qualification recorded 790 passed, 1 skipped and three
expected unpublished-source refusals. The Function commit guard remains intact;
committed-source qualification and fresh-call adoption follow separately.
Physical desktop/microphone evidence does not establish mobile handset or
human listening acceptance. The earlier unexpected Core exit remains a
separate historical investigation.

### Fresh Phone and recording adoption

Committed-source qualification passed **793 tests, 1 skip**. Max operation
16b258d978bb40d7b598b94b3b7ae3a9 adopted generation e4d1747d from e6b2f41f
in all eleven Agent Workers; shared Functions became ready and Core PID 39952
remained live. The two pre-fix stuck Phone Runs were separately cancelled through
their exact public Run scopes, with execution_missing=true; neither was replayed.

A fresh physical LifeCam/cloud call admitted one real Get-Date request, completed
it, returned Phone speech, confirmed provider close and retained its final detail
for the next text request. Actual Provider-input audit preserves snapshot/cutoff
and source-event identities. The generic read tool still correctly carries
unknown effect verification; this is not proof of a verified write.

The separate physical recording survived an actual page reload, recovered its
same confirmation/Run, and completed with Violet Harbor 92 and zero tools. Native
microphone tracks ended. Stimulus used generated Windows speech through physical
speaker/microphone, not a synthetic replacement track. It does not prove handset
or human listening acceptance. A later Phone browser query completed its tools
and returned audio, but the model selected prompt text instead of the newly
inserted test marker; that semantic check is recorded as not passed.

### Streaming Phone representation

A new Codex lane in the same QA Session was rejected before Provider work:
309 real word fragments held 1,447 speech bytes but repeated metadata occupied
67,052 bytes. The newly repaired terminal handler correctly settled that Run as
failed. The Phone serializer now shares adjacent call/source scope and declares
event columns once, retaining every exact fragment, role, source ID, sequence and
time range. Old durable snapshots remain readable; the byte/fragment bounds,
frozen authorization cutoff, consumption ledger and source transcript are unchanged.

The two word-stream regressions failed before this representation repair and
passed afterward (**18 owning checks**). Direct consumers, committed-source
qualification and live new-Engine acceptance follow separately.

The committed Phone representation gate passed **793 tests, 1 skip** and the
real artifact probe accepted 5f2830f2 from f19eb2ce. Max operation
c10eccc3c2d447b8b31037c445d5d618 then failed adoption and restored e4d1747d;
it is not counted as activation of the Phone repair. Core stayed PID 39952.

### Windows live endpoint publication

The failed adoption has a concrete cause distinct from the old Core outage:
at 19:02:54, atomically publishing service_endpoints.json returned Windows
access-denied while replacing the destination. The Backend API owner closed
its unpublished service, so all eight Codex Workers rejected initialization
with "live service endpoint is unavailable: workbench". HERV3 initialization
alone did not qualify that generation; the replacement correctly rolled back.

PAO's shared-Function publisher now retries only that pre-commit Windows rename
for a bounded interval, with the exact same closed/fsynced candidate. Publication
or removal failure restores the previous in-memory route and revision along
with the unchanged durable file. Instance/port validation is retained, and no
startup, Run, model call or tool action is replayed by the write retry.

Three focused regressions failed before repair and passed afterward. They
include a real native Windows read handle that forbids rename until released,
and publication/removal failures preserving prior state. A persistent native
lock also refuses without inventing a live route (**10 owning checks passed**).
Direct consumers, final qualification and adoption are recorded separately.

Five exact obsolete HASHI3 AGY launcher tasks from September 16/17 were Ready,
had no future trigger or active runner, and were confirmed against their exact
H3 executable/arguments before cleanup. Their definitions and original files
were archived, and those five tasks were unregistered. No user program was
terminated, and unrelated task registrations were retained.

### Codex native-hook configuration isolation

The endpoint repair's committed gate passed **797 tests, 1 skip**. Max operation
6a793510dca4437c8b9999646173bbed adopted f83d7e46 in all eleven Workers; Core
PID 39952 stayed live and health returned ready with no issues. A fresh Fixed
Codex acceptance then failed before reporting a Provider turn: ignored user
config plus the inventory-derived enabled-only node_repl override recreated a
server without a transport. This is a PAO adapter composition defect, not an
account restriction. The conservative unconfirmed-exit receipt is retained and
the failed Run is never replayed automatically.

Disabled overrides for the owned-hook path now carry only a same-type transport
skeleton. No external credential/argument is copied; unknown types fail before
launch, and the request-local HASHI gateway remains required. Both new/resumed
CLI paths are covered, including surviving same-type project config. HERV3's
app-server continues to retain its normal user config and enabled-only isolation.

Five focused checks failed before the fix. The full owning CLI/app-server/native
hook set passed **76 tests, 2 platform skips** afterward. Native Codex 0.160
reproduced the missing-transport error without model execution; a deliberately
unknown provider then proves valid config decoding before any model/tool call.
User-wide Codex configuration is unchanged. Consumer checks, final committed
qualification and fresh actual frontend acceptance are recorded separately.

### CLI Device request counter collisions

After native-hook adoption, a fresh Fixed Codex Chrome read completed and the
new Engine consumed all 309 exact Phone fragments once. Later requests included
zero duplicates. A separate Edge task then failed with the Device Worker's
replay rejection: two different canonical requests each supplied CLI call "2"
within the replay window. The earlier tool had already reached that Worker.
Raw CLI counters are request-local, so this was a sender identity defect.

PAO Tool Registry now namespaces wire IDs by canonical request, Agent, task and
call. Missing diagnostic scope uses a registry-local nonce. The hash deliberately
excludes arguments and action, so changed parameters cannot disguise a replay.
Device identity, Worker replay guard, leases, task/browser binding and original
tool audit IDs remain unchanged. Three focused cases failed before repair;
58 owning Registry/Broker/Device Worker checks passed afterward. New-request,
cross-Agent and unscoped-registry counter reuse pass, while same-call repeats
are rejected before execution. Committed qualification and actual frontend
acceptance follow separately; no rejected request is replayed automatically.
