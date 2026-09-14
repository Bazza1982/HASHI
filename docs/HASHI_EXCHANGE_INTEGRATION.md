# HASHI independent Exchange integration

Status: accepted closed-Pilot integration decision

Date: 2026-09-13 (Australia/Sydney)

Scope: HASHI client and PAO admission for the separately deployed
`hashi_exchange` service

## Decision

HASHI Exchange is a separate repository and service. It does not run inside a
particular HASHI or Hub instance, and HASHI does not own its identity provider,
routing table, listener, deployment, DNS, TLS termination, or operations.
HASHI implements only an optional Exchange client and a narrow authenticated
HChat admission adapter. The integration is disabled by default and does not
replace local HChat, LAN/Tailscale discovery, or Remote protocol 2.0.

Functional ownership is split without duplicating authority:

- Frontend Connectors own public-address parsing, the outbound WSS client, the
  loopback Remote API, wire receipts, and exact outbound retry.
- PAO owns durable inbound acceptance, idempotent Message/Run creation,
  scheduling, recovery, verified message context, and reply routing.
- Exchange owns Internet identity, grants, address resolution, connection
  epochs, delivery authorization, and relay receipts.
- PCM renders the PAO-created context but grants no Exchange authority. HER v2
  and Protected Core are unchanged.

The implementation belongs to the Functions and Instance Configuration
layers. No path in `orchestrator.runtime_contract.CORE_SOURCE_PATHS` is part of
this feature.

## Address and identity boundary

Existing names keep their meaning:

- `agent` is local;
- `agent@INSTANCE` is an existing HASHI/Remote address; and
- `agent@instance.username` is an Exchange public address.

The presence of a dot after `@` commits parsing to the public namespace. An
invalid public address is terminal and is never cleaned into, or retried as, a
LAN address. Public identity is the verified tuple of authority, actor,
registered instance, agent, and public address. Alias or Agent-name equality
does not merge actors.

Only the outbound Exchange WebSocket receives the instance credential. HASHI
validates the negotiated subprotocol, capabilities, welcome identity, epoch,
limits, delivery deadlines, complete sender/recipient tuples, and publication
state. TLS verification remains enabled for WSS, plain WS is accepted only on
loopback for the local lab, and WebSocket redirects are rejected before a
credential can be followed to another request target.

When both sides negotiate `authorized_routes_v1`, the client periodically
requests the bounded, grant-scoped route projection. It caches only the exact
target addresses and message kinds returned for its currently published sender
agents, plus a generic availability bit and revision/timestamp. The cache is
marked stale on disconnect and cleared if the configured identity changes.
This projection is not merged with Direct/LAN identities and never makes an
ungranted Exchange member visible.

The local `hchat_send` and `/remote` status hops reach HASHI Remote on loopback
and require the existing shared-token HMAC. They expose only Exchange status
and HChat message submission. They cannot proxy terminal, files, rescue, TUI,
Workbench, or arbitrary HTTP operations.

## Configuration and publication

The instance-owned `exchange` section contains:

```json
{
  "enabled": false,
  "authority_id": null,
  "url": null,
  "registered_instance_id": null,
  "instance_alias": null,
  "credential_ref": null,
  "published_agents": []
}
```

Enabled endpoints must be `wss://.../v1/connect`, except for a loopback
`ws://.../v1/connect` local lab. `credential_ref` names a value in the local
secret store; the credential is never copied into public configuration,
receipts, Session state, or logs. `update_exchange_config` performs a revision
CAS and changes only the Exchange section. Published agents are explicit and
must also be locally active. Removing an Agent from the list blocks new PAO
acceptance immediately and is propagated to the live Exchange connection.

Initial enablement requires adoption by the relevant HASHI Function/Remote
process. Source implementation and a running instance are separate facts.

## Durable delivery flow

```text
HChat public target
  -> authenticated loopback Remote request
  -> outbound WSS resolve/send
  -> Exchange delivery with verified tuple and epoch
  -> signed local connector evidence
  -> PAO inbox transaction
  -> delivered ACK
  -> SessionStore Message/Run transaction
  -> existing PAO queue and executor
```

The ACK means that the PAO inbox commit is durable; it does not claim model
execution has started. If ACK or scheduling is interrupted, replay uses the
same sender identity and message ID. SessionStore idempotency ensures a crash
between queueing and inbox finalisation still resolves to the original Run.

A queued Exchange Run is the narrow exception to general startup
reconciliation: a new receiving process reconstructs the in-memory queue item
with the original request, Message, and Run IDs. Running Exchange Runs are
interrupted like all other Runs because unknown execution state is never
replayed. Delivery expiry no longer applies once PAO has accepted the Run.

The inbox retains the delivery body only while a queued Run may need process
recovery. Once the Run leaves `queued`, the duplicate body is erased; the
minimal scheduled marker is retained for 30 days. The outbound spool retains
the exact wire frame only through its delivery deadline plus cleanup grace.
Body-free reply correlation is retained for 30 days and includes the immutable
frame digest, complete recipient tuple, conversation, message kind, and proof
that Exchange accepted the original request. Terminal rejection or expiry
cannot be overwritten by a late receipt.

## Replies and private authorization

An automatic `agent_reply` preserves the full public sender address,
`conversation_id`, and `in_reply_to`. The receiver accepts it only when those
facts match an Exchange-accepted local outbound `agent_message`. A typed reply
does not itself trigger another automatic reply. A public-looking legacy text
header is display data and cannot trigger Exchange egress.

Exchange v1 has no negotiated HASHI private/resource-proof capability. Any
non-empty authorization message ID, protected resource list, or private proof
is rejected as `UNSUPPORTED_CAPABILITY`; it is never dropped or downgraded to
an ordinary public request. A future proof version requires a separately
reviewed full-tuple binding and capability negotiation.

## Status projection

`/remote` presents transport truth without flattening identity or authority.
The local Remote process is shown first; the Exchange section separately shows
the authenticated WSS state, own public identity, explicit published agents and
only the authorized route snapshot. Direct/LAN peers remain a separate section.
An expected off-LAN direct peer is generic `unavailable`; a verified handshake
rejection is a configuration error. `/remote exchange` and `/remote direct`
select one view, while `/remote refresh` (and the compatibility `/remote list`
alias) refresh both. `/remote off` explicitly states that stopping the Remote
sidecar also stops its Exchange transport.

## Deployment boundary

Public Pilot composition, Cloudflare, identity administration and service
operation remain owned outside HASHI. A HASHI source change, a qualified
Functions artifact, adoption by one running instance and live public-WSS
verification are separate evidence and must be recorded separately. Formal Hub
identity remains a future D-stage boundary.
