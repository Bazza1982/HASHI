# HASHI Remote Peer-to-Peer Communication Upgrade Plan

## Executive Summary

This plan upgrades `Hashi Remote` from a lightweight peer discovery + hchat relay service into a standalone peer-to-peer communication layer for cross-instance HASHI messaging.

The goal is to move cross-instance communication responsibility out of core HASHI runtime logic and into `Hashi Remote`, while preserving local hchat behaviour and avoiding any requirement for cold restart. The upgraded design keeps `Hashi Remote` as a sidecar service that can be enabled or refreshed via `/remote off` + `/remote on`, or via a normal `/reboot` workflow, without coupling communication changes to the core HASHI process model.

The main architectural decision is:

- Local hchat remains local.
- Cross-instance hchat becomes a `Hashi Remote` responsibility.
- Discovery, handshake, remote agent directory, message routing, dedupe, TTL, and reply correlation all live in `remote/`.
- Core HASHI runtimes should not need to understand cross-instance protocol details.

This is cleaner than the current `HASHI1` relay-centric model because it removes central relay version-coupling and gives each instance a direct, discoverable identity such as `@hashi2`, with agents addressed as `ajiao@hashi2`.

## Actual Current State In This Repo

The current codebase already provides a usable standalone `Hashi Remote` foundation:

- `remote/main.py`
  - Starts `Hashi Remote` as an independent Uvicorn/FastAPI service.
  - Loads config from `remote/config.yaml`.
  - Starts discovery and peer registry.
- `orchestrator/flexible_agent_runtime.py`
  - `cmd_remote()` starts/stops `Hashi Remote` as a subprocess using `python -m remote --no-tls`.
  - This confirms `Hashi Remote` is already treated as a service, not core runtime.
- `remote/api/server.py`
  - Exposes `/health`, `/peers`, `/hchat`, `/terminal/exec`, and pairing endpoints.
  - Currently relays inbound remote hchat into the local Workbench API.
- `remote/peer/lan.py`
  - Advertises and discovers peers over mDNS using `_hashi._tcp.local.`
- `remote/peer/registry.py`
  - Persists discovered peer IP/port data into `instances.json`
- `remote/peer/tailscale.py`
  - Provides a second discovery backend based on Tailscale status polling.
- `remote/security/pairing.py`
  - Already supports paired clients and LAN auto-approval mode.

This means the repo already has the correct deployment boundary for the upgrade. The problem is not that `Hashi Remote` is missing; the problem is that it is still too thin and still relies on legacy hchat runtime behaviour for critical routing semantics.

## Current Behavioural Gaps

### 1. Remote discovery only identifies instances, not routable agent addresses

`LanDiscovery` and `PeerRegistry` currently discover instances and sync host/port information, but they do not exchange agent directory information. As a result:

- peers are visible as instances
- agent-level routing still depends on local files or indirect routing
- there is no authoritative remote address book for `agent@instance`

### 2. `/hchat` is only a transport pass-through, not a protocol terminator

`remote/api/server.py` currently:

- accepts a remote message
- optionally forwards exchange traffic
- converts it to text
- injects it into local `/api/chat`

That means `Hashi Remote` is not yet acting as a proper messaging service. It does not own:

- message identity
- reply correlation
- duplicate suppression
- hop limits
- route tracing
- protocol version negotiation

### 3. Cross-instance reply safety still leaks into runtime-level string parsing

The existing cross-instance flow still depends on text envelopes such as:

- `[hchat from ...]`
- `[hchat reply from ...]`

That is too fragile. Version mismatches create loops because older runtimes treat reply traffic as a fresh inbound message.

### 4. Current design still assumes central relay fallback through `HASHI1`

`tools/hchat_send.py` still uses `HASHI1` as a preferred exchange path for non-local delivery. That creates:

- central relay coupling
- protocol mismatch risk
- hard-to-debug cross-version failures
- failure amplification when the relay instance is newer than peers

### 5. Remote activation is standalone, but protocol state is not yet service-owned

The service boundary is already correct, but the communication state model is not. `Hashi Remote` should own remote message lifecycle end-to-end rather than delegating semantics back to agent runtimes.

## Design Principles For The Upgrade

1. `Hashi Remote` must remain a standalone service.
2. Local hchat must not be altered by cross-instance protocol upgrades.
3. No cold restart should be required to enable the new remote communication model.
4. Cross-instance communication must be explicit and addressable as `agent@instance`.
5. All remote routing semantics must be owned by `Hashi Remote`, not hidden in prompt text or agent instructions.
6. The new protocol must be backward-aware and safe under mixed-version LAN conditions.
7. Reply handling must be loop-safe by protocol, not by prompt wording.

## Target Architecture

### Layer split

#### Layer 1: Local HASHI core

Unchanged responsibilities:

- local agent runtime
- local Workbench `/api/chat`
- local transcript logging
- local commands and workflows

Core HASHI should only receive remote-originated messages as normal local work items and should not need to understand peer transport.

#### Layer 2: Hashi Remote sidecar

New responsibilities:

- discovery
- instance handshake
- agent directory exchange
- peer capability/version negotiation
- cross-instance message routing
- reply correlation
- dedupe / TTL / anti-loop
- route trace logging
- peer trust state

#### Layer 3: Peer-to-peer remote protocol

New protocol responsibilities:

- identify each instance as `@hashi2`, `@hashi1`, etc.
- advertise supported capabilities
- exchange active agent addresses
- deliver messages to exact remote agents
- track reply path explicitly

## Proposed Protocol Model

### Instance identity

Each `Hashi Remote` instance should advertise:

- `instance_id`
- `display_handle` such as `@hashi2`
- `remote_endpoint`
- `protocol_version`
- `capabilities`
- `workbench_port`
- `platform`
- `hashi_version`

This extends the current `PeerInfo` model rather than replacing it.

### Agent directory exchange

After peer discovery, instances should perform a handshake and exchange:

- active agent list
- agent display names
- agent status
- directory TTL / last updated timestamp

This produces a local remote directory cache such as:

- `ajiao@hashi2`
- `kasumi@hashi2`
- `arale@hashi1`
- `intel@intel`

### Handshake state machine

The protocol requires an explicit peer handshake state machine.

Minimum states:

- `discovered`
- `handshake_pending`
- `handshake_in_progress`
- `handshake_accepted`
- `handshake_rejected`
- `handshake_timed_out`
- `peer_stale`
- `rehydrate_required`

Required transitions:

1. newly discovered peer -> `handshake_pending`
2. handshake request sent -> `handshake_in_progress`
3. `handshake_accept` received -> `handshake_accepted`
4. `handshake_reject` received -> `handshake_rejected`
5. no response within timeout -> `handshake_timed_out`
6. peer metadata change -> `rehydrate_required`
7. peer absent beyond stale threshold -> `peer_stale`
8. stale peer rediscovered -> `handshake_pending`

Default handshake timing values for v1:

- `handshake_timeout_seconds = 8`
- `handshake_retry_limit = 3`
- `handshake_retry_backoff_seconds = [2, 5, 10]`
- `peer_stale_after_seconds = 30`
- `rehydrate_cooldown_seconds = 5`

Re-handshake triggers must include:

- first discovery
- peer rediscovered after stale/offline period
- peer advertises changed `protocol_version`
- peer advertises changed `capabilities`
- peer advertises changed `hashi_version`
- local sidecar restart when stored peer state predates the current discovery epoch

### Message envelope

All peer-to-peer traffic should use a structured message envelope, not text-only semantics.

Minimum fields:

- `protocol_version`
- `message_type`
- `message_id`
- `conversation_id`
- `in_reply_to`
- `from_instance`
- `from_agent`
- `to_instance`
- `to_agent`
- `created_at`
- `hop_count`
- `ttl`
- `route_trace`
- `body`

TTL must not be sender-trusted. Receiver must enforce a service-side ceiling.

Supported `message_type` values:

- `discover_announce`
- `discover_snapshot`
- `handshake_request`
- `handshake_accept`
- `handshake_reject`
- `agent_directory_snapshot`
- `agent_message`
- `agent_reply`
- `system_notice`
- `ack`
- `error`

### Error handling model

The protocol must define explicit error paths, especially for target resolution and local delivery failures.

Required `error` emission cases:

- target agent does not exist on receiving instance
- target agent exists in directory cache but is currently offline/unavailable
- local `/api/chat` enqueue fails
- reply correlation is missing for an inbound `agent_reply`
- handshake rejected or incompatible
- hard timeout reached before terminal completion

Minimum `error.body` fields:

- `code`
- `message`
- `retryable`
- `failed_message_id`
- `conversation_id`
- `from_instance`
- `from_agent`
- `to_instance`
- `to_agent`
- optional `details`

Recommended error codes:

- `target_agent_not_found`
- `target_agent_unavailable`
- `local_enqueue_failed`
- `reply_correlation_missing`
- `handshake_incompatible`
- `handshake_rejected`
- `reply_timeout`
- `delivery_expired`

Receiver handling rules for `error`:

- append to remote conversation history
- mark correlated outbound state as failed
- if `retryable = true`, allow retry policy or manual resend
- if original local sending agent is available, inject a neutral local system error notice
- if local sending agent is unavailable, persist to remote inbox / pending delivery store

### Reply model

This is the most important change.

Cross-instance replies should no longer depend on runtime-level hchat auto-routing. Instead:

1. Remote receives `agent_message`.
2. Remote injects a local work item into `/api/chat`.
3. Remote tracks the returned `request_id`.
4. Remote observes the local transcript or request completion.
5. Remote packages the assistant output as `agent_reply`.
6. Remote sends the reply directly back to the originating remote peer using `in_reply_to`.

### `agent_reply` receiver behaviour

The receiver side must define what happens when an `agent_reply` arrives. This cannot be left implicit.

Required receiver steps:

1. validate `in_reply_to` against an existing outbound correlation record
2. mark the original outbound message as terminal success
3. append the reply to remote conversation history for that `conversation_id`
4. deliver the reply into the original local context as a neutral system reply
5. do not auto-forward it to another remote peer

Default local delivery target for `agent_reply`:

- the original local sending agent identified by the outbound correlation record

Recommended local injected format:

- `System exchange reply from lily@HASHI1:\n...`

If the original local sending agent is no longer available:

- persist the reply in a remote inbox / pending conversation store
- mark delivery as `local_target_unavailable`
- allow later resume or manual recovery

This means `agent_reply` is not "just logged". It should normally be delivered back into the originating local agent context, but in a neutral form that does not trigger automatic remote bounce.

### Multi-turn conversation rule

The protocol must support multi-turn remote conversations.

Important distinction:

- receiving an `agent_reply` must not be blindly auto-converted into a new outbound `agent_message`
- but a local agent is allowed to produce a follow-up turn after consuming that reply

So the correct rule is:

- a received `agent_reply` may not be mechanically re-emitted as a new remote message by the transport layer itself
- any follow-up remote turn must be created as a fresh `agent_message` with a new `message_id` and the same `conversation_id`

This preserves multi-turn dialogue while still preventing loop-by-transport.

### Reply collection boundary definition

The sidecar must define explicit completion states for a locally injected remote request. Transcript polling alone is not enough unless the sidecar also owns a completion model.

Required terminal states:

- `completed`
- `failed`
- `rejected`
- `timed_out`
- `abandoned_after_restart`

Required intermediate states:

- `queued`
- `matched_user_prompt`
- `assistant_started`
- `assistant_streaming`
- `awaiting_settle_window`

Recommended reply collection rules:

1. Sidecar injects the remote message and stores returned `request_id`.
2. Sidecar polls transcript using the current offset and waits until the exact injected user prompt is observed.
3. Once the matching user prompt is observed, the first assistant message after it marks `assistant_started`.
4. If additional assistant entries continue arriving within the settle window, sidecar remains in `assistant_streaming`.
5. When no new assistant transcript entry appears for `settle_window_seconds`, sidecar marks `completed` and emits one merged `agent_reply`.
6. If an explicit assistant error entry or runtime error marker is observed, sidecar marks `failed`.
7. If the assistant text is a refusal or a system policy block, sidecar marks `rejected`.
8. If no assistant output reaches terminal state before timeout, sidecar marks `timed_out`.

Default timing values for v1:

- `poll_interval_seconds = 0.5`
- `reply_soft_timeout_seconds = 45`
- `reply_hard_timeout_seconds = 180`
- `settle_window_seconds = 2.0`
- `dedupe_success_ttl_seconds = 600`
- `dedupe_retry_ttl_seconds = 180`
- `directory_ttl_seconds = 90`
- `max_allowed_message_ttl = 8`

These values should live in remote config, not hardcoded across modules.

This makes `Hashi Remote` the owner of the remote reply lifecycle.

## Why This Avoids Core HASHI Changes

The repo already exposes what the remote sidecar needs:

- `/api/chat` returns `request_id`
- transcript polling exists via `/api/transcript/{name}/poll`
- transcript history is already persisted by agent runtime
- `cmd_remote()` already starts/stops the sidecar independently

This means the remote service can:

- enqueue work through existing APIs
- monitor completion through existing transcript endpoints
- send protocol-level replies itself

The critical compatibility trick is:

- do not inject cross-instance traffic as raw `[hchat from ...]` into the local runtime
- instead inject a neutral local prompt such as:
  - `System exchange message from ajiao@HASHI2:\n...`

That prevents legacy `_hchat_route_reply()` logic from treating the message as a fresh hchat chain while still allowing the local agent to respond normally.

This keeps the protocol upgrade inside `remote/` and avoids deeper core-service surgery.

## Required Remote Service Upgrades

### Workstream 1: Discovery becomes discoverable identity exchange

Upgrade `remote/peer/lan.py` and `remote/peer/base.py` so advertisements include:

- protocol version
- display handle
- capabilities
- directory sync support flag

Add a post-discovery handshake phase instead of treating mDNS discovery as complete truth.

### Workstream 2: Peer registry becomes peer state store

Upgrade `remote/peer/registry.py` to store:

- peer protocol version
- peer capabilities
- trust/pairing state
- agent directory snapshot
- last successful handshake time
- peer health status

It must also merge multiple discovery backends into one canonical peer record.

Required merge keys:

- `instance_id` is primary identity
- if `instance_id` is missing, fallback identity may use signed handshake identity only after trust

Required merge behaviour:

- mDNS and Tailscale discoveries for the same `instance_id` collapse into one peer record
- peer record stores per-backend observations instead of creating duplicate peers
- route selection chooses one active preferred endpoint while retaining alternates

Default endpoint preference rule for v1:

- prefer `lan` when peer is on same local network and healthy
- otherwise prefer `tailscale`
- if preferred endpoint fails health checks repeatedly, demote and fail over to the alternate endpoint

The registry therefore needs two layers:

- discovery observations per backend
- canonical resolved peer route

This data should live in remote-owned state files, not only in `instances.json`.

`instances.json` can remain a compatibility surface, but it should no longer be the canonical remote routing model.

### Workstream 3: Add remote agent directory sync

Add new remote endpoints such as:

- `POST /protocol/handshake`
- `GET /protocol/agents`
- `POST /protocol/agents/snapshot`

These should exchange active agent addresses automatically after trust is established.

Handshake work must also include:

- `handshake_reject` response support
- bounded retry policy
- automatic re-handshake on rediscovery
- automatic re-handshake on protocol/capability/version change
- stale-peer detection and handshake refresh

### Workstream 4: Introduce protocol router / exchange engine

Add a new remote router module responsible for:

- validating envelopes
- dedupe
- TTL enforcement
- route tracing
- endpoint selection across merged peer backends
- local delivery
- reply dispatch
- reply reception and local reinjection
- structured error emission and error reception
- error handling

This should be a distinct service object under `remote/`, not spread across `api/server.py`.

### Workstream 5: Stop using HASHI1 as required relay

`tools/hchat_send.py` should evolve toward:

- local agent without suffix -> local Workbench path
- explicit `agent@instance` -> resolve via remote peer directory
- direct remote delivery first
- central relay optional, not primary

If relay remains, it should be a backend capability, not a mandatory topology assumption.

### Workstream 6: Add loop prevention at protocol level

Every remote envelope must enforce:

- `message_id` dedupe store with delivery states
- `ttl` and `hop_count`
- `route_trace`
- reject self-bounce
- reject only duplicate deliveries that already reached terminal success
- reject route replay before appending local instance when local instance already appears once in `route_trace`

TTL rules:

- sender may request a `ttl`, but receiver must clamp it to `max_allowed_message_ttl`
- if requested `ttl` is missing, use service default
- if requested `ttl <= 0`, reject immediately
- `max_allowed_message_ttl` is service-owned config, not peer-controlled

The dedupe store must distinguish at least:

- `received_not_delivered`
- `delivery_in_progress`
- `delivered_to_local_queue`
- `reply_sent`
- `failed`
- `expired`

Important rules:

- entry existence must not be treated as delivery success
- retransmit after network jitter must be allowed while the prior attempt is incomplete or failed
- only terminal-success states such as `reply_sent` are safe to hard-reject as duplicates
- dedupe data must be garbage-collected with separate TTLs for success and retryable failures

This is mandatory.

### Workstream 7: Add remote reply correlator

Add a remote-owned correlation store:

- outbound remote message -> local `request_id`
- request completion -> outbound `agent_reply`
- reply timeout / retry policy

This should be persisted under a remote state directory so sidecar restart does not lose active message tracking.

The correlation store should persist at minimum:

- `message_id`
- `conversation_id`
- `from_instance`
- `from_agent`
- `to_instance`
- `to_agent`
- `local_request_id`
- `transcript_path`
- `transcript_offset_at_enqueue`
- `matched_user_offset`
- `last_seen_offset`
- `state`
- `created_at`
- `updated_at`
- `reply_soft_deadline`
- `reply_hard_deadline`
- `attempt_count`

### Workstream 8: Add remote-only activation / restart path

Because `Hashi Remote` is already standalone, the rollout path should be:

- code deploy
- `/remote off`
- `/remote on`

or, if preferred operationally:

- `/reboot`

No cold restart of the whole HASHI environment should be required.

## Recommended Endpoint Additions

These should be added inside `remote/api/server.py` or a split router module:

- `POST /protocol/announce`
- `POST /protocol/handshake`
- `GET /protocol/directory`
- `POST /protocol/message`
- `POST /protocol/reply`
- `POST /protocol/ack`
- `GET /protocol/status`

Keep legacy `/hchat` for compatibility, but treat it as a legacy ingress path.

## Backward Compatibility Strategy

This upgrade must be explicit about compatibility.

### Compatibility mode

When the peer does not support protocol handshake:

- keep peer visible as a legacy peer
- do not enable advanced reply correlation
- allow one-way message delivery only if safe
- mark peer as `legacy_transport_only`

### Safety downgrade

If a peer only supports legacy `/hchat`:

- inject the message locally as a neutral system exchange prompt
- do not rely on runtime auto-reply
- only attempt reply if remote can correlate it safely

### Mixed-version LAN rule

New remote protocol should never assume the remote peer can understand structured reply semantics unless handshake confirms support.

## Phased Implementation Plan

### Phase 1: Remote state and protocol scaffolding

- Add protocol version and capability metadata to `PeerInfo`
- Add remote-owned peer state file
- Add handshake endpoints
- Add remote directory state
- Add handshake state machine, timeout values, retry policy, and rediscovery triggers
- Add peer merge policy for `lan` + `tailscale` observations

### Phase 2: Agent directory exchange

- Export active agents from local `agents.json`
- Sync agent snapshots after handshake
- Expose merged local + remote directory in remote status APIs

### Phase 3: Protocol router

- Implement structured message envelope
- Add dedupe and TTL checks
- Add route trace recording
- Add local request correlation store

### Phase 4: Local delivery without core coupling

- Deliver remote messages to local `/api/chat`
- Use neutral prompt format rather than raw `[hchat from ...]`
- Track `request_id`
- Observe transcript completion
- Add explicit reply collection state machine and settle window rules
- Add target-agent-not-found / target-agent-unavailable error path

### Phase 5: Reply dispatch

- Send `agent_reply` directly from `Hashi Remote`
- Stop depending on runtime hchat auto-route for remote traffic
- Persist inflight reply correlation and resume after sidecar restart
- Define `agent_reply` receiver reinjection rules for the originating local agent

### Phase 6: Compatibility and migration

- Keep legacy `/hchat`
- Mark peers by supported protocol level
- Gradually move `tools/hchat_send.py` toward remote-directory-first routing

## Acceptance Criteria

The upgrade is complete only when the following are true:

- A running `Hashi Remote` instance advertises itself as a discoverable identity such as `@hashi2`
- Peers on the same LAN automatically discover one another
- After handshake, each peer can see remote agent addresses such as `ajiao@hashi2`
- Cross-instance messages route directly peer-to-peer without requiring `HASHI1` central relay
- Return address is explicit as `agent@instance`
- Duplicate inbound remote messages do not create duplicate agent work
- Reply traffic does not loop
- Reply collection can distinguish `completed`, `failed`, `rejected`, and `timed_out`
- Sidecar restart can recover inflight remote requests and continue transcript catch-up
- Peer rediscovery after offline/online cycle automatically triggers re-handshake
- Peer protocol/capability/version change automatically triggers re-handshake
- `agent_reply` is delivered back to the originating local agent or persisted in inbox if the local target is unavailable
- Multi-turn remote conversations remain possible through fresh `agent_message` turns sharing a `conversation_id`
- Targeting an offline or missing agent results in structured `error` response rather than silent drop
- Discovering the same peer over both mDNS and Tailscale results in one canonical peer record with deterministic endpoint preference
- Receiver enforces `max_allowed_message_ttl` even if sender requests a larger value
- A mixed-version peer does not destabilize local hchat
- Local hchat behaviour is unchanged
- Enabling the upgraded remote service requires only sidecar restart or `/reboot`, not cold restart

## Risks And Trade-Offs

### Risk: Transcript-based reply correlation is less elegant than runtime-native transport hooks

True, but it respects the current constraint of keeping communication changes outside core HASHI.

Transcript correlation also has concrete ambiguity risk:

- multi-part assistant output
- partial output before backend failure
- policy refusal vs transport failure
- transcript lag during restart

The design therefore requires an explicit sidecar-owned state machine and persisted offsets, not a naive "first assistant message wins" rule.

### Risk: Directory snapshots may go stale

This is manageable with:

- handshake refresh
- TTL expiry
- peer status polling

### Risk: Legacy `/hchat` path remains confusing during migration

Mitigation:

- clearly mark legacy vs protocol peers
- prefer protocol routes whenever handshake succeeds

## Immediate Next Actions

1. Add a dedicated remote router module under `remote/` for envelope validation and message lifecycle.
2. Extend `PeerInfo` and discovery payloads with protocol metadata.
3. Add handshake + directory endpoints in `remote/api/server.py`, including `handshake_reject`.
4. Add remote-owned persistent state for peers, directories, and inflight messages.
5. Add handshake state machine with timeout, retry, rediscovery, and re-handshake triggers.
6. Change remote local-delivery injection from raw `[hchat from ...]` to neutral `System exchange message from ...`.
7. Add transcript-based reply correlation inside `Hashi Remote`, including settle window, error classification, and hard/soft timeout values.
8. Add delivery-state-aware dedupe with retryable incomplete records and TTL-based cleanup.
9. Add sidecar restart catch-up logic that resumes polling from persisted transcript offsets and finalizes inflight messages safely.
10. Update `tools/hchat_send.py` to prefer explicit peer-directory routing for `agent@instance`.

## Code Anchors Used For This Plan

- `remote/main.py`
- `remote/api/server.py`
- `remote/peer/base.py`
- `remote/peer/lan.py`
- `remote/peer/registry.py`
- `remote/peer/tailscale.py`
- `remote/security/pairing.py`
- `orchestrator/flexible_agent_runtime.py` (`cmd_remote`)
- `orchestrator/workbench_api.py` (`/api/chat`, transcript polling)
- `tools/hchat_send.py`
