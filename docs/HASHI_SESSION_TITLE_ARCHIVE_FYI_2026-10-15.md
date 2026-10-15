# Session title and archive FYI — 2026-10-15

## Scope

PAO Session service, Functions layer. This change supports Workbench navigation
without introducing a second Session store or a maintenance model.

## Behaviour

- A conversation created without a user title records a system-owned placeholder.
- On the first accepted user Run, PAO derives a short deterministic title from
  the user-visible request and marks it as maintenance-owned.
- When an older untouched placeholder is listed, PAO derives the same title from
  its first visible user message. The migration preserves the Session activity
  timestamp and runs only once.
- A user rename is authoritative and is never replaced automatically.
- Replay of an existing idempotency key does not repeat title maintenance.
- The revision-checked Session update accepts `active` and `archived`.
- Archive clears channel bindings but retains all Session Messages, Runs and
  Events. Restore exposes the same retained records.
- The permanent default Session cannot be archived.
- Title and archive Events contain no conversation text.

## Excluded

No local model, online model, model download, provider call or maintenance-model
configuration is part of this change.

## Validation

Focused title, archive, restore, replay, persistence and API tests pass together
with the complete Session API suite. Protected Core remains unchanged.
