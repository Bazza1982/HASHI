# HEA v1: minimal durable terminal event feed

Branch: feat/hea-event-mvp-20261120. Paired with Workbench same-named branch. No production adoption implied.

The source is the existing, committed SessionStore run_events table. GET /api/v1/automation/events?after=-1&limit=50 is a new qualified Session API route; it projects only:
- run.completed -> agent.run.completed
- run.failed -> agent.run.failed

The response contains a stable event_id, monotonic feed cursor, agent_id, run_id, session_id, occurred_at and automation_origin boolean. No final text, tool arguments, error details, or other Session data.

## Session schema change: 36 -> 37 (disclosed)

This branch legitimately raises `SessionStore.SCHEMA_VERSION` from 36 to 37 and
adds ONE table: `automation_event_index` (feed_id AUTOINCREMENT, event_id,
session_id, run_id, kind, owner_id, agent_id, event_sequence, created_at) with
indexes on (owner_id, feed_id) and (session_id). The migration is additive and
idempotent (`CREATE TABLE IF NOT EXISTS`); no existing Session column, table,
Run or message is altered or rewritten. Sessions and Runs carry over untouched
(covered by tests/test_hea_schema_migration.py).

Why a new table: SQLite rowids can be reused after destructive history
deletion, which would let a feed cursor silently skip new events. The
AUTOINCREMENT identity is monotonic even when history is purged, and the
purge path removes the owner's indexed rows so the feed head moves
backwards — consumers observe `reset_required: true` and re-baseline instead
of replaying or dropping events.

Baseline policy: only terminal events written AFTER adoption are projected.
Terminal events recorded on a schema-36 database before the migration are not
retro-projected; the feed head starts at zero and `after=-1` initialises there
(no history replay).

## Rollback behaviour (documented)

Downgrade to schema 36 is NOT transparent: a 36 build refuses to open a 37
database by design ("Session database requires a newer schema reader"). Do not
casually decrement the stored schema version to force compatibility.

Supported rollback procedure (operational, with HEA consumers stopped):

1. Preferred: restore the pre-upgrade backup of `sessions.sqlite3`
   (take one before adopting this build).
2. Explicit manual downgrade (only when no backup exists and no HEA consumer
   is connected):
   - `DROP TABLE automation_event_index;`
   - `UPDATE schema_metadata SET value='36' WHERE key='schema_version';`
   A 36 build then opens the database normally; the extra metadata is harmless
   to 36 readers.
3. Re-upgrading later re-creates the index through the normal idempotent
   migration. The feed head restarts at zero; any consumer holding an older
   cursor receives `reset_required: true` and re-baselines — never silent
   skipping. This round trip is covered by
   `test_downgrade_procedure_then_reupgrade_rebaselines`.

The caller must use the existing Session API owner and existing authenticated Remote Workbench gateway. after=-1 initialises at the current owner-scoped head (no history replay). after>=0 returns ordered committed events, has_more and next_cursor. wait_seconds=0..12 performs one bounded long-poll with a <=5s indexed database-check fallback for separate Function-process writers. No new per-Agent watcher, model call, worker, or Core edit.

Automation-origin runs are identified by the same persisted Run idempotency key prefix, hea:, which Workbench suppresses by default to prevent feedback loops in this MVP.

Important limits: the feed covers only the two terminal kinds of this MVP. Failed/aborted/cancelled non-run.failed terminal variants are not projected. The by-idempotency reconciliation endpoint used by Workbench is the existing owner-scoped Session lookup (`find_run_by_idempotency`), exposed through the Session API.

Local tests:
python -m pytest -q tests/test_automation_events.py tests/test_session_api.py tests/test_remote_workbench_gateway.py tests/test_hea_schema_migration.py

Test only with isolated state, no production database. Bring up two local agents and a separate Workbench installation; accept/create a run in agent A, complete and fail separate runs, confirm each terminal event appears once across Session boundaries and is delivered via Remote gateway to Workbench. Repeat after server restart and changed connection. No merging or deployment before paired review and evidence.
