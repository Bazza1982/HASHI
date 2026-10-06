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
