# HASHI Remote Protocol Spec (Draft)

## Purpose

This draft defines a peer-to-peer protocol for cross-instance HASHI messaging on top of `Hashi Remote`.

It is designed to:

- keep `Hashi Remote` standalone
- preserve local hchat integrity
- remove hard dependence on `HASHI1` as central relay
- prevent infinite reply loops by protocol design

## Identity Model

### Instance address

- Canonical instance address: `@hashi2`
- Canonical instance id: `HASHI2`

### Agent address

- Canonical agent address: `ajiao@hashi2`

Cross-instance delivery must always resolve to an explicit agent address.

## Peer Discovery

### Discovery announce

Advertised fields:

- `instance_id`
- `display_handle`
- `remote_endpoint`
- `protocol_version`
- `capabilities`
- `platform`
- `hashi_version`
- `workbench_port`

### Discovery result

Discovery only means:

- a peer exists
- a transport endpoint was found

Discovery does not mean:

- trust is established
- protocol is compatible
- agent directory is known

### Multi-backend merge rules

Discovery backends may report the same peer through:

- `lan` / mDNS
- `tailscale`

Canonical peer identity rule:

- `instance_id` is the primary merge key

Merge behaviour:

- observations from multiple backends for the same `instance_id` must merge into one canonical peer record
- canonical peer record keeps backend-specific endpoints as alternate routes
- discovery alone must not create duplicate peer entries in directory or routing tables

Default endpoint preference:

- prefer `lan` when healthy and same-network reachable
- otherwise prefer `tailscale`
- if the preferred route fails repeated health checks, fail over to the alternate route

Canonical peer record should retain:

- `instance_id`
- `preferred_endpoint`
- `alternate_endpoints`
- backend-specific health state
- last_seen per backend

## Handshake

### Handshake request

Sent after discovery to establish:

- protocol compatibility
- supported capabilities
- trust state
- remote agent directory availability

### Handshake accept

Returns:

- accepted protocol version
- peer capabilities
- pairing requirement
- agent directory sync support
- optional initial agent snapshot

### Handshake reject

Returns:

- reject reason
- supported protocol version range if known
- whether downgrade is allowed
- retry hint if temporary

### Handshake state machine

States:

- `discovered`
- `handshake_pending`
- `handshake_in_progress`
- `handshake_accepted`
- `handshake_rejected`
- `handshake_timed_out`
- `peer_stale`
- `rehydrate_required`

Default handshake timing values:

- `handshake_timeout_seconds = 8`
- `handshake_retry_limit = 3`
- `handshake_retry_backoff_seconds = [2, 5, 10]`
- `peer_stale_after_seconds = 30`
- `rehydrate_cooldown_seconds = 5`

Re-handshake triggers:

- first discovery
- peer rediscovered after stale period
- peer advertised `protocol_version` changes
- peer advertised `capabilities` changes
- peer advertised `hashi_version` changes
- local sidecar restart with stale peer state

Rediscovery rule:

- mDNS or other discovery update alone is not sufficient
- rediscovery must schedule a new handshake if the peer was previously stale or its advertised metadata changed

## Agent Directory

### Directory snapshot

Minimum fields per agent:

- `agent_name`
- `agent_address`
- `display_name`
- `is_active`
- `updated_at`

### Directory rules

- directories are cached with TTL
- remote directory is not treated as permanent truth
- local instance keeps its own authoritative local directory

## Message Envelope

```json
{
  "protocol_version": "2.0",
  "message_type": "agent_message",
  "message_id": "msg-uuid",
  "conversation_id": "conv-uuid",
  "in_reply_to": null,
  "from_instance": "HASHI2",
  "from_agent": "lin_yueru",
  "to_instance": "HASHI1",
  "to_agent": "lily",
  "created_at": "2026-04-26T14:45:00+10:00",
  "hop_count": 0,
  "ttl": 8,
  "route_trace": ["HASHI2"],
  "body": {
    "text": "Hello"
  }
}
```

TTL is sender-supplied but receiver-governed.

Default service rule:

- `max_allowed_message_ttl = 8`
- receiver clamps requested TTL to `min(requested_ttl, max_allowed_message_ttl)`
- missing TTL uses service default
- non-positive TTL is invalid

## Message Types

- `agent_message`
- `agent_reply`
- `ack`
- `error`
- `discover_announce`
- `handshake_request`
- `handshake_accept`
- `handshake_reject`
- `agent_directory_snapshot`
- `system_notice`

## Delivery Rules

### For `agent_message`

Remote receiver must:

1. validate envelope
2. normalize TTL against `max_allowed_message_ttl`
3. check delivery state for `message_id`
4. check `route_trace` before append; if local instance already appears once, reject as looped return
5. append local instance to `route_trace`
6. inject a neutral local prompt into `/api/chat`
7. store correlation: `message_id -> local request_id`

If `to_agent` does not exist locally or is unavailable, receiver must not silently drop the message.

Receiver must emit `error` with one of:

- `target_agent_not_found`
- `target_agent_unavailable`

The receiver must also persist:

- `local_request_id`
- `transcript_path`
- `transcript_offset_at_enqueue`
- `state = queued`
- `reply_soft_deadline`
- `reply_hard_deadline`

### Neutral local prompt format

Recommended format:

```text
System exchange message from lin_yueru@HASHI2:
Hello
```

Avoid raw `[hchat from ...]` as the local injected prompt for protocol traffic.

## Reply Collection State Machine

### States

- `queued`
- `matched_user_prompt`
- `assistant_started`
- `assistant_streaming`
- `awaiting_settle_window`
- `completed`
- `failed`
- `rejected`
- `timed_out`
- `abandoned_after_restart`

### Default timing values

- `poll_interval_seconds = 0.5`
- `settle_window_seconds = 2.0`
- `reply_soft_timeout_seconds = 45`
- `reply_hard_timeout_seconds = 180`

### Collection rules

1. Poll transcript from the persisted offset.
2. Do not begin reply collection until the exact injected user prompt is observed.
3. The first assistant entry after that prompt changes state to `assistant_started`.
4. Additional assistant entries inside the settle window extend the same response and move state to `assistant_streaming`.
5. When no new assistant entry arrives during the settle window, state becomes `completed`.
6. If transcript or runtime metadata indicates backend/tool failure, state becomes `failed`.
7. If the assistant output is an explicit refusal or policy block, state becomes `rejected`.
8. If hard timeout is reached before a terminal state, state becomes `timed_out`.

### Reply payload generation

- `completed` -> send `agent_reply` with merged assistant output
- `failed` -> send `error` with failure classification
- `rejected` -> send `agent_reply` with refusal classification metadata
- `timed_out` -> send `error` with timeout classification

## Reply Rules

### For `agent_reply`

Replies must be created by `Hashi Remote`, not by local runtime auto-routing.

Remote sender must:

1. wait for local request completion
2. collect assistant output
3. send `agent_reply` with `in_reply_to = original.message_id`

Reply collection must not treat the first assistant transcript line as final by default.

### `agent_reply` receiver behaviour

Receiver must:

1. validate `in_reply_to` against an outbound correlation record
2. mark the matched outbound message as terminal success
3. append the reply to conversation history for the same `conversation_id`
4. deliver a neutral local system reply to the original local sending agent

Default local reinjection target:

- the original local sending agent recorded in the outbound correlation

Default local reinjection format:

```text
System exchange reply from lily@HASHI1:
Hello back
```

Fallback when the original local agent is unavailable:

- store reply in remote inbox / pending delivery store
- mark state `local_target_unavailable`
- do not discard the reply

## Error Message Rules

`error` is a first-class protocol message, not a generic transport failure bucket.

### Required emission cases

- target agent does not exist
- target agent is offline/unavailable
- local enqueue failure
- missing reply correlation for inbound `agent_reply`
- handshake rejected or incompatible
- hard timeout before terminal reply state

### Required `error.body`

```json
{
  "code": "target_agent_unavailable",
  "message": "Target agent exists but is offline",
  "retryable": true,
  "failed_message_id": "msg-uuid",
  "conversation_id": "conv-uuid",
  "from_instance": "HASHI2",
  "from_agent": "lin_yueru",
  "to_instance": "HASHI1",
  "to_agent": "ajiao",
  "details": {}
}
```

### Receiver behaviour for `error`

Receiver must:

1. append the error to remote conversation history
2. mark the correlated outbound message as failed
3. if original local sending agent is available, inject a neutral local system error notice
4. if original local sending agent is unavailable, persist to remote inbox / pending delivery store

Recommended local injected error format:

```text
System exchange error for ajiao@HASHI2:
target_agent_unavailable - Target agent exists but is offline
```

### Anti-loop rule

Raw receipt of `agent_reply` must not be blindly re-emitted by the transport layer as a new remote message.

This rule does not forbid multi-turn conversations.

Multi-turn rule:

- follow-up dialogue is allowed
- any follow-up turn must be created as a fresh `agent_message`
- fresh turn must use a new `message_id`
- fresh turn should retain the same `conversation_id` where appropriate

## Dedupe And Loop Prevention

Each remote instance must maintain a recent-message store keyed by `message_id`.

The store must not be boolean. It must track delivery state.

Minimum dedupe states:

- `received_not_delivered`
- `delivery_in_progress`
- `delivered_to_local_queue`
- `reply_sent`
- `failed`
- `expired`

Reject message when:

- normalized `ttl <= 0`
- `hop_count >= ttl`
- local instance already appears once in `route_trace` before local append
- `(message_id, to_agent)` is already in a terminal success state such as `reply_sent`

Retry rules:

- if a duplicate arrives while state is `delivery_in_progress`, receiver may return `ack_in_progress`
- if a duplicate arrives while state is `failed` or non-terminal and retry TTL has not expired, receiver may re-attempt delivery or resume correlation
- receiver must not permanently drop a retransmit only because the `message_id` was seen before

Retention rules:

- `dedupe_success_ttl_seconds = 600`
- `dedupe_retry_ttl_seconds = 180`
- expired entries must be garbage-collected periodically
- dedupe state for non-terminal inflight messages must survive sidecar restart

## Restart Catch-Up Rules

Because `Hashi Remote` is a standalone sidecar, restart recovery is mandatory.

On startup, the sidecar must reload inflight correlations and for each non-terminal record:

1. reopen transcript from persisted `transcript_offset_at_enqueue`
2. scan forward to the last persisted `last_seen_offset`
3. continue polling until a terminal state is reached or hard timeout expires
4. if transcript file rotated or disappeared, mark `abandoned_after_restart`
5. never emit a second `agent_reply` for the same `message_id`

If a terminal reply was already emitted before crash, dedupe cache must suppress replay after restart.

## Backward Compatibility

### Legacy peer

If handshake fails but `/hchat` exists:

- mark peer as `legacy_transport_only`
- do not assume safe reply semantics
- one-way delivery may be allowed

### Structured reply requirement

Structured `agent_reply` must only be used after handshake confirms protocol support.

## Activation Constraint

This protocol is intended to ship entirely inside `Hashi Remote`.

Operational goal:

- enable with `/remote off` then `/remote on`
- or during normal `/reboot`
- never require cold restart of the whole HASHI system
