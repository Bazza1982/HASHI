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

DNS-SD encodes each TXT character-string within the 255-byte wire limit. HASHI
discovery metadata schema 2 additionally caps the aggregate TXT payload at
8 KiB, each scalar hint at 160 UTF-8 bytes, and each address-hint collection at
four entries. Values that need fragmentation use an explicit manifest
(`<field>_meta`) containing schema version, fragment count, UTF-8 byte length,
and SHA-256, followed by zero-based `<field>_0`, `<field>_1`, ... fragments.
Readers accept fragments in any order but reject gaps, duplicate numeric
indices, unknown versions, impossible lengths, checksum mismatches, oversized
individual records, and aggregate payloads over budget.

Schema 2 advertisements carry bounded identity and route hints plus fixed-size
`identity_digest`, `capabilities_digest`, and `address_candidates_digest`
values. Complete display metadata, capabilities, address candidates, Remote
supervisor facts, and the Agent directory come only from the mutually
authenticated handshake. A stable digest preserves the last authenticated
projection; a changed digest immediately changes the peer to
`rehydrate_required` and removes the old capability/directory proof. Legacy
bounded scalar and contiguous capability records remain readable as routing
hints during mixed-version adoption.

### Discovery result

Discovery only means:

- a peer exists
- a transport endpoint was found

Discovery does not mean:

- trust is established
- protocol is compatible
- agent directory is known

### Discovery readiness and recovery

Every configured backend reports `advertising`, `browsing`, `readiness`, peer
count, last classified error, attempt/success timestamps, retry count, and next
retry time. Partial startup is cleaned up as one unit. Startup and advertisement
update failures use bounded exponential backoff; an unsuccessful update never
advances the advertised snapshot revision.

The public health/status projection distinguishes:

- `ready_empty`: advertising and browsing work, but no peer is currently seen;
- `ready`: discovery works and at least one peer has a trusted handshake;
- `starting`: a configured backend has not completed initialization;
- `degraded`: discovery failed, or visible peers have no accepted trust result;
- `disabled`: no discovery backend is configured.

Periodic trust revalidation keeps the most recent accepted handshake visible
while the replacement request is in flight. The public registry changes an
accepted peer only after a definitive reject, timeout, stale transition, or
metadata/credential invalidation; a first-time handshake still reports
`handshake_in_progress`. This prevents a healthy trusted route from briefly
appearing untrusted merely because its proof is being refreshed.

Backend API startup issues are historical observations, not permanent current
health. While a Remote lifecycle issue is latched, the health projection may
perform a throttled, read-only ownership and `/health` inspection. A currently
ready (or intentionally disabled) Remote clears only the matching Remote issue;
Agent and connector failures and unrelated startup issues remain intact. The
inspection never starts, stops, or replaces Remote.

Static `instances.json` discovery is an observable bootstrap fallback. A peer
reached through it is marked as such and must not be reported as proof that
mDNS discovery succeeded.

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
- every snapshot identifies its backend even when it contains zero peers; an
  empty successful snapshot retracts only that backend's observations, so a
  departed mDNS peer is removed without discarding a surviving static or
  Tailscale route

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

### Shared-token authentication

Protocol trust requires `hashi-shared-hmac-v1` unless a deployment explicitly
enables legacy compatibility mode. Discovery alone does not establish trust.

An absent `secrets.json`, or an existing valid document with no
`hashi_remote_shared_token`, may intentionally start Remote in discovery-only
mode. An existing secrets file that is unreadable, invalid UTF-8, invalid JSON,
or not a JSON object is a configuration failure and must stop startup with an
actionable diagnostic. It must not be reported as an unconfigured token or
silently weaken an intended authenticated deployment.

On Windows, the Remote supervisor installer binds the protected secrets-file
ACL to the intended scheduled-task principal before registration or start. It
uses the invoking interactive identity unless an operator supplies
`-TaskUserId`; an installer or controller running as `SYSTEM`, `LOCAL SERVICE`,
or `NETWORK SERVICE` must name the intended Limited user explicitly. The
private ACL contains only the setup writer, that runtime SID, `SYSTEM`, and
Administrators; it never grants `Users` or `Everyone` access. Cross-account
configuration publication passes the same runtime SID to the shared JSON
writer so the initial file is born readable, rather than relying only on a
later supervisor repair.

Handshake and protocol message requests include:

```json
{
  "auth_scheme": "hashi-shared-hmac-v1",
  "timestamp": 1778579000,
  "nonce": "random-128-bit-value",
  "auth_digest": "hex-hmac-sha256"
}
```

The HMAC input is newline-joined in this exact order:

```text
METHOD
PATH
FROM_INSTANCE
TIMESTAMP
NONCE
CANONICAL_PAYLOAD_HASH
```

Field rules:

- `METHOD` is uppercase HTTP method, for example `POST`.
- `PATH` is the request path only, for example `/protocol/handshake`.
- `FROM_INSTANCE` is uppercase canonical instance id.
- `TIMESTAMP` is a Unix timestamp in seconds.
- `NONCE` is a high-entropy one-time value generated by the sender.
- `CANONICAL_PAYLOAD_HASH` is the SHA256 hex digest of the exact HTTP request
  body bytes received on the wire.

Implementations must not compute `CANONICAL_PAYLOAD_HASH` from ad hoc
`json.dumps()` output or from a partially selected set of top-level fields. The
request body bytes are the transport-level canonical form. This keeps HASHI1,
HASHI2, HASHI9, INTEL, and future mixed-language clients interoperable.

Receiver validation:

- shared token is configured locally;
- timestamp is inside the fixed `±300s` protocol window;
- nonce has not been used recently by this Remote instance;
- HMAC digest matches;
- `from_instance` is not the local instance id.

Nonce storage:

- store accepted nonces in an in-memory per-Remote-instance TTL set;
- retain nonces for `600s`, which is `2 * timestamp_window`;
- isolate nonce stores per instance/process, so same-host HASHI1 and HASHI2 do
  not share nonce state;
- Remote restart clears the nonce store. This is an accepted Phase 1 tradeoff
  because the timestamp window bounds replay exposure.

Unauthenticated or invalid handshakes return:

```json
{
  "status": "handshake_reject",
  "reason": "auth_required"
}
```

or:

```json
{
  "status": "handshake_reject",
  "reason": "auth_failed"
}
```

`GET /health` may remain public with redacted metadata. Unauthenticated
`GET /peers` must return only aggregate count information, not peer entries.
Full peer lists require successful shared-token authentication. The legacy
`GET /protocol/agents` compatibility endpoint likewise requires shared-token
HMAC authentication (or an actual loopback request); discovery alone never
grants an Agent-directory snapshot.

### Handshake accept

Returns:

- accepted protocol version
- peer capabilities
- pairing requirement
- agent directory sync support
- optional initial agent snapshot

When a shared token is configured, a successful `handshake_accept` also carries
`hashi-shared-response-v1` proof. The proof HMAC binds the canonical JSON
response to the request nonce. A client must reject an absent, forged, or
nonce-mismatched proof, so request authentication cannot be turned into a
one-sided trust decision by a forged HTTP success response.

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
- local shared-token revision is added, rotated, or removed
- an advertised identity, capability, address, or directory digest changes

The Remote process watches the authoritative environment/file credential
boundary while it is running. A valid token revision atomically becomes the
in-process credential used by both request authentication and protected API
operations, clears cached trusted peer metadata, and schedules all peers for a
new handshake. A malformed or unreadable revision is classified in status but
does not silently replace a working in-memory credential. Health, logs, mDNS,
and directory views expose only configuration state/generation, never token
bytes or a reusable credential digest.

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
- `abandoned_after_restart`

### Default timing values

- `poll_interval_seconds = 0.5`
- `settle_window_seconds = 2.0`

Reply collection has no elapsed-time deadline. It remains correlated until an
authoritative completion/failure, an explicit stop, or process restart
reconciliation.

### Collection rules

1. Poll transcript from the persisted offset.
2. Do not begin reply collection until the exact injected user prompt is observed.
3. The first assistant entry after that prompt changes state to `assistant_started`.
4. Additional assistant entries inside the settle window extend the same response and move state to `assistant_streaming`.
5. When no new assistant entry arrives during the settle window, state becomes `completed`.
6. If transcript or runtime metadata indicates backend/tool failure, state becomes `failed`.
7. If the assistant output is an explicit refusal or policy block, state becomes `rejected`.

### Reply payload generation

- `completed` -> send `agent_reply` with merged assistant output
- `failed` -> send `error` with failure classification
- `rejected` -> send `agent_reply` with refusal classification metadata

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
3. continue polling until a terminal state or an explicit operator stop is reached
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

## Implementation and verification receipt — 2026-09-13

- Owner/layer: PAO Remote Function plus the Windows lifecycle adapter; no Core
  source changed. Implementation is on `fix/nightly-20260913-open-items`, based
  on shared main `54ddadf6`.
- Focused Remote/Move/SessionStore/Frontend regression passed 398 tests with 13
  platform/live skips. The repository Core gate passed 667 tests with 1 skip;
  protected-Core, Python compilation, JSON, PowerShell parsing, Ruff for the
  changed scope, whitespace, and FYI-size checks passed.
- An isolated production-path canary used the HASHI3 Windows and HASHI2 WSL
  Python environments, randomized DNS-SD service identity, empty peer state,
  and no `instances.json`. The real advertiser, browser, status API, mutual-HMAC
  handshake, directory hydration, malformed-token retention, token rotation,
  trust invalidation, and bidirectional re-handshake passed in 47.86 seconds.
- The canary started only disposable processes and removed them. Formal HASHI2
  and HASHI3 Remote PIDs/start times were unchanged; HASHI1 and HASHI4 were not
  touched. No formal runtime has adopted this branch yet.
- Static seeds remain an observable fallback and were not removed. Their removal
  is a separate post-adoption decision after the same zero-seed result is
  confirmed on the intended formal deployment topology.
