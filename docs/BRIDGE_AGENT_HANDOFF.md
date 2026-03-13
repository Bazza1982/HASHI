# Bridge Agent Handoff

Date: 2026-03-10
Project: `<project_root>`

## Purpose

This file is the handoff for another agent to continue the bridge agent conversation work. It captures:

- the current verified implementation state
- the live smoke test plan that still needs to be run
- all remaining scope after the current Phase 1 code drop

## Current Verified State

Phase 1 bridge code exists in the live workspace.

Verified modules:

- `orchestrator/bridge_protocol.py`
- `orchestrator/agent_directory.py`
- `orchestrator/conversation_store.py`
- `orchestrator/conversation_router.py`
- `orchestrator/workbench_api.py`
- `agent_capabilities.json`

Verified current behavior from code:

- bridge request validation exists
- bridge reply validation exists
- SQLite-backed thread/message storage exists at `state/bridge_conversations.sqlite`
- sender/receiver permission checks exist in router logic
- runtime delivery uses `runtime.enqueue_api_text(..., source="bridge:<message_id>", deliver_to_telegram=False)`
- automatic reply capture exists through `runtime.register_request_listener(...)`
- manual reply submission endpoint also exists
- bridge data is stored in SQLite, not in `conversation_log.jsonl`

Verified bridge endpoints:

- `POST /api/bridge/message`
- `POST /api/bridge/reply`
- `POST /api/bridge/spawn`
- `GET /api/bridge/message/{message_id}`
- `GET /api/bridge/thread/{thread_id}`
- `GET /api/bridge/capabilities/{agent}`

Important current limit:

- `POST /api/bridge/spawn` is not implemented yet and currently returns `501`

Important current protocol limit:

- only `ask` and `notify` are supported request intents in `bridge_protocol.py`
- `delegate`, `spawn`, and `command` are planned but not implemented in protocol validation yet

## What Is Still Unproven

The main unresolved item is not code presence. It is live runtime validation.

Not yet proven end-to-end in the current state:

- two live agents can complete a full bridge request/reply cycle over HTTP
- permission failures behave correctly against real running runtimes
- automatic reply capture works reliably with real agent responses
- thread/message fetch shows the expected final persisted state after live runtime execution

## Smoke Test Plan

## Objective

Prove the implemented Phase 1 bridge request/reply flow works end-to-end against live running agents.

## Preconditions

1. Workbench API is running on localhost.
2. At least two fixed agents are running and healthy.
3. Their entries in `agent_capabilities.json` allow the chosen sender and target pair.
4. If admin auth is enabled, have the `workbench_admin_token` ready for `/api/admin/*` requests.

## Suggested Agents

Use two already configured fixed agents that are online and permitted to talk to each other.

Good first check:

- `GET /api/bridge/capabilities/{agent}`
- `GET /api/health`

Optional admin helpers:

- `POST /api/admin/start-agent`
- `POST /api/admin/commands/{name}`
- `POST /api/admin/smoke`

## Test Sequence

### 1. Confirm agent runtime availability

Check health:

```text
GET http://127.0.0.1:18800/api/health
```

Confirm capability and policy for both agents:

```text
GET http://127.0.0.1:18800/api/bridge/capabilities/{agent}
```

Expected:

- sender and target both exist
- target runtime reports online
- policy allows sender -> target

### 2. Send a bridge request

Example:

```json
POST http://127.0.0.1:18800/api/bridge/message
{
  "from_agent": "coder",
  "to_agent": "claude-coder",
  "intent": "ask",
  "text": "Reply with exactly: bridge smoke reply",
  "reply_required": true
}
```

Expected response:

- `ok: true`
- `message_id` returned
- `thread_id` returned
- `request_id` returned
- status is `queued`

### 3. Poll the bridge store

Fetch message:

```text
GET http://127.0.0.1:18800/api/bridge/message/{message_id}
```

Fetch thread:

```text
GET http://127.0.0.1:18800/api/bridge/thread/{thread_id}
```

Expected intermediate state:

- original request message exists in SQLite
- request status moves from `received` to `queued`
- thread status becomes `waiting_reply`

### 4. Wait for automatic runtime reply capture

Poll the thread/message endpoints until one of these happens:

- request message status becomes `completed`
- a correlated reply message appears in the thread
- request status becomes `failed`

Expected success state:

- request message status becomes `completed`
- reply message exists with `kind: reply`
- reply `in_reply_to` equals the original `message_id`
- thread status becomes `completed`
- reply `result_text` contains the agent answer

### 5. Negative permission test

Send a bridge request using an unauthorized sender/target pair or intent.

Expected:

- HTTP `403`
- clear error string
- permission audit row recorded
- message/thread persisted as rejected if the request reached store creation

### 6. Optional manual reply path test

Use `POST /api/bridge/reply` with a valid `in_reply_to` request if manual reply submission still needs verification.

Expected:

- reply stored in SQLite
- request status updated from pending state to final state
- thread status updated consistently

## Pass Criteria

The smoke test passes only if all of the following are true:

- live bridge message accepted over HTTP
- message persisted in SQLite
- target runtime receives and processes the task
- reply is captured automatically or manually with correct correlation
- final state is retrievable from message/thread endpoints
- permission denial path returns the expected error

## Fail Buckets

If the smoke test fails, classify it as one of these:

- API wiring failure
- capability/policy mismatch
- target runtime offline
- bridge prompt delivered but no reply captured
- reply captured but not persisted correctly
- message/thread retrieval mismatch

## Remaining Scope

## Remaining Phase 1 Work

This is the scope still open before Phase 1 can be called proven and stable:

1. Run the live end-to-end smoke test above.
2. Confirm the request listener callback reliably fires for real runtime responses.
3. Verify failure behavior when the target runtime errors, times out, or never replies.
4. Verify the manual `POST /api/bridge/reply` path against a real stored request.
5. Verify permission-denied flows against real capability settings.
6. Decide whether any additional operator endpoint or status field is needed after the first live run.

## Phase 1 Hardening Scope

These are not core missing features, but they are likely the next engineering tasks after the first live pass/fail result:

1. Add clearer operational status values if current `queued/completed/failed/rejected` is not enough.
2. Add better timeout marking for requests that never produce a runtime callback.
3. Add tests for `ConversationRouter`, `AgentDirectory`, and `ConversationStore`.
4. Verify behavior across backend types if bridge is expected to work beyond the first two agents tested.
5. Confirm whether duplicate manual replies or duplicate callbacks need explicit guard logic.

## Phase 2 Scope

Delegation support:

1. Add `delegate` intent to protocol validation.
2. Add required scope mapping for delegation.
3. Support richer reply statuses: `ok`, `partial`, `failed`, `refused`.
4. Add artifact/reference support in replies if needed.
5. Add timeout and work-tracking semantics for delegated tasks.

## Phase 3 Scope

Controlled spawn:

1. Implement real `POST /api/bridge/spawn`.
2. Restrict spawn to preconfigured fixed agents only.
3. Start the target agent if offline.
4. Wait for healthy/ready runtime state.
5. Optionally send an initial bridge task after startup.
6. Audit all spawn events in SQLite.

## Phase 4 Scope

Supervisor/control flows:

1. Add reserved control intents only if truly needed.
2. Keep control rights separate from normal conversation rights.
3. Require explicit allowlists per action.
4. Audit every control action.

## Explicit Non-Goals Still Deferred

These remain intentionally out of scope for now:

- arbitrary dynamic agent creation
- multi-hop agent chains
- mixing bridge protocol records into human transcript logs
- using `/api/chat` or `/api/admin/*` as the final inter-agent contract

## Useful Files For The Next Agent

- `<project_root>/orchestrator\bridge_protocol.py`
- `<project_root>/orchestrator\agent_directory.py`
- `<project_root>/orchestrator\conversation_store.py`
- `<project_root>/orchestrator\conversation_router.py`
- `<project_root>/orchestrator\workbench_api.py`
- `<project_root>/agent_capabilities.json`
- `<project_root>/BRIDGE_AGENT_CONVERSATION_DEVELOPMENT_PLAN.md`

## Recommended Immediate Next Action

Do not add new protocol features first.

Run the live smoke test against two real agents, classify the first failure or confirm a full pass, then use that result to decide whether the next task is:

- Phase 1 bug fixing
- Phase 1 hardening/tests
- or Phase 2 delegation work
