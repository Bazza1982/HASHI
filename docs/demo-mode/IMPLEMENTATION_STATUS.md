# HASHI Demo Connector — Implementation Status

Date: 2026-09-21
Status: integrated into `main` for the HASHI v4 Beta source line

## Implemented

### Connector and protocol
- `orchestrator/demo/api.py`
- `orchestrator/demo/profile.py`
- `orchestrator/demo/leases.py`
- automatic registration from the existing Backend API Functions service
- protocol `hashi.shared-demo` version 1
- service-token authentication
- no-store dynamic responses
- bootstrap / me / sessions / snapshot / events / runs / cancel / end routes

### Anonymous isolation
- raw visitor token never persisted
- credential digest, owner, Agent and epoch are durable
- capacity reservation is transactional
- absolute TTL plus optional idle TTL
- CSRF value per lease
- owner + Agent + Session checks on data paths
- no IP-derived identity

### Native HASHI execution
- real AgentCreationService
- HER v2 only
- effort `zero`
- empty Tool permission list
- inactive Agent provisioning
- on-demand Function Worker start
- original SessionStore messages/runs/events
- memory injection skipped for Demo requests
- habit learning disabled on Run admission
- Session memory policy disabled and promotion schedule disabled

### Admission and limits
- per-visitor one active Run
- idempotency replay before new admission
- bounded running Worker count
- bounded generation semaphore
- durable conservative UTC daily request budget
- idempotency reservation survives process restart

### Events
- bounded JSON long poll
- public event allowlist
- no internal event detail
- no fake token streaming; `text_deltas=false`

### Cleanup
- lease revoke / expiry fencing
- native Run cancellation
- Worker stop
- scoped SessionStore owner purge
- Agent config removal
- safe Demo workspace deletion
- cleanup-pending retry state

## Source changes outside the new Demo package

`orchestrator/session_store.py` gains narrowly scoped helpers required for:
- disabling Session memory policy
- promotion schedule control
- owner-scoped active Run lookup
- idempotency lookup
- owner + Agent purge

`orchestrator/workbench_api.py` registers the Demo Connector in the existing Functions-layer HTTP service.

No protected Core file is changed.

## Offline evidence

GitHub Actions validates the branch with:
- protected Core manifest/diff guard
- Ruff
- focused Demo tests
- existing Session API tests

Additional route tests cover:
- config authentication and zero-Agent-ready behavior
- replay without Worker start
- real native Run admission shape
- durable budget reservation/commit
- explicit no-memory/habit request flags
- owner/session-scoped cancellation

## Explicitly not claimed yet

The following require local or deployment validation:
- Workbench Demo frontend ↔ real HASHI Connector
- real provider/model response
- Worker cold-start latency and measured RSS/PSS
- cancellation behavior against a live provider
- Windows/WSL target deployment
- 200 connected visitor test
- 200-user steady traffic test
- Cloudflare/public HTTPS
- third-party model retention behavior

See `LOCAL_TESTING.md`.
