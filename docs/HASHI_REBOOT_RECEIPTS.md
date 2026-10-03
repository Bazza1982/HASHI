# Agent reboot receipts

## Decision and ownership

Approved for HASHI2 on 2026-09-07. Implementation branch:
`feature/reboot-receipts-20260907`, based on `6c61ad16`.
The user approved code changes and will perform cold adoption separately.
This decision authorizes no operational restart or real message delivery.

PAO owns acceptance, the exact Agent target set, lifecycle outcome and persisted
receipts in the shared **Functions** process. Frontend Connector Functions own
command handling, localized wording and transport fallback. Core has no receipt,
retry, model, command or presentation policy. See
[Minimal Core](HASHI_SLIM_CORE_ARCHITECTURE.md) and
[Layered Runtime Boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md).

`/reboot min` and `/reboot same` replace only the requesting Agent Worker;
numbered and group scopes replace exactly their selected Workers. Only
`/reboot max` replaces shared Functions and all running Agent Workers through
the existing Core handoff. No `/reboot` mode replaces Core or Remote. The new
shared process skips Remote lifecycle setup. Remote has its own lifecycle;
`/restart` is the comprehensive instance operation.
See [Function Adoption and Remote Restart](HASHI3_FUNCTION_ADOPTION_AND_REMOTE_RESTART_2026-09-19.md).

## Observable contract

1. Commands and buttons await acceptance from shared Functions. Acceptance is
   persisted before the execution loop is signalled. A lost acknowledgement is
   reported as unconfirmed, never as proof of rejection or success.
2. A duplicate frontend update returns its existing receipt. A different request
   during a pending/active reboot or shared handoff is rejected as busy. The
   pending request is never overwritten. A group button submits one explicit
   target set; if any target is unavailable, the whole group request is rejected.
3. Runtime sends one concise start notice and one final outcome; the command
   path does not add a redundant acceptance reply. Multi-Agent success notices
   show counts instead of name lists, while failures retain actionable target
   names and success includes elapsed seconds. Admission closes only selected
   Agent routes. During `min|same` other Agents continue on their old Workers;
   during `max` all Agent routes close until the successor commits. No new
   message reaches an old Worker after admission. During `max`, new requests
   into the old shared process are rejected for retry; Telegram leaves new
   updates queued at its server. Telegram's idle long poll is interrupted
   immediately, while already accepted updates finish and checkpoint.
Workbench's Connector keeps the last verified Agent list and conversation
projection while the shared API is briefly unavailable, marks cached Agents
offline, and replaces the list on reconnection. A deliberate instance switch
clears this cache so one instance cannot display another's Agents.
4. The terminal outcome is saved before notification delivery. Delivery never
   changes that outcome and never runs the operation again.

| Outcome | Required evidence | User meaning |
|---|---|---|
| `accepted` | Request and exact target set persisted | Work has not switched yet |
| `candidate_rejected` | Qualification, committed-source, dependency, probe or READY preparation failed before cutover | Reboot was not performed |
| `committed` | Atomic route-pointer exchange completed | New route exists, final health is still pending |
| `rolled_back` | Cutover failed; every previous Worker resumed and passed readiness | Original state restored |
| `online` | Old PID exited; new PID differs; exact Agent/runtime/generation reports ACTIVE, accepting, backend ready and startup successful | Selected Agents are verified online |
| `unconfirmed` | Observation was interrupted, a rollback was incomplete, or a committed switch lacks verified readiness | Result must not be claimed as success or rollback |

Broad receipts additionally persist the shared request identity, old/new shared
process evidence, committed generation, and per-Worker evidence. Remote health
is independent of reboot success. A Core handoff receipt alone is not broad
reboot success.

The compatibility `status` field remains for existing clients, while
`lifecycle_state` carries the precise state above. Each committed/final receipt
also stores per-target old/new and observed PID, Agent identity, runtime ID,
generation ID, source commit and health booleans. Raw exceptions remain in logs,
not in the bounded user-queryable receipt.

Readiness is a Worker lifecycle check, not a provider/model conversation probe.
A failure publishing diagnostics after route commit must not destroy the new
Workers or report a rollback. Gate waiting is bounded by the existing Worker
drain timeout. Cancelled preparation retires candidates; failed cutover releases
route gates and checks the old Workers rather than assuming restoration.

Chinese examples (runtime messages have no persona greeting):

- `🔄 开始热重启…`
- `✅ 月如热重启成功，耗时3秒。`
- `✅ 完整热重启成功，耗时12秒，20个代理在线。`
- `❌ 系统最小热重启失败，月如已恢复原状态。`
- `❌ 系统最小热重启失败，月如暂未恢复在线。`

Names use display metadata and HTML escaping. Both English and Chinese wording
live in the runtime language catalogs. Normal notices omit operation IDs,
process IDs and raw exceptions. Detailed correlation remains in local logs.

## Delivery and recovery

Delivery uses the initiating Agent's configured Bot credentials independently
of its Worker. If that Bot cannot send, the runtime tries the configured
preferred fallback and other Bots belonging to this instance. It retains the
original chat and topic/thread and labels a fallback sender. Shared credentials
are deduplicated and known active Telegram rate limits are respected. There is
no model call, new Agent, new poller or external monitoring service.

Destination identifiers are scoped to their frontend. A WhatsApp or Backend API
command receives the same start and final notices on that surface without an
extra acceptance message; its phone number or channel ID is never interpreted
as a Telegram chat ID.
Proactive fallback in this implementation is for Telegram-origin requests.

Each delivery round has a 15-second total budget and a 5-second per-Bot budget.
The final notice has at most four rounds, with persisted attempts and increasing
backoff respecting Telegram RetryAfter. The attempt is reserved before transport
I/O, so a failed acknowledgement write cannot reset the budget. Telegram can
accept a message whose response is lost; therefore exactly-once delivery is not
promised. The reboot itself is never retried by the notifier.

The bounded instance store is `state/instance/reboot-receipts.json` under the
canonical instance home: at most 50 records and 128 KiB. It contains identifiers,
display names, destination metadata, stable reason codes and delivery status;
never Bot tokens, prompts or raw exception bodies. Writes replace the record
file atomically. Old completed delivery records may be pruned. Pending records
are never discarded to admit a new request; full or invalid storage rejects a
new reboot without interrupting healthy services.

On shared-process recovery, an inherited broad receipt with a published handoff
request remains active so the successor can reconcile Core and Worker evidence.
Other inherited accepted/running receipts become unconfirmed. Pending terminal
notices retain their delivery budget without an extra delayed-result label.
Recovery does not rerun a targeted reboot or infer success
merely because an Agent is online.

For the one-generation transition from legacy Worker-only `same|max`, a newly
qualified deterministic leader Worker may publish the existing Core handoff only
after the schema-1/2 receipt is terminal, committed, and online for every exact
target. The successor does not promote that receipt to the current schema until
Core has written a successful replacement receipt for the expected generation.
This keeps pre-commit rollback compatible with the old shared process and avoids
claiming that the first Worker switch was the complete broad reboot.

`/reboot`, `/reboot status` and the refresh button show the latest receipt for
the same authenticated actor, frontend, original chat and thread. Another Agent in the
same instance can serve this query. The view separates outcome from delivery;
a queued start or an exhausted notification budget is never rendered as success.
The interactive menu labels an active receipt as the current reboot and a
terminal receipt as the previous reboot result. It shows the immutable target
scope and the receipt's UTC `created_at` or `finished_at` time, so an earlier
whole-instance success cannot be mistaken for a newly selected operation. This
menu context does not alter the concise proactive start and final notices.
No final message means **unconfirmed**: the runtime, network or Telegram channel
may be unavailable. The saved status is the recovery/query path when available.

## Implementation and validation status

Source implementation and offline verification are scoped to HASHI2. Production
reboot, cold start, real Telegram delivery and live generation adoption have not
been performed for this change.

Focused checks exercise the real RebootManager and atomic route transaction,
with process/transport boundaries replaced by deterministic test fixtures. They
cover exact targets, group submission, acknowledged command/RPC handling,
identity-scoped status, readiness, rollback failure, storage failure, bounded
retries, cancellation and delivery without the initiating Worker.

Red/green evidence includes missing persisted results on the pre-change source;
retargeting after configuration reordering; a blocked Bot being retried through
its alias; newly accepted work misclassified during watcher startup; and repeated
notification sends when saving the delivery acknowledgement fails; and a
non-Telegram channel being misinterpreted as a Telegram destination. Each focused
case failed before its correction. Reversible mutations also demonstrated
readiness enforcement, truthful rollback, duplicate suppression, bounded pending
history, unready-process cleanup and HTML escaping. All mutations were restored.

The lifecycle/Worker minimum, direct UI/transport consumers, curated Core gate,
and isolated shared Functions/minimal-Core tests are required by
[Testing Policy](TESTING_POLICY.md). Verification counts are recorded in the
implementation commit. These checks are not live acceptance evidence.

## Receipt precision hardening (2026-09-17)

PAO and Frontend Connector Functions now persist and render the explicit
`accepted`, `candidate_rejected`, `committed`, `rolled_back`, `online` and
`unconfirmed` lifecycle. Final success requires a different new PID, confirmed
old-process exit, exact Agent/runtime/generation identity, ACTIVE/accepting and
backend/startup readiness. Schema-1 records are read compatibly and normalized;
new writes use schema 2. This change does not add a retry, timeout, Core restart
or notification dependency to the transaction.


## Interactive recovery feedback (2026-09-08)

User scope: repair misleading two-minute reboot failures and explain recovery
when a user suspects a stuck agent. PAO Functions now reads live Worker activity
before qualification. Busy/queued work or unreadable metadata rejects without
cancelling work or touching routes. This observation is not an idle reservation.
The subsequent route wait and Worker drain each have a 10-second PAO reboot
budget; generic lifecycle timeouts are unchanged. RPC transport adds its existing
10-second allowance to drain. Preparation, delivery and rollback have separate
budgets, so 10 seconds is not an end-to-end reboot promise.

Start notices acknowledge the operation. Persisted failure reasons retain precise
diagnostic categories, while ordinary notices describe only the user-visible
outcome and next action. Recovery state is independently verified even when
restoration fails. Busy responses suggest /stop for work that appears stuck;
/reboot status remains an optional diagnostic view rather than required follow-up.
No forced cancellation, automatic retry or widening of targets is introduced.
Unresponsive activity rejects with a targeted recovery suggestion, not a claim
that the agent is healthy. New behavior requires shared Functions adoption;
an already updated Agent Worker does not establish coordinator adoption.

Validation: five focused regression cases failed on the preceding patch (generic
switch reason and missing actionable status guidance), then passed. Real manager,
route gate, persistent receipts and renderer are exercised with deterministic
process boundaries. Live restart and terminal delivery remain separate acceptance.

## HASHI1 notification diagnosis (2026-09-27)

On HASHI1, two of the latest three Telegram broad-reboot final notices used a
different Agent's Bot from the initiating Agent. The receipt retained the
initiating Agent and original destination, but the prior runtime did not log
whether the initiating Bot was skipped, rate limited or rejected by transport.
The existing fallback therefore made the failure invisible and the visible
sender surprising.

Frontend Connector Functions now logs each reboot notice attempt with its
operation ID, notice kind, source Agent, candidate Agent, safe delivery error
code/type and retry delay. Skipped source credentials and active delivery blocks
are logged too. Bot tokens, request URLs and raw exception text are excluded.
PAO passes the operation ID and notice kind from the owning receipt; the
persisted delivery outcome and retry budget are unchanged. Focused tests cover
the previously silent rate-limit and blocked-source paths. Live adoption and
root-cause verification are recorded separately after the authorized reboot.

The first HASHI1 adoption exposed a second logging fault: the notice logger
propagated only to console output, while `logs/bridge.log` is attached to the
dedicated `BridgeU.Bridge` logger with propagation disabled. Notice attempts
now use that persistent bridge audit logger. A focused red/green check verifies
that the rate-limit and blocked-source entries reach the same logger as reboot
acceptance and outcome records.

After that adoption, an authorized, read-only Telegram probe reproduced a
fresh Bot initialization failure for the initiating Agent's credentials:
`Bot.__aenter__()` calls `getMe`, and its new connection failed before the
actual destination or message was tested. The other initiating Bot completed
the same read-only checks. Both affected reboot results had been emitted after
their initiating ingress had started, yet the notice path created another Bot
connection instead of using the initialized ingress Bot. The prior two failure
classes cannot be reconstructed from the missing logs, but this is a live
reproduction of an avoidable failure in the same path.

The notice sender now reuses an initialized, active ingress Bot for that Agent.
Only when no initialized ingress is available does it create a short-lived Bot.
This removes an unnecessary `getMe` and connection setup at the vulnerable
post-reboot moment while preserving the existing fallback and persisted retry
contract. The attempt log records which transport was used. A focused test
failed before this correction because the notice opened a second Bot despite
an active ingress, then passed after the correction.

The next HASHI1 adoption exposed the startup failure behind the initiating
Bot's absence. The Lily Worker log showed `getMe` returning 200 on all three
attempts, followed each time by a five-second timeout while setting the
default command menu. The Worker then reported `local` and had no Telegram
ingress, even though its Agent remained otherwise ready. Command-menu setup
now retries independently after Bot startup, so a transient menu timeout
cannot disable the Telegram transport. The same diagnosis found that notice
credential lookup did not apply the configuration loader's default token key
(the Agent name) when an ingress was absent. It now uses that default before
considering another Bot. Both defects have focused red/green cases.

## HASHI1 scope correction — 2026-09-27

The user directed a lightweight `/reboot`: `min|same` affects only the selected
Agent, `max` replaces shared Functions and running Workers, and Remote remains
independent. The source change closes selected routes at admission, rejects
new old-process requests during `max`, and leaves Telegram updates queued on
its server. Empty Telegram polling is interrupted; accepted updates finish and
checkpoint. Workbench's Connector retains its last verified Agent list and
conversation projection during the shared API gap. The start and final notices
are short; success includes measured elapsed seconds. The obsolete Remote reboot
helper and assertions were removed.

Implementation and focused offline checks are recorded in the HASHI1 source
checkout and Workbench Connector checkout. No instance reboot, Remote restart,
or live frontend acceptance was authorized for this correction; source, tests,
and live adoption are separate facts.

## HASHI1 release adoption warning — 2026-09-27

The user observed that `/reboot max` and `/restart` both reported success while
the changed Functions source had not been adopted. Source qualification requires
the exact Function manifest to be committed in Git. When qualification fails,
the last verified generation can restore availability, but process liveness
must not be presented as adoption of the candidate code.

PAO passes a bounded adoption result (`qualified` or `fallback`, with a reason
code) from release qualification into the shared Function process. A broad
reboot receipt records that result, and the Frontend Connector shows an explicit
warning when a successful process replacement used the previous generation.
The Backend API health result exposes the same status as a warning issue, so
Remote's existing degraded-restart notification explicitly warns after a cold
restart too. Recovery still completes and the previous generation remains
usable. Neither notice claims that new source was adopted without evidence.

On HASHI1 `main`, the user authorized this correction and its necessary hot
adoption. The source was committed before `/reboot max`. The final receipt
reported `succeeded`, a new shared generation, `adoption.status=qualified`, and
all six Workers online; Backend health reported the same generation and
adoption status. The full curated Core gate passed (725 tests), plus the
Function/Worker/reboot minimum (170 tests). A cold `/restart` fallback was
verified offline through the health and notification contracts, not by a live
cold restart.
