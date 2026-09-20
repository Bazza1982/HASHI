# HASHI Demo Connector — Local Testing Handover

Date: 2026-09-20  
Branch: `feature/demo-mode-20260920`

This handover intentionally stops before real Workbench/HASHI integration or public deployment.

## 1. Pull safely

Prefer a separate worktree:

```powershell
git fetch origin
git worktree add -b local/hashi-demo-test-20260920 ..\hashi-demo-test origin/feature/demo-mode-20260920
cd ..\hashi-demo-test
```

Do not merge into main for the first test.

## 2. Offline checks first

Use the repository's normal Python 3.12 environment.

```powershell
python scripts/check_protected_core_changes.py --base origin/main
python -m pytest -q tests/test_demo_connector.py tests/test_session_api.py
```

If Ruff is installed:

```powershell
python -m ruff check orchestrator/demo tests/test_demo_connector.py
```

## 3. Use a disposable HASHI instance

Do not point the first Demo test at a daily-use HASHI bridge_home.

Create a separate instance / bridge_home with:
- no private workspaces
- no Telegram/WhatsApp credentials
- no Remote/HChat requirement
- only the provider credential you intend to test
- its own state/log/media directories

The Demo Connector is Functions-layer code and is registered by the normal Backend API service.

## 4. Required Demo configuration

The connector is fail-closed unless all of these are true:

```text
HASHI_DEMO_ENABLED=1
HASHI_DEMO_DAILY_RUN_LIMIT=<positive integer>
HASHI_DEMO_SERVICE_TOKEN=<strong random secret, at least 24 chars>
```

The service token may alternatively live in HASHI secrets as:

```json
{
  "demo_service_token": "<strong random secret>"
}
```

Suggested first local values:

```text
HASHI_DEMO_MAX_VISITORS=5
HASHI_DEMO_MAX_WORKERS=2
HASHI_DEMO_MAX_GENERATIONS=1
HASHI_DEMO_MAX_SESSIONS=3
HASHI_DEMO_TTL_SECONDS=3600
HASHI_DEMO_IDLE_TTL_SECONDS=900
HASHI_DEMO_WORKER_IDLE_SECONDS=30
HASHI_DEMO_CLEANUP_SECONDS=30
HASHI_DEMO_EVENT_WAIT_SECONDS=20
HASHI_DEMO_DAILY_RUN_LIMIT=50
```

Do not commit the real token or environment file.

## 5. Backend smoke test before Workbench

With HASHI running, call the Demo API only from loopback using the configured service token.

Expected first check:

```http
GET /api/demo/config
X-Hashi-Demo-Service-Token: <token>
```

Expected properties:
- HTTP 200
- `protocol = hashi.shared-demo`
- `version = 1`
- `ready = true`
- `tools = false`
- `text_deltas = false`

Wrong/missing service token must not reveal configuration and should fail closed.

## 6. Bootstrap flow

```http
POST /api/demo/bootstrap
X-Hashi-Demo-Service-Token: <token>
Content-Type: application/json

{"locale":"en"}
```

Expected:
- HTTP 201
- `visitor_token` appears only on this internal server-to-server response
- one Demo Agent configuration is created inactive
- one native HASHI Session is created
- no Worker starts merely because bootstrap occurred
- returned Agent name starts with `demo_`

Record the returned visitor token only in your disposable test notes; do not paste it into logs/issues.

## 7. Visitor-authenticated checks

For later calls the Workbench server will send:

```text
X-Hashi-Demo-Service-Token: <service token>
X-Hashi-Demo-Visitor: <visitor token>
X-Hashi-Demo-CSRF: <csrf token from bootstrap/me>
```

Check:
- `GET /api/demo/me`
- `GET /api/demo/sessions`
- create a second Session
- snapshots stay separate
- max fourth Session is rejected when max is 3

## 8. First real Run

Use one short benign message.

Expected behavior:
1. Connector authenticates visitor + Session.
2. Daily budget reserves one unit.
3. Demo Agent Worker starts on demand.
4. The message is accepted by the normal HASHI runtime.
5. Run appears in native SessionStore.
6. Events report queued/generating/final states.
7. After the configured idle period the Agent Worker stops.
8. Lease and Sessions remain available.

Verify that the request metadata shows:
- surface `hashi-demo`
- execution mode `zero`
- explicit Demo owner/session
- no memory injection
- habit learning ineligible

## 9. Idempotency

Retry the exact same message with the same idempotency key.

Expected:
- same Run ID
- `replayed=true`
- no second Worker start solely for replay
- no second daily-budget unit

Reuse the same key with different text.

Expected:
- HTTP 409 / idempotency conflict
- no new Run

## 10. Two-browser isolation

After Workbench-side integration is connected:
- open two independent browser profiles/contexts from the same IP
- ensure they receive different visitor leases and Agent IDs
- exchange Session IDs, Run IDs and event cursors deliberately
- all cross-owner reads/writes must fail without revealing whether the foreign object exists

Do not use IP as identity.

## 11. Cancel and end

Cancel a generating Run:
- native Run becomes stopped/cancelled
- Connector requests Worker shutdown
- late results must not be surfaced as a new valid Demo answer

End Demo:
- lease becomes inaccessible immediately
- active Run is cancelled
- Worker is stopped before destructive cleanup
- owner-scoped Session data is removed
- Agent config/workspace are removed
- daily aggregate budget does not reset

## 12. Expiry/restart recovery

Shorten TTL for this test.

Test:
- expire while idle
- expire during a Run
- restart shared Functions with a cleanup-pending lease
- ensure cleanup resumes
- ensure old visitor credentials never become valid again

## 13. Capacity work is separate

After correctness passes, increase toward the planned 32GB profile:

```text
HASHI_DEMO_MAX_VISITORS=200
HASHI_DEMO_MAX_WORKERS=32
HASHI_DEMO_MAX_GENERATIONS=24
```

Measure rather than assume:
- full process-tree memory
- Worker cold-start time
- Worker stop/release behavior
- CPU
- SQLite lock/wait time
- provider rate-limit responses
- latency p50/p95/p99

Do not interpret 200 valid leases as 200 simultaneous generations.

## 14. Merge gate

Before considering a PR to main:
- offline HASHI tests green
- real Workbench↔HASHI local flow green
- two-browser isolation green
- real provider smoke test green
- expiry/purge recovery green
- normal HASHI startup and existing frontend behavior unchanged

Public HTTPS and load testing are later deployment gates, not prerequisites for reviewing the source branch.
