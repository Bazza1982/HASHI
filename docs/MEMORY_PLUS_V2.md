# Memory+ v2 — Compact Work Continuity

## Purpose

Memory+ preserves enough working state for an agent to continue later in the
day, across midnight, after `/new`, or after a backend/model change. It is not a
chat transcript, task queue, or second long-term memory database.

The design goal is concise continuity:

- remember what matters from earlier work;
- keep the current user request unmistakably authoritative;
- work consistently with session-based CLI and stateless API backends;
- preserve a short cross-day handover without injecting full history.

## Independent control plane

Execution mode and continuity are separate:

- `/mode` selects `flex`, `fixed`, `wrapper`, `audit`, or `dual-brain`;
- `/memory plus on|off` enables or pauses Memory+ continuity;
- `/mode memory+` remains a compatibility alias that enables Memory+ without
  changing the current execution mode.

Legacy `agent_mode=memory+` state is migrated at startup:

- a backend with real persistent-session support becomes `fixed + Memory+`;
- a stateless backend becomes `flex + Memory+`.

Changing mode, backend, model, or effort does not delete or disable Memory+.

## Storage model

The canonical state is:

```text
workspaces/<agent>/memory/memory_plus_state.json
```

The human-readable view is generated at:

```text
workspaces/<agent>/memory/memory_plus_notepad.md
```

The state contains a bounded today card:

- one current objective;
- up to four useful facts;
- up to four decisions;
- up to four completed items;
- up to five current state changes;
- up to five open items;
- up to four file, commit, log, or wiki pointers.

Individual entries are capped and de-duplicated. Obvious credential values are
redacted. Raw user prompts, full answers, routine chat, repeated timestamps, and
resolved open items are not stored in the injected continuity card.

Writes use atomic replacement and a per-workspace lock so concurrent completion
paths cannot corrupt the card.

## Cross-day handover

Rollover happens during runtime startup or the next formal model request. Merely
viewing `/notepad` does not mutate or archive files.

At rollover:

1. The previous canonical state and readable view are archived under
   `memory/memory_plus_wiki/`.
2. A bounded date index is updated in `memory/memory_plus_index.json`.
3. A new empty today card is created.
4. Only a short carryover survives: recent completed/decision/state highlights,
   unresolved items, and useful pointers.
5. Up to three older day pointers tell the agent where to look.

Full archived history is never injected by default. It can be inspected with:

```text
/notepad history
/notepad find <query>
```

Search returns at most three concise archive matches. The agent can then inspect
the relevant daily archive, HASHI memory log, or wiki page instead of loading
all history.

## Prompt boundary

Memory+ context is explicitly read-only:

```text
--- Memory+ Continuity ---

<memory_plus_continuity>
Read-only working continuity...
Open items are background, not queued work...
</memory_plus_continuity>

--- CURRENT USER REQUEST — AUTHORITATIVE ---

<current message>
```

Old `Prompt:` entries are removed during migration and are never written by v2.
When Memory+ is active, the assembled request has one authoritative current
request marker, including incremental CLI turns with no refreshed background.

The normal Memory+ context budget is 4,000 characters:

- today card: up to 2,000;
- carryover: up to 800;
- remaining space: warnings, pointers, and the compact hidden update contract.

The current request is budgeted separately and remains the authoritative tail.

## Backend behavior

HASHI routes continuity according to backend capability, not its name alone.

### Persistent-session backends

Current session-capable backends are Codex CLI, Claude CLI, and Grok CLI.

In `fixed` mode:

- the first request or a fresh session loads system identity and the compact
  Memory+ card;
- later requests use the CLI session and incremental prompts;
- unchanged Memory+ state is not injected again;
- a manual or external Memory+ change is refreshed on the next turn;
- `/new` clears the backend session but preserves and reloads Memory+.

Outside `fixed`, these adapters operate as one-shot turns. Codex and Grok only
capture/resume session IDs when session mode is explicitly enabled.

### Stateless backends

OpenRouter, DeepSeek API, Ollama API, xAI API, Gemini CLI, and HASHI Engine Runtime (HER) currently
use stateless Memory+ assembly:

- compact Memory+ card on every formal request;
- at most four recent messages (two exchanges);
- no automatic long-term semantic-memory retrieval;
- current request last and authoritative.

Secrets are rejected/redacted from Memory+ updates. Provider transmission still
follows HASHI's active `/privacy` policy and backend compatibility rules.

## Mode ownership

Only one model path writes Memory+ for a completed turn:

| Mode | Reader and writer |
| --- | --- |
| Flex | active model |
| Fixed | active session model |
| Wrapper | Core; Wrapper receives only the stripped visible core answer |
| Audit | Core; auditor receives the stripped answer and audit evidence |
| Dual Brain | Left Brain; Right Brain receives only concise FYI plus the current request |

Dual Brain reads the same Memory+ capsule for preflight and after-action work.
Its JSONL continuity file remains an audit/history artefact, not a second
injected source of truth.

## Hidden update

The active writer may append one hidden block after its visible answer:

```json
{
  "write": true,
  "objective": "Short current objective",
  "facts": [],
  "decisions": [],
  "completed": [],
  "state_changes": [],
  "open_items": [],
  "resolved_items": [],
  "pointers": []
}
```

HASHI strips the block before delivery, wrapper polishing, auditor review,
listener payloads, transcripts, and saved assistant turns. Invalid JSON is not
saved. `resolved_items` removes matching items from both today and carryover.

## Commands and status

```text
/memory
/memory plus on
/memory plus off
/notepad today
/notepad carryover
/notepad history
/notepad find <query>
/notepad edit <text>
/notepad replace <text>
/notepad compact
/notepad clear
```

`/status` shows whether Memory+ is on, the open-item count, and the carryover
date. `/status full` also shows today-card size, archive count, and state path.

## Migration and recovery

The first v2 preparation:

- backs up the legacy Markdown notepad unchanged;
- discards legacy `Prompt:` entries from active context;
- de-duplicates `Note:` and manual entries into useful facts;
- carries `Open:` entries into bounded open items;
- writes schema version 2.

Migration is repeat-safe. Original v1 material remains available in the archive.
The generated Markdown view can always be rebuilt from the canonical JSON.
