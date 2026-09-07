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

`/reboot` still replaces only Agent Workers. It does not replace shared Functions
or Core. This feature changes both shared Functions and Agent command Functions;
a running old shared generation will not acquire the new acknowledgement RPC
from an Agent-only reboot. The planned operator cold start adopts both together.

## Observable contract

1. Commands and buttons await acceptance from shared Functions. Acceptance is
   persisted before the execution loop is signalled. A lost acknowledgement is
   reported as unconfirmed, never as proof of rejection or success.
2. A duplicate frontend update returns its existing receipt. A different request
   during a pending/active reboot or shared handoff is rejected as busy. The
   pending request is never overwritten. A group button submits one explicit
   target set; if any target is unavailable, the whole group request is rejected.
3. Runtime sends a concise start notice, then performs the existing verified
   candidate/drain/atomic route-switch transaction. Target membership is frozen
   at acceptance, even if numbered configuration ordering changes afterward.
4. The terminal outcome is saved before notification delivery. Delivery never
   changes that outcome and never runs the operation again.

| Outcome | Required evidence | User meaning |
|---|---|---|
| Rejected | Invalid scope or candidate preparation rejected before cutover | Reboot was not performed |
| Succeeded | Routes committed; exact new Worker PID/generation reports ACTIVE, accepting, backend ready and startup successful | Selected Agents recovered online |
| Failed, restored | Cutover failed; previous Workers resumed and passed readiness checks | Original state restored |
| Failed, unavailable | At least one previous Worker did not recover | Name the unavailable targets; list restored targets separately |
| Unconfirmed | Interrupted transaction or committed switch without verified readiness | Result has not been confirmed |

Readiness is a Worker lifecycle check, not a provider/model conversation probe.
A failure publishing diagnostics after route commit must not destroy the new
Workers or report a rollback. Gate waiting is bounded by the existing Worker
drain timeout. Cancelled preparation retires candidates; failed cutover releases
route gates and checks the old Workers rather than assuming restoration.

Chinese examples (runtime messages have no persona greeting):

- `🔄 系统正在最小热重启：月如……`
- `✅ 系统最小热重启成功，月如已恢复在线。`
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
command receives acceptance and can query its stored result on that surface;
its phone number or channel ID is never interpreted as a Telegram chat ID.
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

On shared-process recovery, inherited accepted/running receipts become
unconfirmed. Pending terminal notices are retried within their existing budget
and labeled as delayed historical results. Newly accepted work in the current
process is not mistaken for an interrupted previous operation. Recovery does
not re-execute a reboot or infer its outcome merely because an Agent is online.

`/reboot`, `/reboot status` and the refresh button show the latest receipt for
the same authenticated actor, frontend, original chat and thread. Another Agent in the
same instance can serve this query. The view separates outcome from delivery;
a queued start or an exhausted notification budget is never rendered as success.
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
