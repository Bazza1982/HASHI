# HEA v1: minimal durable terminal event feed

Branch: feat/hea-event-mvp-20261120. Paired with Workbench same-named branch. No production adoption implied.

The source is the existing, committed SessionStore run_events table. GET /api/v1/automation/events?after=-1&limit=50 is a new qualified Session API route; it projects only:
- run.completed -> agent.run.completed
- run.failed -> agent.run.failed

The response contains a stable event_id, rowid cursor, agent_id, run_id, session_id, occurred_at and automation_origin boolean. No final text, tool arguments, error details, or new Session database schema.

The caller must use the existing Session API owner and existing authenticated Remote Workbench gateway. after=-1 initialises at the current owner-scoped head (no history replay). after>=0 returns ordered committed events, has_more and next_cursor. wait_seconds=0..12 performs one bounded long-poll with a <=5s indexed database-check fallback for separate Function-process writers. No new per-Agent watcher, model call, worker, or Core edit.

Automation-origin runs are identified by the same persisted Run idempotency key prefix, hea:, which Workbench suppresses by default to prevent feedback loops in this MVP.

Important limits: SQLite rowid is not an immutable global event log across destructive history deletion. If the latest row is deleted and SQLite reuses rowids, a cursor may miss an event; a later version should use a monotonic owner-scoped event index with matching forget/retention handling. Failed/aborted/cancelled non-run.failed terminal variants are not in this two-event MVP.

Local tests:
python -m pytest -q tests/test_automation_events.py tests/test_session_api.py tests/test_remote_workbench_gateway.py

Test only with isolated state, no production database. Bring up two local agents and a separate Workbench installation; accept/create a run in agent A, complete and fail separate runs, confirm each terminal event appears once across Session boundaries and is delivered via Remote gateway to Workbench. Repeat after server restart and changed connection. No merging or deployment before paired review and evidence.
