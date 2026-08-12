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
Internal one-shot/ephemeral HER backends are always ineligible, including when
the process-wide environment override is on, so health probes and sidecars
cannot recursively learn Habits.

## Planning

Habit files contain a short title, compact natural-language metadata, and an
actionable body. Retrieval scores only the title and metadata. The body is read
only for the small set of matches selected for the current request.

Matched records are appended to the HER task input as advisory internal planning
context. They are explicitly subordinate to the current user request, policies,
permissions, and exact-output requirements.

Retrieval and use are deliberately different observations. Recording a selected
Habit ID proves retrieval only when the same ID is present in the executed
planning context. It does not prove that the model followed the Habit. Behavioral
use needs its own predeclared, observable next-request output or tool-side-effect
assertion. A conflicting Habit must lose to the current request.

## Execution and observable evidence

The main HER run is unchanged except for the optional planning context. After a
completed run, Meditation receives a bounded trace made from evidence HER can
actually expose. A HER timeout, non-zero exit, or cancellation after execution
started can also be reflected on; a pre-execution backend discovery failure
cannot. Evidence includes:

- provider-visible thinking deltas or summaries;
- redacted-thinking notices;
- plans and termination diagnostics;
- tool starts, results, failures, and permission errors;
- the final response and completion reason.

No design assumption requires unavailable private chain-of-thought.
Common credential-shaped values are redacted before a queued Meditation prompt
is stored. This is a narrow leakage guard, not a general-purpose deterministic
judgement of arbitrary natural-language safety.

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

A `no_change` journal terminal proves that the background Meditation wire,
isolation, validation, journal, and silence path completed. It does not prove a
Habit was formed, retrieved, or behaviorally used. Certification records those
three observations and their evidence references separately.

The Meditation model call has a bounded timeout. Timeout and other recoverable
runtime failures release the shared lock, return the durable job to the bounded
retry queue, and remain invisible to the user. Certification measures the
foreground lock wait against an explicit upper bound rather than merely checking
that the foreground eventually finishes.

Before the background model call starts, HASHI atomically journals the bounded,
redacted Meditation prompt under the owning agent workspace:

```text
workspaces/<agent>/backend_state/her_habit_meditation/*.json
```

HASHI's `request_id` remains a trace field and may repeat after a process
restart. At the start of each eligible HER foreground execution, HER creates a
separate 32-hex execution-scoped Meditation `job_id`. Every success, failure,
timeout, and cancellation exit for that execution reuses the same `job_id`;
the journal filename, recovery, and write idempotency use it instead of
`request_id`. Consequently, two runtimes may both process `req-0001` without
aliasing jobs, while repeated scheduling inside one execution still deduplicates.
Existing v1 journal files whose IDs were derived from request IDs remain
readable and recoverable without migration.

Interrupted jobs return to a bounded three-attempt queue and resume when an
eligible HER adapter initializes again. Once model actions are validated they
are journalled before Write, so a restart replays the same actions without
paying for or accepting a second model decision. Invalid model output fails that
job; queue, recovery, or write errors remain fail-open for the user-facing run.

## Write and storage

HASHI accepts only the closed `actions` JSON shape, bounds every field, rejects
credential-shaped Habit content, and performs the file write; the model does
not edit Habit files directly. Active records live at:

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

Writes are atomic. Creates use a stable per-job action identity, making replay
idempotent without semantic deduplication or an evaluation system. A valid
`create` or `update` becomes usable immediately. Deletion is recoverable: the
record is moved into the owning agent's `habits/archive/` directory and excluded
from retrieval.

## Dream remains separate

`/skill dream` remains available for nightly, multi-run memory reflection. It
does not read or write HER Habit files. Future Dream work may be designed later,
but this implementation does not prescribe any relationship between Dream and
Habit.
