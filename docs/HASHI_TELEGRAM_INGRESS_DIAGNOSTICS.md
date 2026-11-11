# Telegram ingress diagnostics

Functional owner: Frontend Connector. Engineering layer: shared Functions.
PAO startup health and the authenticated Backend API are derived consumers.
This decision supplements `HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md`.

## Facts and privacy

The existing unique Telegram poller records observed failure stages:
Bot initialization, webhook initialization, getUpdates, watchdog expiry,
Worker delivery, offset checkpoint, status propagation, and Bot shutdown.
The supervisor's swallowed Worker IPC failure is connected to the same
diagnostic owner. No diagnostic opens another poller or changes retry,
webhook, offset, ownership, or delivery acknowledgement behavior.

Each fact binds the configured instance, Agent, shared Function generation,
existing safe Bot fingerprint, UTC timestamp, and `tg-poll-*` operation ID.
Autonomous polling has no invented user request or Run ID. Poll success alone
updates last-success time. Retry and heartbeat do not. Failure counts,
first-disconnection time, actual retry interval, next retry and effective
watchdog deadline remain visible. An accepted batch and successful state
propagation clear the failure streak; a failed delivery cannot reset the streak
merely because the next getUpdates succeeded. Normal pause/stop is distinct
from an observed exception.

Free-form exception text, cause messages, HTTP bodies, update bodies, chat IDs,
URLs and tracebacks are withheld at the first diagnostic boundary. Records
retain bounded exception/cause types and safe standard reason messages;
unknown causes stay `unknown`. PTB rejection types identify Telegram error
classes (401/403/409/429), not a fabricated separately inspected HTTP status.
Only observed numeric status/retry attributes are retained. The same projection
feeds file, console, cache and API, and credential redaction is still applied.

## Durable sink and projection

Records use UTF-8 JSONL below the shared Functions instance's
`logs/telegram-ingress/`. Generation/PID filenames isolate concurrent old/new
shared processes; each stream rotates at 512 KiB with three backups. At most
eight stream groups are retained. Query and periodic cleanup retain seven
days of timestamped records. Directories are private (0700) and files 0600
where supported. The sink does not depend on console filters or Worker log
relay. First failure, error-class/stage/backoff change and recovery are written
immediately; unchanged failures summarize at most every 30 seconds while
in-memory counts and first/latest times stay current. Sink I/O errors have
their own safe type/time in the summary and never trigger update replay.

`telegram_ingress_snapshot(agent).diagnostics` is a bounded health summary.
`telegram_ingress_diagnostics(agent, limit=20)` reads retained facts across
generations, filtered by instance, Agent and Bot fingerprint, with a maximum
50 records. Disk data is reprojected through known fields; appended private
payloads are not an API extension. The Backend API exposes the reader only
through the existing authenticated, Agent/owner-scoped diagnostic route.
Connection state remains owned by the poller; the UI localizes only its view.

## HASHI1 implementation evidence, 2026-10-05

The initial owning red run had nine failures: Remote still selected full health
and the poller lacked persistent diagnostics. The Connector/Remote/supervisor
direct-consumer set then passed 119 tests. Further focused tests passed for
generation/PID streams, strict capacity/retention, real sink I/O failure,
repeated Worker errors and recovery, initialization and real supervisor IPC
failures. The production Bot was not fault-injected and no second poller was
started. Core protection passed. Source adoption and live observations must
be recorded separately by the HASHI1 rollout owner.

Old HASHI4/ying failures cannot be reconstructed from newly added logs. This
scope does not authorize investigation, deployment or reboot of that instance.

## Remote desktop identity preflight

Remote selects a local desktop target using the strictly authenticated,
bounded `/api/v1/instance/identity` protocol `hashi-instance-identity-v1`.
Only exact instance and listening-port matches pass. The local admin credential
is injected solely on the loopback hop; no legacy/LAN unauthenticated fallback
or permanent host cache is used. Full health size is irrelevant to preflight.
Invalid/missing/oversized identity cannot cause a desktop POST.

Before any POST, failure is 503 `local_workbench_identity_unverified`,
`accepted=false`, `retryable=true`. After a desktop request has been sent,
transport loss is 502 `request_outcome_unknown`, `accepted=null`,
`retryable=false`; the input is never replayed to another address. The caller
must inspect the actual desktop outcome before another action.

## Processing acknowledgement and independent readiness

A Worker whose Telegram handlers are not initialized refuses delivery without
acknowledging the update. PTB handler errors propagate into the Functions
processing acknowledgement. Denied actors are a completed policy decision,
not a retryable business failure. Registered handlers complete synchronously.

The poller advances its in-memory offset only after the selected Worker
acknowledges processing and the durable checkpoint succeeds. A failed checkpoint
retains the processing acknowledgement and retries that checkpoint instead of
dispatching the update again. Canonical request idempotency remains the recovery
fence across process interruption. Bot identity and route gates still apply.

Trusted per-Agent extra.telegram_api_url optionally selects an official Bot
API server for both Worker egress and shared ingress. It accepts an HTTPS origin
or an HTTP loopback origin without credentials, path, query or fragment. The
default remains Telegram's public endpoint. A loopback test server allows
contained offline/reconnect acceptance without blocking other instances.
Frontend requests cannot supply this setting. Never log URL token paths.

Session schema 36 marks completion of the full migration before later Workers
use its read-only version fast path. Older versions still migrate completely.
Audio retention and legacy scalar Workzone recovery remain active; future
schemas are rejected instead of downgraded. HERV3 does not enumerate historical
ledger files for an explicitly empty canonical recovery set. Unfiltered legacy
and nonempty recovery retain their original integrity rules.

### Failed checkpoint handoff and initialization retry

A processing acknowledgement retained during a checkpoint failure is included
in the durable shared handoff, even if the normal offset file remains stale.
Recovery merges cursors monotonically only for the same Bot fingerprint.
Transport replacement also retains that acknowledgement. Local handoff cancels
idle login/polling/checkpoint retries; only a dispatched update drains its ACK.

Failed or cancelled Worker application initialization marks its client state
for reset. Before retry, stop/shutdown the application and explicitly close
the Bot clients, including the partial-initialization case that PTB's
Application.shutdown skips. This cleanup belongs to background connector work;
quiesce need not wait for it.
