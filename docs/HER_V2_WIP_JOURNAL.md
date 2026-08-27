# HER v2 WIP Journal

Status: **active operational contract**

The HER v2 WIP Journal is crash-safe, model-independent transient Context for
unfinished work. It is not Agent Memory, Memory+, a continuation command, or a
feature toggle.

## Lifecycle

1. At the start of every HER v2 turn, HASHI reads any prior Journal records and
   appends the new request boundary durably.
2. If prior records exist, HASHI appends them to the current prompt with a
   neutral warning: the work may be unrelated and must not be continued by
   default.
3. While the turn is active, observable HER v2 audit events are copied into the
   Journal only after their canonical audit record is durable.
4. An error, interruption, or other non-`COMPLETED` Ledger state preserves the
   accumulated Journal.
5. After a later Ledger is durably `COMPLETED`, HASHI atomically replaces the
   Journal with an empty file.

The Journal does not independently decide that the user said “continue”, and
its presence does not flatten the current request's authority. The current
request remains authoritative.

## Files and audit evidence

The active Journal is stored per Agent:

```text
<agent-workspace>/backend_state/her_v2/wip_journal.jsonl
```

The canonical lifecycle evidence is stored in the normal HER v2 audit log:

```text
<base-logs-dir>/<agent>/her_v2_audit.jsonl
```

If the primary audit path is unavailable, the durable fallback is:

```text
<agent-workspace>/backend_state/her_v2/audit_fallback.jsonl
```

Filter canonical audit rows where `stage` is `wip_journal`. The lifecycle event
names are:

- `wip_journal_turn_started`
- `wip_journal_context_injected`
- `wip_journal_preserved`
- `wip_journal_cleared`

Lifecycle payloads contain non-content metrics such as `record_count`,
`size_bytes`, `context_injected`, `ledger_status`, and `reason`. This makes the
Journal's create/inject/preserve/clear behaviour observable without copying its
private content into the lifecycle event.

The Journal itself can contain prompts and audited work details and is created
with owner-only file permissions. Treat it as sensitive transient operational
state. A torn final JSONL line is ignored without hiding earlier durable
records.

## Correct interpretation

- An empty Journal after a successful turn is expected and means the clear
  stage ran.
- A non-empty Journal after an interrupted/error turn is expected and means
  recovery context remains available.
- A later turn receiving the Journal does not prove that it correctly resumed
  the old task; it proves only that the recovery context was supplied.
- The lifecycle audit log is the authoritative record for checking when the
  Journal started, was injected, was preserved, or was cleared.
