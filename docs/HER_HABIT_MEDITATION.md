# HER Habit–Meditation

## Status and boundary

Habit learning is an optional capability of the HER backend only. It is not a
HASHI orchestration feature, a Skill, a Dream extension, a shared registry, or a
cross-backend protocol.

The lifecycle is:

```text
Planning → Execution → Meditation → Write
```

Each agent owns only the Habit files in its own workspace. Relevance, rather
than a separate scope field, determines whether a Habit is used for a task.

## Controls

The default is disabled. Configuration is resolved in this order:

1. `global.her_providers.habit_meditation` supplies the HASHI instance default.
2. A `habit_meditation` object in an individual HER backend entry overrides it.
3. `HASHI_HER_HABIT_MEDITATION=on|off` is the final operational override.

Example instance configuration:

```json
{
  "global": {
    "her_providers": {
      "habit_meditation": {
        "enabled": true,
        "retrieval_limit": 5,
        "max_actions": 3,
        "max_trace_chars": 24000,
        "max_catalog_habits": 200,
        "meditation_timeout_seconds": 180
      }
    }
  }
}
```

Example backend override:

```json
{
  "engine": "her",
  "provider": "openrouter",
  "model": "deepseek/deepseek-v4-flash",
  "habit_meditation": {"enabled": false}
}
```

When disabled, HER preserves the original execution path: it does not create a
Habit directory, alter the task prompt, acquire the Habit execution lock, or
start a Meditation model call. This invariant applies to every HER effort level.

## Planning

Habit files contain a short title, compact natural-language metadata, and an
actionable body. Retrieval scores only the title and metadata. The body is read
only for the small set of matches selected for the current request.

Matched records are appended to the HER task input as advisory internal planning
context. They are explicitly subordinate to the current user request, policies,
permissions, and exact-output requirements.

## Execution and observable evidence

The main HER run is unchanged except for the optional planning context. After a
successful run, Meditation receives a bounded trace made from evidence HER can
actually expose:

- provider-visible thinking deltas or summaries;
- redacted-thinking notices;
- plans and termination diagnostics;
- tool starts, results, failures, and permission errors;
- the final response and completion reason.

No design assumption requires unavailable private chain-of-thought.

## Meditation

Meditation is scheduled silently after the user-facing run completes. It uses
the same configured HER model but an isolated session, read-only permission,
and a small execution budget. HER requires at least one valid tool when a tool
filter is supplied, so the subprocess exposes only `read_file`; the Meditation
prompt instructs the model not to call it. It cannot replace the normal HER
session checkpoint.

The model may return `create`, `update`, or `delete` actions. An empty action list
is valid and preferred when the run did not contain a reusable learning event.
There is no candidate, promotion, confidence, evaluation, expiry, or automatic
cross-agent copying lifecycle.

Meditation failures are logged and never turn a successful user task into a
failure. Pending Meditation and foreground HER executions share a lock so their
subprocess and session state cannot race.

## Write and storage

HASHI validates the structured response and performs the file write; the model
does not edit Habit files directly. Active records live at:

```text
workspaces/<agent>/habits/*.json
```

Minimal record shape:

```json
{
  "format": "her-habit-v1",
  "id": "check-permissions-before-writing-a1b2c3d4",
  "title": "Check permissions before writing",
  "metadata": "Relevant when a filesystem write may be denied or the workspace is read-only.",
  "body": "Inspect the active permission boundary before attempting or retrying the write.",
  "created_at": "2026-08-12T00:00:00+00:00",
  "updated_at": "2026-08-12T00:00:00+00:00"
}
```

Writes are atomic. Deletion is recoverable: the record is moved into the owning
agent's `habits/archive/` directory and excluded from retrieval.

## Dream remains separate

`/skill dream` remains available for nightly, multi-run memory reflection. It
does not read or write HER Habit files. Future Dream work may be designed later,
but this implementation does not prescribe any relationship between Dream and
Habit.
