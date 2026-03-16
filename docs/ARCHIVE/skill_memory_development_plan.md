# `/skill memory` Development Plan

**Status:** Planning
**Created:** 2026-03-11
**Author:** Sakura (research assistant)

---

## Overview

Implement a `memory` toggle skill that automatically writes journal entries and updates agent memory files on a configurable schedule. Default-on for all agents except openrouter-based agents.

---

## Goals

1. Give each agent a persistent memory routine that runs without manual intervention
2. Keep `MEMORY.md` current across sessions without relying on bridge handoff alone
3. Build up a dated journal archive for retrospective review
4. Never interrupt an agent that is actively processing a task

---

## Skill Specification

| Property | Value |
|----------|-------|
| Name | `memory` |
| Type | `toggle` |
| Default state | ON (except openrouter agents) |
| Default schedule | Daily at 01:00 |
| Cron behavior | Managed — pause/resume, not delete/recreate |
| Interrupt protection | YES — skips cycle if agent is busy |

---

## Command Interface

```
/skill memory              → show current status, schedule, last run time
/skill memory on           → enable (re-enable paused cron without reconfiguring)
/skill memory off          → pause cron job (preserve schedule for re-enable)
/skill memory daily        → set schedule to 01:00 daily (default)
/skill memory hourly       → set schedule to top of every hour
/skill memory weekly       → set schedule to Monday 01:00
/skill memory monthly      → set schedule to 1st of month at 01:00
```

**Pause vs delete:** `/skill memory off` pauses the cron entry (sets `enabled: false`). Schedule is preserved. `/skill memory on` re-enables without reconfiguration.

---

## File Structure

```
skills/memory/
  skill.md         # Skill manifest: type, default_on, description, parameter docs
  schedule.py      # Cron entry management: create, pause, resume, update frequency
  journal.py       # Journal write logic: context summary, MEMORY.md update
```

---

## Cron Entry Schema (in `tasks.json`)

```json
{
  "id": "memory_journal_{agent_name}",
  "agent": "{agent_name}",
  "type": "cron",
  "schedule": "0 1 * * *",
  "skill": "memory",
  "enabled": true,
  "managed_by": "skill:memory",
  "last_run": null,
  "frequency_label": "daily"
}
```

- `managed_by` marks this as skill-owned, preventing manual scheduler interference
- `frequency_label` tracks human-readable schedule label for status display
- `enabled: false` = paused; entry is preserved

---

## Journal Write Logic (`journal.py`)

### Pre-flight checks
1. Query agent runtime: is the agent currently processing a message?
2. If YES: log `[memory] skipped — agent busy` and exit
3. If NO: proceed

### Journal content
The journal prompt injected into the agent queue:

```
SYSTEM (memory skill): Write today's journal entry.
- Summarize key work done this session
- Note decisions made and outcomes
- Flag any unresolved issues or open questions
- Update MEMORY.md if any facts have changed or should be added
- Save journal to: memory/journals/YYYY-MM-DD.md
Do not send this as a user-facing reply. Write files only.
```

### Files written
- `memory/journals/YYYY-MM-DD.md` — dated journal entry
- `memory/MEMORY.md` — updated if facts changed

---

## `agents.json` Changes

Add `default_skills` to each agent entry:

```json
{
  "sakura":   { "default_skills": ["memory", "recall"] },
  "lily":     { "default_skills": ["memory", "recall"] },
  "claude-coder": { "default_skills": ["memory", "recall"] },
  "codex-coder":  { "default_skills": ["memory", "recall"] },
  "gemini-coder": { "default_skills": ["memory", "recall"] }
}
```

Openrouter agents omit `memory` from `default_skills` (or use empty array).

---

## `skill_manager.py` Responsibilities

On agent startup:
1. Read `agents.json` for `default_skills`
2. For each default skill, check if it is already active in `tasks.json`
3. If not active: create the managed cron entry with default schedule

On `/skill memory [param]`:
1. Parse parameter: `on | off | daily | hourly | weekly | monthly`
2. Update cron entry accordingly
3. Return current status

---

## Interrupt Protection Design

`schedule.py` or `journal.py` must check agent state before writing. Two options:

**Option A — Agent state flag file:**
Agent runtime writes `{workspace}/.processing` when handling a message, removes it when done.
Memory skill checks for this file before proceeding.

**Option B — Workbench API query:**
Memory skill calls `GET /api/agent/{name}/status` on the workbench API.
Cleaner but requires workbench to be running.

**Recommendation:** Option A (file flag) — simpler, no runtime dependency, works even if workbench is down.

---

## Implementation Phases

### Phase 1 — Skill manifest + cron management
- [ ] Create `skills/memory/skill.md` with frontmatter and parameter docs
- [ ] Create `skills/memory/schedule.py` — cron entry CRUD, pause/resume
- [ ] Wire `/skill memory [param]` command routing in skill_manager
- [ ] Add `default_skills` to `agents.json`
- [ ] Add `skill_manager.py` startup behavior for default_skills

### Phase 2 — Journal write logic
- [ ] Create `skills/memory/journal.py` — pre-flight check + prompt injection
- [ ] Implement `.processing` flag in agent_runtime.py (both Fixed and Flex)
- [ ] Test journal write on manual trigger before enabling cron

### Phase 3 — Testing and defaults
- [ ] Test with sakura agent: manual trigger, then scheduled trigger
- [ ] Verify skip-when-busy behavior
- [ ] Verify pause/resume preserves schedule
- [ ] Confirm openrouter agent does NOT get memory skill by default
- [ ] Add status display to `/skill memory` with last-run timestamp

---

## Open Questions

1. **Journal format:** Free-form markdown, or structured template with fixed sections?
2. **Memory retention:** Should old journal files be archived/summarized after N days?
3. **Agent awareness:** Should the agent be told the journal prompt is system-internal, or treat it as a normal task? (Current plan: system-internal, no user-facing reply)
4. **Failure handling:** If journal write fails (e.g., disk error), should the agent notify the user via Telegram?

---

## Dependencies

- `skill_manager.py` — needs to exist and handle toggle skill routing
- `tasks.json` — `enabled: false` on cron entries is **already implemented** (confirmed in current schema — no new work needed)
- `agent_runtime.py` (both variants) — must write `.processing` flag
- `agents.json` — must support `default_skills` array per agent

---

*This plan is subject to revision after review.*
