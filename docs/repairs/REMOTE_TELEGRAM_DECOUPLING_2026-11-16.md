# Remote and Telegram lifecycle decoupling — 2026-11-16

## Scope and ownership

- Instance/repository: HASHI4 only.
- Branch: `remote-telegram-decouple`, based on `core5-fix`.
- Owners changed: Frontend Connector, shared Functions/Worker lifecycle,
  Remote protocol/discovery, Remote API notification recovery and platform
  Remote lifecycle adapter.
- Protected Core source and wire contracts are unchanged.

The objective is availability through deletion of duplicate work. Remote and
Telegram are communication capabilities, not prerequisites for Core,
Functions, Worker or local Workbench readiness.

## Removed coupling and idle work

| Previous operation | Previous trigger/cost | New behavior |
| --- | --- | --- |
| Topology publication reloaded configuration, created/started shared Telegram ingress and sent `worker.connectors_start`. | Every topology request, including unchanged Agent/restart events; could fan out over all Workers and external Bot endpoints. | Topology computes a digest and sends only changed `worker.topology` RPCs. Explicit committed-route activation owns connector work. |
| Initial/replacement Agent assembly attempted connector activation and direct topology publication while the shared Functions route was still fenced. | With 24 Agents this produced 24 guaranteed connector no-ops, repeated full route-list traversal and transient topology fan-out before the one authoritative commit. | Draining suppresses both operations. The shared commit owns one connector activation and one final topology publication after routes are usable. |
| Device capability heartbeats refreshed `expires_at` and broadcast the full topology to every Agent. | A healthy 30-second heartbeat caused 24 Worker RPCs although endpoint identity, actions and routing were unchanged. Capability lease timestamps also made otherwise identical topology digests unstable. | Successful heartbeats update only the capability broker. Worker topology contains structural capability facts without heartbeat/lease timestamps; actual registration or removal still publishes once. |
| Shared replacement had a second connector retry owner. | A 5-second exponential loop duplicated the retry already owned by each ingress. | Removed. Every shared ingress and Worker connector has one task owner. |
| Shared replacement reconciled Remote before connector completion. | Could run Remote task/supervisor lifecycle and add an external sidecar to the replacement path. | Removed. Shared hot replacement leaves Remote untouched and commits before connector recovery. |
| Initial Worker startup awaited Remote. | Remote task/config/network failure could delay all local Workers. | Local Workers/services become usable first; initial Remote lifecycle is one non-overlapping background task. |
| Workbench health reconciled Remote and every connector. | Every health read could probe, enumerate, refresh state or activate lifecycle work. | Health is a read-only projection and exposes only existing state/counters. |
| Healthy Remote lifecycle refreshed the fixed actuator/supervisor. | Every lifecycle check could invoke task registration, PowerShell or service refresh despite an owned healthy process. | Owned healthy Remote returns before any actuator/supervisor operation. |
| Remote protocol control woke every 0.5 seconds, checked Core every 5 seconds, handshakes every 5 seconds and processed inflight state every wake. | 120 control wakes and inflight scans/minute while idle, plus repeated local/network checks. | Idle wake is 30 seconds; Core/directory at most 2/minute, liveness 0.5/minute, accepted-handshake revalidation 0.2/minute, bootstrap 0.2/minute. Only active inflight work uses 0.5 seconds. |
| Remote discovery maintenance ran every second and independently refreshed the Agent directory. | 60 cycles/minute plus duplicate `agents.json`/Core checks. | 2 cycles/minute, using the protocol owner's cached directory; unchanged file signatures avoid payload reads and unchanged revisions make no advertisement call. |
| Tailscale discovery polled every 15 seconds and its loop ignored longer failure backoff. | 4 CLI/status reads/minute and repeated failures. | One 60-second owner; failures back off exponentially to 300 seconds. |
| Registry persisted each healthy liveness heartbeat. | Rewrote peer state and potentially `instances.json` on every refresh. | Material changes persist immediately; identical healthy state checkpoints at most every 600 seconds. |
| Restart-notification recovery rescanned receipt payloads every 30 seconds. | Up to 512 JSON payload reads per pass even after delivery. | Reads only new, changed or due records, retains full paged coverage, wakes on an exact retry deadline, otherwise sleeps up to 300 seconds. |
| Peer route resolution synchronously probed candidate addresses. | A local API/status path could inherit several unreachable-address timeouts. | Route resolution performs no probe. Background owners verify routes; failed instance/host/port tuples use a 5–300 second circuit and all-open routes fail immediately. |
| One unreachable instance retried every advertised address in one liveness/handshake pass and persisted every later failure count. | Several addresses could multiply one peer timeout; repeated failures rewrote peer state and `instances.json` without changing the functional offline state. | Each background pass attempts at most one eligible address per instance. Exact failure counts remain in memory, while disk persistence records only the first-failure/offline transitions and a bounded 600-second checkpoint. |
| Unchanged Zeroconf service refreshes were published as new peer snapshots. | Normal mDNS TTL refreshes rebuilt canonical peer state, updated a volatile observation timestamp and rewrote state even though endpoint identity and metadata were unchanged. | The LAN listener suppresses byte-for-byte equivalent service records; the registry independently compares structural peer facts and checkpoints volatile observation time at most once per 600 seconds. |
| Offline peer retries treated rotating address-specific exception text as a durable state change. | A peer that remained offline could rewrite peer state and `instances.json` whenever the selected address or Windows error text changed. | Exact diagnostics remain current in memory. Durable signatures ignore diagnostic text after the functional first-failure/offline transition, and liveness/handshake reuse the existing failure state for a bounded 30–300 second peer backoff. |
| Discovery maintenance reparsed and hashed `secrets.json` every 30 seconds. | Two full credential-file reads per minute even when its file identity was unchanged. | The existing maintenance owner performs one metadata check and reloads/parses the credential only when its source signature changes; startup remains fail-closed and a changed invalid revision retains the last valid in-memory credential. |
| Authenticated `/protocol/status` built the complete protocol projection twice. | Duplicate peer, discovery, directory and activity projection for one read-only request. | One projection is reused to add API capability facts; the endpoint remains read-only. |
| An authenticated inbound handshake rebuilt an unchanged peer observation and let derived `live_status` age between `online` and `stale`. | A healthy HASHI6 heartbeat could rebuild the same route every ~37 seconds and persist the derived age transition, despite proving fresh authenticated liveness itself. | Equivalent inbound observations bypass canonical rebuild, refresh the existing peer's authenticated liveness in memory, and log only at debug level. Route, identity, capability or directory-revision changes still take the normal persistent path. |
| Five-minute static bootstrap re-probed peers already registered as offline. | It duplicated liveness/handshake recovery and produced 14 route probes in one healthy four-minute acceptance segment that crossed the bootstrap boundary. | Bootstrap probes only unregistered instances. Registered peers remain with the liveness/handshake owner, which already reads current routes and applies peer/endpoint backoff. |
| Route and status helpers reparsed `instances.json` on every lookup, and bootstrap read live endpoints before knowing whether an unknown peer existed. | One handshake/liveness pass could parse identical configuration once per peer; an all-known bootstrap still performed payload I/O. | A metadata signature, sampled at most once per second, preserves the parsed instance projection until the source changes. Bootstrap reads live endpoints lazily only for an unregistered instance. |
| A successful Workbench gateway request wrote an INFO record for every receive/authenticate/forward/body/response stage. | The observed `chat-projection/*` client traffic produced about ten durable records per successful GET, amplifying legitimate traffic into multi-megabyte log growth. | Successful intermediate stages are DEBUG and skipped before JSON serialization at the normal INFO level. One completion summary remains INFO; failures, exceptions and 4xx/5xx stages remain INFO. |
| The Windows task runner appended native output with `*>>`. | Windows PowerShell 5.1 ignored `$OutputEncoding` for file redirection and wrote UTF-16LE, doubling ASCII-dominant log bytes and creating mixed historical encoding after restarts. | Native stdout/stderr is merged into `Out-File -Encoding utf8`; exit-code, structured argv, Unicode path and `-B` contracts remain covered. Existing incident logs are not rewritten or deleted. |

## Independent recovery contracts

- Shared Telegram ingress: one task per Agent. `InvalidToken`/`Forbidden` stop
  retries for that token revision. Temporary errors use exponential backoff to
  300 seconds, stable per-connector jitter and a server `RetryAfter` floor.
  Health now reports `automatic_retry=false` for that permanent-failure state,
  while retaining `automatic_retry=true` for a transient disconnected ingress.
- Worker outbound Telegram: one task per committed Worker route. Permanent
  credential errors remain stopped until the Bot-token fingerprint changes;
  temporary retries are bounded and cancellable through the Worker stop event.
- Remote: OS supervisor or explicit lifecycle command owns process recovery.
  Core health reads and Agent restart do not become Remote lifecycle owners.
- Peer discovery: canonical instance identity is deduplicated before endpoint
  probing; recent successful endpoints are preferred and failed endpoints are
  circuit-broken independently. Authentication rejection is revalidated at
  most every 300 seconds until configuration changes, rather than every control
  pass.
- Telegram, Remote and WhatsApp do not share a global retry loop.

Targeted Agent restart explicitly refreshes connector configuration after the
new route commits. The shared Telegram ingress is reused when its token and API
root are unchanged; a new token replaces only that Agent's receiver. This
operation schedules network recovery but does not wait for connection success.

## Retained correctness boundaries

- Remote authentication, expected instance identity and handshake protocol
  checks remain mandatory.
- Private Worker nonce/generation admission and committed route ownership remain
  mandatory before connector activation.
- Telegram Bot fingerprint/offset binding, update ACK ordering and delivery
  idempotency are unchanged.
- Remote restart receipts, notification identity and bounded delivery attempts
  remain durable; the optimization changes when payloads are reread, not their
  meaning.
- A newly adopted or changed endpoint is still verified by the existing
  background bootstrap/liveness owner. A manifest digest or cached address is
  not treated as proof that current network bytes or identity are trustworthy.

## Measurement counters

The existing health/status projections now expose monotonic process-local
counters only:

- Functions: topology requests, effective publications, unchanged/draining
  suppressions, topology RPC attempts, effective connector activation requests,
  draining suppressions and Worker connector RPC attempts.
- Remote protocol: control wakes, bootstrap/liveness/handshake/Core/directory/
  inflight cycles, actual Agent-directory file reads, route probes, circuit
  skips, known-peer bootstrap suppressions, instance-source checks/actual file
  reads and liveness/handshake peer-backoff suppressions. Liveness and handshake
  run counts now represent passes that attempted network work.
- Remote discovery: maintenance cycles, advertisement calls, cheap credential
  source checks and actual credential reloads.
- Peer registry: state writes, `instances.json` syncs, suppressed heartbeat
  writes, suppressed unchanged discovery writes and suppressed unchanged
  handshake/failure-state writes.
- Worker metadata: Telegram connector attempts and permanent-failure reason.

These counters reset with their owning process and do not add a monitor thread,
daemon or persistent telemetry service.

## Focused verification

Offline tests cover pure topology, explicit scoped connector activation,
permanent/transient Telegram failure, token-revision recovery, read-only health,
Remote lifecycle reuse, single-owner scheduling, peer/endpoint deduplication,
circuit fast-failure, cached Agent directory, registry write suppression,
Tailscale backoff and paged notification recovery. Live HASHI4 restart,
30-minute idle and external recovery evidence is recorded only after adoption;
offline success is not presented as live network proof.

Current offline evidence (groups overlap and therefore are not additive):

- complete Remote-focused set: 542 passed, 11 platform/live skips;
- complete Telegram-focused set: 401 passed;
- combined startup/lifecycle/topology/Workbench set: 433 passed, 5 skips;
- post-cleanup reboot/receipt/Remote API set: 151 passed;
- changed-scope Ruff: passed;
- protected-Core diff guard and manifest: passed (no Protected Core change);
- Agent FYI catalogue validation: passed.

Post-adoption duplicate-work regression tests: 109 passed. The focused subset
covering topology, connector commit and Telegram readiness contains 59 passing
tests; Ruff passes for all files changed by this follow-up.

The subsequent Remote idle-write/backoff regression group contains 173 passing
tests and 2 platform skips. The static-bootstrap duplicate-owner regression
failed before the fix by observing an actual route probe for an already known
offline peer; the instance-source cache regression likewise failed by observing
two payload reads for an unchanged file, then failed again when a single status
burst still performed two metadata checks. Both pass after the fix, together with
unknown-peer bootstrap and existing healthy/discovery-owner cases (5/5).
The complete `test_remote*` focused inventory is 481 passing tests and 11
platform/live skips; this includes Remote peer,
discovery, shared-token endpoint, lifecycle, supervisor, routing, delivery and
Workbench gateway coverage. Live idle evidence is recorded separately after
the updated Remote process is adopted.

## Final live acceptance

The final gateway lifecycle build (`39986480`) was adopted through the
HASHI4-only Remote task in 17.891 seconds. During that Remote-only restart,
150/150 direct Workbench probes remained ready with all 24 Agents (7.45 ms
median, 13.17 ms p95). Core PID 16292, Functions PID 24596 and every Worker PID
were unchanged; only Remote moved to PID 26252. The accepted runtime claim
reported commit `39986480ed0b7a840db379e322729630c844fa42`.

A 1,831-second final soak sampled every 30 seconds. All 60 samples succeeded;
Workbench, Remote and all 24 ACTIVE Workers stayed ready and every Core,
Functions, Remote and Worker PID stayed unchanged. Workbench latency was 31.38
ms median / 47.54 ms p95 / 50.49 ms maximum; Remote health was 54.73 ms median /
68.22 ms p95 / 70.83 ms maximum. The temporary InvalidToken Agent remained
local/degraded with `automatic_retry=false` throughout.

Functions activity had zero delta for topology requests, topology publications,
topology Worker RPCs, connector activation requests and connector Worker RPCs.
Remote protocol deltas were 55 idle control wakes, 14 liveness passes, six
handshake passes and six bootstrap passes. Unchanged instance metadata caused
202 cheap source checks but only three payload reads. Discovery ran 60 bounded
maintenance cycles with zero advertisement updates and zero credential reloads.
The registry performed three 600-second checkpoint writes/syncs while
suppressing 48 unchanged discovery writes, 13 heartbeat writes and 70 other
unchanged writes.

The soak included real external Workbench traffic and therefore is a traffic
soak, not a claim of network silence: 6,294 authenticated gateway requests were
observed. They produced exactly 6,294 completion summaries and zero successful
intermediate-stage records. Before the task-runner encoding correction this
segment occupied 5,103,412 UTF-16LE bytes; the UTF-8 runner regression proves
the same ASCII-dominant stream is no longer doubled. Core consumed 12.78 CPU
seconds, Functions 318.84, Remote 31.17 and the 24 Workers together 1,663.48;
these figures include the 6,294 real projections and are not attributed to
background idle loops.

The UTF-8 task-runner follow-up (`ba0f8242`) was then adopted through another
HASHI4-only Remote restart in 18.032 seconds. Workbench remained ready on
150/150 probes; Core, Functions and all Worker PIDs were unchanged, and Remote
moved alone to PID 41612. Twenty uniquely correlated authenticated requests
produced 20 completion records and zero intermediate-success records. The
5,875-byte post-restart segment decoded as strict UTF-8 with a zero NUL-byte
ratio, providing live evidence in addition to the Windows runner regression.
