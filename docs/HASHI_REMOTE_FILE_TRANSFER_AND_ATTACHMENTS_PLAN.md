# HASHI Remote File Transfer and Message Attachments Plan

Status: planned  
Scope: Hashi Remote cross-instance transport  
Last updated: 2026-05-14

## Why this plan exists

Current Hashi Remote behavior is split:

- trusted protocol messaging (`/protocol/handshake`, `/protocol/message`) uses
  shared-token HMAC
- file transfer (`/files/push`, `/files/stat`) still uses bearer token or LAN mode
- legacy `/hchat` also still uses bearer token or LAN mode

This creates an inconsistent trust model for cross-instance operations:

- cross-instance agent messaging can succeed in shared-token mode
- direct file transfer between HASHI instances can fail on the same link
- passing `hashi_remote_shared_token` to the file transfer CLI as `--token`
  fails because that CLI currently treats it as a bearer token, not an HMAC secret

The result is a product gap: Hashi Remote is documented as supporting direct
cross-PC file transfer between HASHI instances, but a shared-token-only
deployment does not currently provide that path end-to-end.

This plan fixes that gap and also defines a safe path for message attachments.

## Goals

1. Support direct file transfer between HASHI instances on LAN using the same
   shared-token HMAC trust model as protocol messaging.
2. Preserve backward compatibility for existing bearer-token and LAN-mode flows
   where practical.
3. Add message attachments without turning `/protocol/message` into a generic
   bulk file transport.
4. Guarantee "arrive together" semantics for messages with attachments.
5. Keep old peers interoperable through capability detection and downgrade paths.

## Non-goals

1. Replace all remote auth with one mechanism in a single step.
2. Allow arbitrarily large binary payloads to travel inside normal message bodies.
3. Solve internet-scale resumable transfer, chunk retry, or CDN-style distribution.
4. Redesign local Workbench rendering in this phase beyond basic attachment display.
5. Change the auth contract of legacy `/hchat` in Phase 1.
6. Break plain chat compatibility with current HASHI Remote peers that only support
   existing `/protocol/message` or legacy `/hchat` behavior.

## Current state summary

### Works today

- `/protocol/handshake` and `/protocol/message` accept shared-token HMAC.
- `remote.protocol_manager` signs outbound protocol requests with
  `hashi-shared-hmac-v1`.
- file transfer writes are atomic and verify `sha256` when provided.

### Does not work today

- `/files/push` and `/files/stat` do not accept shared-token HMAC.
- `tools/remote_file_transfer.py` only sends `Authorization: Bearer ...`.
- `/hchat` is still bearer/LAN-gated, so some legacy fallback routes also fail in
  shared-token-only deployments.

### Documentation mismatch

Current docs imply that file transfer belongs to trusted shared-token mode, but
the dedicated file transfer doc still describes bearer/LAN auth. The
implementation matches the narrower doc, not the higher-level product promise.

## Design principles

1. One trust fabric for inter-instance operations.
   Shared-token HMAC should protect protocol-owned instance-to-instance traffic.

2. Message transport is not bulk storage transport.
   File content should move through an attachment/file path, while the message
   carries only structured metadata plus user-visible text.

3. Arrival must be transactional from the user point of view.
   A message with attachments should not appear if its declared attachments were
   not successfully stored and verified.

4. Backward compatibility matters.
   Existing bearer-token and LAN-mode workflows should keep working during the
   migration window unless explicitly retired.

## Phase 1: Shared-token file transfer

### Objective

Make direct file transfer between HASHI instances work under shared-token HMAC.

### Server changes

Add shared-token HMAC verification support to:

- `POST /files/push`
- `GET /files/stat`

Recommended auth order:

1. shared-token HMAC if Hashi protocol auth headers are present
2. loopback shortcut where already explicitly allowed
3. existing bearer-token verification
4. LAN-mode auto-auth where enabled

This preserves old clients while allowing instance-to-instance trust to use the
same model as `/protocol/message`.

Implementation note:

- do not keep `Depends(verify_token)` as the only gate on file endpoints
- add `request: Request` to the file handlers
- read `body_bytes = await request.body()` in the handler
- call `try_authenticate_request(request, body_bytes=body_bytes)` directly
- keep existing bearer/LAN behavior by preserving the current auth order inside
  `try_authenticate_request()`

Reason:

- `verify_token` only extracts bearer auth
- HMAC headers will never reach file endpoints unless the handler explicitly
  routes the request through `try_authenticate_request()`
- for `GET /files/stat`, use `b""` as the canonical body input

### Client changes

Upgrade `tools/remote_file_transfer.py` to support HMAC signing.

Recommended CLI shape:

- `--shared-token <secret>` or env `HASHI_REMOTE_SHARED_TOKEN`
- keep `--token` / `HASHI_REMOTE_TOKEN` for legacy bearer mode
- `--from-instance <id>` or env `HASHI_INSTANCE_ID` for HMAC sender identity
- auto-select HMAC when shared token is configured and no explicit bearer token
  is requested

If `--from-instance` is absent, the CLI may read the local HASHI instance id from
existing config, but the plan should treat a stable sender identity as required
for auditability.

### Capability

Advertise a new protocol capability:

- `file_transfer_hmac_v1`

Meaning:

- this peer accepts shared-token HMAC on `/files/push` and `/files/stat`
- this flag means the server-side file endpoint changes are already deployed, not
  merely that a client knows how to attempt HMAC

### Acceptance criteria

1. Two HASHI instances on LAN can push and stat files with shared-token HMAC only.
2. Old bearer-token clients still work where bearer auth is configured.
3. Wrong shared token is rejected cleanly without ambiguous "expired token" wording.
4. File transfer docs and top-level docs agree on the supported auth model.

## Phase 2: Message attachments

### Objective

Support a message arriving together with one or more transferred files.

### Recommended transport model

Recommended v1: transactional two-step flow.

Do not place raw file bytes inside ordinary `/protocol/message`.

Instead, use one of these patterns:

1. combined endpoint: `POST /protocol/message-with-attachments`
2. transactional two-step flow:
   - upload attachment(s)
   - commit message referencing uploaded attachment manifest

Recommendation: use the transactional two-step flow first, then consider a
combined endpoint later only if it is implemented as streaming multipart rather
than base64-in-JSON.

### Why the transactional two-step flow is preferred for v1

It still gives a clear success contract without the memory pressure of a large
base64 JSON body:

- upload attachments into a managed pending area
- verify digest and size before commit
- commit the message only after every attachment is present
- if commit fails, roll back or quarantine the pending files

This reuses the existing file push path more naturally and avoids a large
single-request payload.

Combined endpoint note:

- a future combined endpoint is acceptable only if it uses streaming multipart
  or another streamed transport
- do not ship a base64-in-JSON combined endpoint with the current attachment
  limits

### Proposed payload shape

The commit request should include:

- normal protocol message envelope:
  - `message_id`
  - `conversation_id`
  - `from_instance`
  - `from_agent`
  - `to_instance`
  - `to_agent`
  - `ttl`
  - `route_trace`
  - `body`
- attachment list:
  - `attachment_id`
  - `filename`
  - `mime_type`
  - `size_bytes`
  - `sha256`
  - `pending_upload_id` or equivalent server-issued handle
  - optional `caption`

The receiver should resolve pending uploads first, then inject a normalized
manifest into the delivered message body.

### Delivered message shape

The local runtime should receive a message body that contains:

- user-facing text
- structured attachment manifest
- local stored path or managed attachment reference

Suggested normalized attachment fields after receipt:

- `attachment_id`
- `filename`
- `mime_type`
- `size_bytes`
- `sha256`
- `stored_path`
- `received_at`

### Storage model

Store received attachments under a managed per-instance spool, for example:

```text
~/.hashi-remote/attachments/<message_id>/<filename>
```

Benefits:

- avoids arbitrary destination-path risks for chat attachments
- simplifies cleanup
- keeps message attachments distinct from generic file push

Plain `/files/push` should continue supporting explicit destination paths because
it is a different operation.

Pending attachment state should be tracked explicitly, for example:

```text
pending_attachments[message_id] -> [attachment_id, spool_path, sha256, size]
```

The commit phase flips pending entries into delivered state or rolls them back.

## Size limits

### Standalone file transfer

Keep current large-transfer path:

- hard limit: `256 MiB`

This remains suitable for release bundles, EXP packs, and artifact movement.

### Message attachments

Recommended default limits:

- max `16 MiB` per attachment
- max `32 MiB` total per message
- max `4` attachments per message

Why:

- prevents chat delivery from becoming a bulk-transfer path
- avoids excessive memory pressure in interactive message delivery
- keeps end-to-end latency reasonable for interactive agent messaging

These limits can be config-driven later if needed.

## Transaction semantics

### Required behavior

"Arrive together" means:

1. if any attachment fails validation or storage, the message is not delivered
2. if message enqueue fails after storage, the newly written attachment files are:
   - removed, or
   - moved into a quarantine/orphan area with audit records

### Recommended implementation

For each attachment:

1. upload into a pending spool path
2. size-check
3. verify digest
4. atomically mark the pending upload as ready

After all attachments succeed:

5. commit the message with the list of ready pending uploads
6. enqueue the local message with normalized manifest

If enqueue fails:

7. remove or quarantine those files
8. return a structured failure response

Crash recovery:

- startup should sweep the pending/quarantine area
- orphaned attachment directories without a committed message record should be
  cleaned or moved to quarantine with an audit note

## Capability negotiation

Add protocol capability:

- `message_attachments_v1`

Meaning:

- peer accepts combined message+attachment protocol
- peer will return structured attachment metadata on delivery

Downgrade behavior:

- if target lacks `message_attachments_v1`, sender may:
  - fall back to standalone file transfer plus plain text message, or
  - reject the operation explicitly with a clear reason

Recommendation:

- reject by default unless the caller explicitly asks for fallback

That avoids silently breaking "arrive together" semantics.

Plain chat compatibility rule:

- if target lacks `message_attachments_v1`, ordinary `/protocol/message` chat
  must still work unchanged
- attachment send should fail closed or explicitly downgrade only when the caller
  opts in

## Security model

### For file transfer endpoints

- accept shared-token HMAC as first-class inter-instance auth
- optionally keep bearer/LAN paths for backward compatibility
- audit caller identity as the authenticated instance id when HMAC is used
- document that HMAC mode requires a stable `from_instance` value
- for large/slow transfers, evaluate whether file endpoints should use a wider
  timestamp window than the default 300 seconds

### For attachments

- only accept attachments over authenticated protocol routes
- do not allow arbitrary attachment destination paths
- enforce strict spool directories for message attachments
- verify `sha256` before making files visible to the runtime

### Error messages

Avoid misleading auth errors such as:

- "Invalid or expired token"

when the failure is actually:

- wrong auth mode
- missing HMAC headers
- bad shared-token signature

Return mode-specific auth failures where possible.

## Runtime and UI integration

### Runtime

The local runtime should receive normalized attachment metadata as part of the
message body. It should not need to understand raw upload mechanics.

### Workbench / chat display

Minimum viable behavior:

- display attachment filename, size, and type
- allow local open/download from stored path

Nice-to-have later:

- inline previews for text, markdown, image, and PDF
- explicit "saved from peer" badges

## Backward compatibility

### Keep working

- existing bearer-token file transfer
- LAN-mode auto-auth flows
- plain `/protocol/message`

### New behavior

- shared-token HMAC file transfer
- combined message+attachment delivery when both peers advertise capability

### Legacy `/hchat`

This plan does not require changing `/hchat` in Phase 1 or Phase 2.

However, there is a related decision:

- either teach `/hchat` to accept shared-token HMAC too
- or keep it bearer/LAN-only and clearly document it as a legacy compatibility
  surface rather than a primary trusted inter-instance path

Recommendation:

- treat `/hchat` as legacy and prefer protocol-owned messaging
- only extend `/hchat` auth if a concrete compatibility need remains
- do not change current `/hchat` behavior as part of the file-transfer fix
- do not require `/hchat` changes for plain chat interoperability with current
  HASHI Remote versions

### Plain chat compatibility with current peers

Backward compatibility for chat is a hard requirement:

- plain `/protocol/message` between current and upgraded peers must remain
  unchanged
- upgraded peers must not require `message_attachments_v1` for normal chat
- mixed-version peers may continue using legacy `/hchat` exactly as today
- attachment support is an additive feature gate, not a replacement for existing
  chat paths

## Testing plan

### Phase 1 tests

1. `/files/push` accepts valid shared-token HMAC
2. `/files/push` rejects missing or invalid HMAC in shared-token mode
3. `/files/stat` accepts valid shared-token HMAC
4. bearer-token file push remains functional
5. wrong auth mode produces clear error codes/messages
6. `remote_file_transfer.py` signs requests correctly in HMAC mode
7. mixed-version plain chat via `/protocol/message` remains unchanged
8. legacy `/hchat` behavior remains unchanged

### Phase 2 tests

1. attachment upload + commit succeeds with valid payload
2. message is not delivered if any attachment digest fails
3. message is not delivered if any attachment exceeds limit
4. attachment files are cleaned or quarantined if enqueue fails
5. receiver gets normalized manifest, not raw transfer blobs
6. capability downgrade is handled explicitly
7. plain chat still succeeds when attachment capability is absent
8. orphaned pending uploads are cleaned on startup sweep

## Rollout sequence

### Step 1

Implement shared-token HMAC for `/files/push` and `/files/stat`.

### Step 2

Upgrade `tools/remote_file_transfer.py` to use HMAC.

### Step 3

Update docs to make file transfer auth consistent across README and remote docs.

### Step 4

Add `file_transfer_hmac_v1` capability and compatibility tests.

### Step 5

Implement `message_attachments_v1` with transactional upload+commit and managed
spool.

### Step 6

Expose attachment metadata in runtime/workbench surfaces.

## Open questions

1. Should attachment files be kept permanently or cleaned after a retention period?
2. Should attachments be allowed for all agent-to-agent messages or only explicit
   tool-driven operations?
3. Do we want resumable/chunked transfers later for files larger than `256 MiB`?
4. Should `/hchat` remain legacy-only or be pulled into the shared-token trust model?
5. Should retries permit overwrite of prior pending attachment ids, or always
   write a new spool path?
6. Should file endpoints use a wider HMAC timestamp window than protocol
   messages for slow transfers?

## Recommended decision

Proceed in two layers:

1. fix file transfer auth immediately
2. add attachments as a protocol-owned feature after auth is unified

That keeps the first repair focused and low-risk while leaving room for a
cleaner attachment-commit model instead of bolting files onto legacy message
paths or destabilizing existing chat compatibility.
