# HASHI Exchange integration baseline

Date: 2026-09-13 (Australia/Sydney)

## Revisions and environment

- HASHI branch base: `19e330f985aaf6d5523fd82a3bcc8a4256b536c0` (`origin/main`).
- Exchange contract reviewed at: `9f6d7933d77483062595f6757fc9709540344db0`.
- Integration work is isolated from the running HASHI2 tree in a linked worktree.
- Baseline interpreter: CPython 3.12.13 on WSL2 Ubuntu 22.04.

## Existing call chain

- `orchestrator/hchat_delivery.py::deliver_hchat_draft` delegates to
  `tools/hchat_send.py::send_hchat`.
- Local delivery enters Workbench `/api/chat`; authenticated Remote protocol
  delivery enters `remote/api/server.py::_dispatch_protocol_message`, then
  `remote/protocol_manager.py::ProtocolManager.handle_protocol_message`, and
  finally the local Workbench admission path.
- PAO admission is
  `orchestrator/flexible_agent_runtime.py::enqueue_request` ->
  `orchestrator/runtime_session.py::accept_request` ->
  `orchestrator/session_store.py::SessionStore.accept_run`. The Session
  transaction atomically writes the user Message, Run, idempotency record and
  acceptance event before the in-memory queue is populated.
- Automatic replies currently originate in
  `orchestrator/flexible_agent_runtime.py::_hchat_route_reply`. Legacy routing
  parses the display header and then selects local, Remote 2.0, contact-cache,
  or discovery delivery.
- Remote 2.0 ACK and outbound correlation are stored separately by
  `remote/protocol_ack.py` and `remote/protocol_outbound.py`.
- Private authorization v1 binds message ID, old instance/agent names, target,
  content SHA-256 and resources. Proofs are verified again at PAO admission and
  execution. They do not bind Exchange authority, actor, registered-instance
  IDs, public address, or Exchange expiry, so v1 cannot be promoted to an
  Internet proof capability. Exchange v1 currently requires empty proof fields.

## Compatibility and trust boundaries to preserve

- Existing `agent@INSTANCE`, local groups, LAN/Tailscale discovery and Remote
  protocol `2.0` remain independent of public `agent@instance.username`.
- The old `normalize_identity` helper and old header regex are not valid public
  address parsers; a failed dotted address must never fall back to LAN routing.
- The existing `/api/bridge/hchat-exchange` is a HASHI-to-HASHI proxy and is not
  the independent Exchange trust boundary.
- Loopback access, a display header, ordinary Workbench JSON, a peer name, and
  an instance folder name are not verified Internet identity.
- Internet delivery must not expose or forward Remote terminal, file, rescue,
  TUI proxy, Workbench gateway, or arbitrary HTTP capabilities.
- `SessionStore.reconcile_incomplete_runs` currently interrupts pre-existing
  queued/running Runs because the general runtime has no durable queue replay.
  Exchange recovery therefore needs a narrow durable inbox/scheduling bridge;
  unknown execution state must never create a new Run.

## Baseline validation

Command scope: HChat send/delivery, Remote protocol/ACK/config/auth, message
context, private authorization, configuration CAS, and Workbench HChat channel
gate tests.

Result: **162 passed**, 1 third-party deprecation warning, 0 failed, in 7.15s.

Protected-Core verification was also run before implementation and passed. No
Core tuple file is part of the planned integration change.
