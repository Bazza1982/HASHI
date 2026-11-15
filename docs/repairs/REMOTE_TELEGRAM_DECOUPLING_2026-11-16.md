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

## Independent recovery contracts

- Shared Telegram ingress: one task per Agent. `InvalidToken`/`Forbidden` stop
  retries for that token revision. Temporary errors use exponential backoff to
  300 seconds, stable per-connector jitter and a server `RetryAfter` floor.
- Worker outbound Telegram: one task per committed Worker route. Permanent
  credential errors remain stopped until the Bot-token fingerprint changes;
  temporary retries are bounded and cancellable through the Worker stop event.
- Remote: OS supervisor or explicit lifecycle command owns process recovery.
  Core health reads and Agent restart do not become Remote lifecycle owners.
- Peer discovery: canonical instance identity is deduplicated before endpoint
  probing; recent successful endpoints are preferred and failed endpoints are
  circuit-broken independently.
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

- Functions: topology requests, topology RPC attempts, connector activation
  requests and Worker connector RPC attempts.
- Remote protocol: control wakes, bootstrap/liveness/handshake/Core/directory/
  inflight cycles, actual Agent-directory file reads, route probes and circuit
  skips.
- Remote discovery: maintenance cycles and advertisement calls.
- Peer registry: state writes, `instances.json` syncs and suppressed heartbeat
  writes.
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
