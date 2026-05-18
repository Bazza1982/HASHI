# Dual-Brain Structure Design and Implementation Plan

Status: implemented runtime baseline.

Owner: HASHI1 implementation.

Related design:

- `docs/WRAPPER_AGENT_MODE_PLAN.md`
- `docs/AUDIT_AGENT_MODE_PLAN.md`
- `docs/WIKI_REDESIGN_PLAN.md`
- `docs/WIKI_AI_CURATION_FIX_PLAN.md`
- `docs/AGENT_FYI.md`

## 1. Purpose

The dual-brain structure separates memory continuity from task execution.

Current fixed-mode agents are asked to do two different jobs in the same model
call:

- remember what happened before, decide what is current, and recover relevant
  background from past context;
- execute the user's current request by planning, using tools, editing files,
  and producing the final answer.

This overloads the execution model with both past-facing memory work and
future-facing task work. The result is weak same-day continuity even when HASHI
already has useful long-term memory in `AGENT.md`, logs, consolidated memory,
and the generated wiki.

The dual-brain structure introduces:

- Left Brain: a past-facing continuity and memory supplier.
- Right Brain: the existing execution backend/model that acts on the user's
  request.

The left brain does not rewrite the user prompt, decide the user's intent,
plan the task, or command the right brain. It is an LLM-based memory/context
model with constrained memory read/write tools. It maintains a same-day
continuity notepad, recalls relevant wiki background when useful, and supplies
an appropriate FYI context block to the right brain.

The right brain receives the original user prompt faithfully, plus the FYI
context block, and executes the user request.

## 2. Core Principle

```text
User provides orientation and intent.
Left Brain records continuity and supplies memory.
Right Brain executes the user's request with FYI context.
Original prompt is passed faithfully.
Context packet is FYI, not an instruction override.
Continuity notepad is updated after meaningful turns.
```

The left brain is not a second planner. It is a continuity clerk and memory
supplier.

## 2.1 Product Principle: Real Product Only

The dual-brain structure should be implemented as the real product design.

Do not replace the left brain with a keyword matcher, template renderer, or
static search wrapper. That would collapse the design back into a single-brain
architecture plus retrieval plumbing.

The left brain must be LLM-based from the first product implementation. Fixed
rules may provide scaffolding for file IO, schema validation, locking,
diagnostics, and safe tool boundaries, but memory selection and context
sufficiency are left-brain cognition tasks.

Testing is still required, but testing should validate the real product path
rather than a reduced surrogate design.

## 3. Memory Layers

HASHI should treat memory as separate layers with different lifetimes and
injection rules.

### 3.1 Identity Layer

Source:

- `workspaces/<agent_id>/AGENT.md`
- agent seed material where applicable

Properties:

- very long-term;
- relatively stable;
- injected at chat/session start by existing agent configuration;
- defines identity, role, voice, boundaries, and durable commitments.

Dual-brain behavior:

- left brain should know the same identity layer;
- left brain should not duplicate the full identity layer in every FYI context;
- if identity matters for a task, left brain may include a short reminder.

### 3.2 Knowledge Layer

Source:

- generated Obsidian wiki topics;
- wiki index and topic candidate pages;
- consolidated memory and classifications that feed the wiki.

Properties:

- structured long-term memory;
- mostly current up to the most recent successful Lily wiki daily update;
- suitable for project background, stable facts, workflows, and previous
  outcomes.

Dual-brain behavior:

- left brain uses the wiki as the primary long memory source;
- left brain is free to recall from wiki topics and indexes as needed;
- deterministic search can expose candidate materials, but must not impose
  keyword-only filtering as the decision layer;
- right brain receives sufficient FYI context selected by the left brain, not
  raw wiki dumps.

### 3.3 Continuity Layer

Source:

- left brain same-day notepad;
- optionally same-day hchat/message summaries;
- optionally selected current-session events.

Properties:

- short-term day memory;
- covers the gap between the last successful wiki update and the current prompt;
- resets after the wiki pipeline absorbs it;
- can be consolidated earlier if it grows too large.

Dual-brain behavior:

- left brain reads and writes this layer continuously;
- right brain receives relevant continuity notes as FYI;
- daily wiki publish should become the normal sleep/reset boundary.

### 3.4 Working Layer

Source:

- the current prompt;
- immediate CLI backend context window;
- current tool state inside the right-brain execution turn.

Properties:

- shortest lifetime;
- execution-local;
- not reliable as persistent continuity;
- should not be treated as the only memory source.

Dual-brain behavior:

- original prompt is always forwarded faithfully to the right brain;
- left brain does not alter the prompt even if the topic changes suddenly or
  contradicts previous continuity.

## 4. Architecture

Initial logical pipeline:

```text
incoming user prompt
  -> left brain preflight
       - read same-day notepad
       - decide whether same-day notepad is sufficient
       - recall wiki/background memory only when the notepad is insufficient
       - build sufficient FYI context
  -> right brain execution
       - receive original prompt unchanged
       - receive FYI context
       - execute using normal backend/tools
       - produce final answer
  -> left brain after-action update
       - record meaningful results, decisions, promises, and changed state
       - maintain the same-day notepad
```

The left brain can be implemented as a standalone script/service before any
runtime integration. The first product implementation should not require
changing HASHI core runtime files.

## 5. Left Brain Responsibilities

The left brain must:

- maintain the same-day continuity notepad;
- read the notepad before each user-facing execution turn;
- decide internally whether the notepad alone is enough;
- query wiki/knowledge sources only when notepad context is insufficient;
- produce a sufficient FYI block for the right brain;
- preserve the original prompt exactly;
- update the notepad after meaningful right-brain turns;
- log what it read, wrote, and retrieved;
- support diagnostics and dry-run inspection.

The left brain must not:

- rewrite the user prompt;
- decide whether the user request is valid;
- refuse, reroute, or block the request;
- plan the execution task;
- call execution/task tools on behalf of the right brain;
- inject large evidence dumps into the right-brain prompt;
- override higher-priority system/developer/runtime instructions;
- become a second agent in command.

The left brain may use memory tools:

- read the continuity notepad;
- write the continuity notepad;
- inspect wiki index/topic files;
- inspect safe same-day event sources configured for continuity;
- write its own diagnostics and generated FYI artifacts.

These tools are memory/context tools, not task execution tools.

## 6. Right Brain Responsibilities

The right brain is the existing execution model/backend.

It must:

- receive the original user prompt faithfully;
- receive the left-brain FYI context as background only;
- execute the user's request according to normal HASHI behavior;
- use tools, edit files, run commands, or reply as needed;
- produce the user-facing answer;
- expose enough final-turn information for the left brain to update the
  continuity notepad.

The right brain should not be asked to reconstruct large past context if the
left brain can provide the relevant same-day or wiki memory.

## 7. FYI Context Contract

The left brain's output is not a new instruction layer. It is an FYI context
packet.

Recommended markdown form:

```markdown
<left_brain_fyi>
This FYI does not modify or override the user's prompt.

## Same-Day Continuity
- ...

## Relevant Wiki Memory
- ...

## Open Continuity Notes
- ...

## Useful Background
- ...
</left_brain_fyi>
```

Recommended JSON form for internal tools:

```json
{
  "agent_id": "lily",
  "generated_at": "2026-05-15T09:30:00+10:00",
  "last_wiki_publish_id": "20260515T040535+1000",
  "original_prompt_sha256": "...",
  "same_day_continuity": [],
  "relevant_wiki_memory": [],
  "open_continuity_notes": [],
  "useful_background": [],
  "source_pointers": [],
  "soft_budgets": {
    "max_items": 12,
      "target_chars": 6000
  }
}
```

Source pointers should be brief references, not full evidence payloads. The
right brain should not be forced back into past-facing evidence evaluation.
The left brain should choose enough context for the right brain to act well,
without treating character count as a hard goal.

## 8. Continuity Notepad Contract

Each agent gets a same-day continuity notepad.

Suggested path:

```text
workspaces/<agent_id>/continuity/
  current_notepad.md
  current_notepad.jsonl
  archived/
```

Markdown is useful for human inspection. JSONL is useful for tooling.

Suggested JSONL event fields:

```json
{
  "ts": "2026-05-15T09:30:00+10:00",
  "agent_id": "lily",
  "source": "left_brain_after_action",
  "kind": "decision | result | promise | file_state | handoff | warning | topic",
  "summary": "Paper 3 materials were zipped and uploaded to Lily INTEL.",
  "scope": "task | project | agent | system",
  "expires": "after_next_wiki_publish",
  "related_paths": [],
  "related_topics": [],
  "right_brain_request_id": ""
}
```

Notepad policy:

- reset after successful daily wiki publish absorbs same-day continuity;
- keep only same-day continuity by default;
- allow manual or scheduled mid-day compaction if the notepad grows too large;
- never use the notepad as a permanent knowledge store;
- do not store secrets unless they are already allowed in the existing local
  private memory policy.

## 9. Wiki Boundary and Sleep Cycle

The daily wiki pipeline becomes the normal sleep boundary.

Daily cycle:

```text
03:05 memory consolidation
04:05 Lily wiki daily update
  -> classify
  -> persist classifications
  -> incremental topic discovery
  -> candidate promotion
  -> publish generated wiki zones
  -> mark continuity notepads eligible for reset
```

After a successful wiki publish:

- same-day continuity prior to the publish is considered absorbed if it was
  included in memory sources;
- left brain starts a fresh current notepad for the day;
- old notepad files are archived with manifest metadata;
- if publish fails, do not reset the notepad.

## 10. Recall Policy

Recall is not automatic. The left brain must use a notepad-first policy:

1. Read current same-day notepad first.
2. If the notepad is sufficient, do not query wiki.
3. If the notepad is insufficient, produce a wiki query and request long-memory
   recall.
4. After wiki recall, forward only concise FYI context to the right brain.

Available memory sources:

1. Current same-day notepad.
2. Recent local session/hchat/event records, if configured.
3. Wiki index and relevant generated topic pages, only on demand.
4. Optional raw consolidated memory only when wiki is insufficient.

The system may provide search primitives over these sources, but the left brain
decides what is relevant. Do not hard-code keyword-only relevance rules as the
context selection layer.

Recall should consider:

- exact project names;
- agent names;
- command names;
- file paths;
- current task nouns;
- recent open promises and handoffs;
- wiki topics with high quality and low uncertainty.

The left brain may retrieve more internally than it sends to the right brain.
Only useful FYI context should be forwarded.

## 11. Source Routing

Initial sources to run through dual-brain preflight:

```python
DUAL_BRAIN_USER_SOURCES = {
    "api",
    "text",
    "voice",
    "voice_transcript",
    "photo",
    "audio",
    "document",
    "video",
    "sticker",
}
```

Initial bypass sources:

```python
DUAL_BRAIN_BYPASS_SOURCES = {
    "startup",
    "system",
    "scheduler",
    "scheduler-skill",
    "loop_skill",
    "retry",
    "session_reset",
}
```

Initial bypass prefixes:

```python
DUAL_BRAIN_BYPASS_PREFIXES = (
    "bridge-transfer:",
    "ticket:",
    "cos-query:",
)
```

Hchat should be decided carefully. Many hchat messages are continuity-relevant.
The initial implementation may record hchat into the notepad without adding
full dual-brain preflight to every hchat path.

## 12. Commands and Operator UX

Potential commands:

```text
/dualbrain status
/dualbrain on
/dualbrain off
/dualbrain check
/dualbrain brief <text>
/dualbrain notepad
/dualbrain reset-notepad CONFIRM
/dualbrain compact
```

For the first product implementation, use standalone scripts before runtime
commands:

```bash
python3 scripts/dual_brain/build_fyi.py --agent lily --prompt-file /tmp/prompt.txt --mode normal
python3 scripts/dual_brain/update_notepad.py --agent lily --request-id ... --result-file /tmp/result.txt
python3 scripts/dual_brain/check.py --agent lily
python3 scripts/dual_brain/reset_after_wiki_publish.py --agent lily --publish-id ...
```

Runtime commands should be added only after the external tools are proven.

## 13. Configuration

Suggested config path:

```text
private/dual_brain_config.json
```

Example:

```json
{
  "enabled_agents": ["lily", "zelda"],
  "default_mode": "normal",
  "notepad_warning_chars": 200000,
  "fyi_soft_budget_chars": 6000,
  "wiki_root": "/mnt/c/Users/thene/Documents/lily_hashi_wiki",
  "wiki_generated_topics": "10_GENERATED_TOPICS",
  "wiki_generated_indexes": "30_GENERATED_INDEXES",
  "reset_after_successful_wiki_publish": true,
  "midday_compaction_threshold_chars": 30000
}
```

Keep machine-specific paths and toggles in `private/`.

## 14. Implementation Phases

### Phase 0: Document and Validate

Deliverables:

- this design plan;
- review against wrapper/audit mode boundaries;
- confirm source routing policy;
- confirm notepad reset behavior with the daily wiki pipeline.

No runtime changes.

### Phase 1: External Left-Brain Tools

Deliverables:

- `scripts/dual_brain/build_fyi.py`
- `scripts/dual_brain/update_notepad.py`
- `scripts/dual_brain/check.py`
- `scripts/dual_brain/reset_after_wiki_publish.py`
- `private/dual_brain_config.json` sample or documented schema
- per-agent notepad files under `workspaces/<agent_id>/continuity/`

Requirements:

- LLM-based left brain from the first product implementation;
- deterministic code handles file IO, locking, schema validation, and
  diagnostics;
- the left brain may use memory read/write/search tools;
- the left brain must not use execution/task tools reserved for the right brain;
- verbose logging;
- dry-run/check modes;
- no HASHI core runtime edits;
- source paths, memory tools used, and selected wiki topics printed in
  diagnostic output;
- non-zero exit codes on failure.

### Phase 2: Manual Operator Workflow

Use the tools manually:

```text
build FYI -> paste/attach to right-brain request -> update notepad after result
```

Validate:

- FYI is sufficient without flooding the right brain;
- right brain uses context without being overloaded;
- notepad stays useful for one day;
- wiki recall is relevant;
- reset after wiki publish is safe.

### Phase 3: Sidecar or Existing Runner Integration

Add a sidecar or runner that can:

- receive a prompt and agent id;
- call `build_fyi.py`;
- forward original prompt + FYI to an execution path;
- call `update_notepad.py` after completion.

Prefer an existing command runner or sidecar service before changing runtime
core.

### Phase 4: Runtime Mode Proposal

Only after Phase 1-3 are proven with the real product path, consider a runtime
mode:

```text
/mode dual-brain
```

This phase may require core runtime integration. It should not be implemented
without explicit approval.

If approved, it must cover:

- normal foreground completion;
- background completion;
- retry semantics;
- hchat semantics;
- verbose trace visibility;
- failure fallback;
- state preservation.

### Phase 5: Daily Wiki Integration

Add a post-publish hook or cron-adjacent task:

- verify latest wiki publish succeeded;
- archive current notepad;
- start fresh notepad;
- record publish id and reset time;
- do not reset if wiki publish failed.

Prefer external cron/task configuration over core runtime changes.

## 15. Failure and Fallback Rules

If left brain fails before execution:

- right brain should still receive the original user prompt;
- the request should not be blocked;
- failure should be logged;
- user-facing output may mention context preflight failure only when useful.

If wiki recall fails:

- continue with notepad-only FYI;
- log the wiki error.

If notepad update fails after execution:

- do not alter the user-facing answer;
- log the failure;
- allow retry of `update_notepad.py`.

If notepad grows too large:

- run compaction;
- if compaction fails, keep most recent notes and archive older notes;
- never silently delete without logging.

## 16. Security and Privacy

- Do not store tokens or secrets in the notepad unless the existing local
  private memory policy explicitly allows it.
- Keep machine-specific config in `private/`.
- Do not publish continuity notepads directly into human-written Obsidian
  notes.
- Daily wiki publishing must continue to use generated zones only.
- FYI context should not expose irrelevant private/personal notes to the right
  brain.

## 17. Testing Plan

Unit-style tests for external tools:

- missing config path;
- missing notepad path;
- empty notepad;
- large notepad;
- wiki unavailable;
- no relevant wiki result;
- prompt with file paths;
- prompt with agent names;
- after-action update appends expected JSONL;
- reset after publish archives notepad and creates a fresh one;
- failed publish does not reset.

Scenario tests:

- same topic after 10 minutes;
- new topic after same-day unrelated work;
- hchat handoff followed by user request;
- post-wiki task that has not reached the wiki yet;
- wiki-known old project with no same-day continuity;
- contradictory user prompt where original prompt must still pass unchanged.

## 18. Open Decisions

- Name of the runtime-facing mode or command: `dual-brain`, `context`, or
  another operator-facing term.
- Whether hchat should trigger full left-brain preflight or only notepad
  recording in v1.
- Which left-brain LLM backend/model should be used by default.
- Which memory read/write tools the left brain should receive in the first
  product implementation.
- Whether left brain should use embeddings directly, wiki index/topic search,
  or both.
- Exact archive/reset handshake with Lily's daily wiki pipeline.
- How much final result text should be shown to `update_notepad.py` after a
  long execution turn.

## 19. Recommended Product Build

Build the real dual-brain product path as external tools first:

1. `build_fyi.py` runs an LLM-based left brain with memory read/search access to
   the agent notepad and wiki material, then writes a sufficient FYI block.
2. `update_notepad.py` appends concise same-day continuity after a meaningful
   turn.
3. `check.py` prints config, notepad size, latest wiki publish id, and health.
4. `reset_after_wiki_publish.py` archives and resets notepad only after a
   successful wiki publish.

This proves the real dual-brain structure without changing HASHI core. Runtime
integration can be designed after the external left-brain behavior is stable.

## 20. Detailed Step-by-Step Implementation Plan

This section is the execution sequence for building the real dual-brain product.
The order is intentional: establish state contracts first, then build left-brain
tools, then integrate delivery, then automate the daily reset.

### Phase A: Groundwork and Safety Boundaries

Goal: prepare the repository and local state layout without changing runtime
core.

Steps:

1. Confirm and, if needed, merge `.gitignore` coverage before creating any
   continuity files:
   - `workspaces/*/continuity/`
   - generated FYI artifacts
   - local dual-brain diagnostics if they may include private context
2. Run `git status --short` before creating continuity state and verify no
   continuity path is already tracked or staged.
3. Add a documented config schema for `private/dual_brain_config.json`.
4. Create `scripts/dual_brain/` as the isolated implementation folder.
5. Define common library modules:
   - `paths.py` for resolving HASHI root, workspace, wiki root, and notepad
     paths;
   - `config.py` for loading private config with clear errors;
   - `logging_utils.py` for consistent diagnostic output;
   - `atomic_io.py` for append-with-lock and rewrite-with-rename operations;
   - `memory_read_tools.py` for path-whitelisted left-brain read/search tools.
6. Define fixed continuity subpaths in `paths.py`:
   - `current_notepad.jsonl`;
   - `current_notepad.md`;
   - `notepad_status.json`;
   - `pending_updates/`;
   - `.turn_active.lock`;
   - `artifacts/`;
   - `archived/`.
7. Add a `check.py` command first, before other behavior:
   - print mode;
   - print config path;
   - print enabled agents;
   - print workspace path;
   - print continuity directory path and writable status;
   - print latest wiki manifest path and publish id;
   - print `notepad_status.json` values;
   - print notepad entry count and character count;
   - print pending update count;
   - print active turn lock status;
   - print whether `.gitignore` appears to protect continuity files;
   - return non-zero on unsafe state.

Acceptance checks:

- `check.py --agent lily` gives enough output to diagnose setup remotely.
- Missing config, missing wiki root, and unwritable continuity directory fail
  loudly.
- No runtime core file is touched.

### Phase B: Continuity Notepad Product Contract

Goal: make same-day continuity a reliable state store.

Steps:

1. Make JSONL the source of truth:
   - `current_notepad.jsonl` is authoritative;
   - `current_notepad.md` is generated for human reading only.
2. Implement notepad event schema validation.
3. Implement `update_notepad.py`:
   - accepts `--agent`, `--kind`, `--summary`, optional related paths/topics,
     and optional `--request-id`;
   - can also accept a right-brain result file for left-brain assisted
     summarization;
   - appends normal events using append + advisory lock;
   - records `last_successful_update_at` in `notepad_status.json`;
   - prints every resolved path and final status.
4. Implement `render_notepad_md.py`:
   - reads JSONL;
   - produces readable markdown;
   - never becomes the source of truth.
5. Add lock handling:
   - prevent reset while `update_notepad.py` is writing;
   - allow read during write only when a stable previous file exists.
6. Implement separate atomic IO operations:
   - append event: append + `fcntl.flock`;
   - rewrite/compact/reset/archive: write tmp file, fsync where practical, then
     rename.
7. Keep `notepad_status.json` small and fast to read:
   - `last_successful_update_at`;
   - `entry_count`;
   - `total_chars`;
   - `last_event_id`;
   - `updated_at`.

Acceptance checks:

- concurrent read/write does not corrupt JSONL;
- malformed events are rejected with useful errors;
- markdown regeneration can be deleted and recreated from JSONL;
- check output warns if no successful notepad write occurred despite activity.

### Phase C: LLM-Based Left Brain FYI Builder

Goal: build the real left brain as a constrained LLM memory/context model.

Steps:

1. Implement memory tool interfaces for the left brain:
   - read current notepad;
   - read latest wiki manifest;
   - inspect wiki index and topic files;
   - inspect configured same-day event sources;
   - write FYI artifact and diagnostics.
   - enforce path allowlists for every read/search operation.
2. Implement `build_fyi.py`:
   - accepts `--agent`, `--prompt-file`, `--source`, `--request-id`, and
     `--mode`;
   - reads the original prompt but never rewrites it;
   - gives the left-brain LLM memory/context tools only;
   - forbids execution/task tools;
   - asks the left brain to select sufficient FYI context for the right brain;
   - writes both markdown FYI and JSON FYI artifact;
   - logs memory tools used, wiki pages inspected, notepad entries considered,
     and output size.
3. Define the left-brain system prompt:
   - "You are the past-facing memory/context brain."
   - "Do not rewrite or reinterpret the user's prompt."
   - "Do not plan or execute the task."
   - "Only provide FYI context needed for the right brain."
   - "Use wiki and notepad memory as needed."
   - "If nothing useful is found, say so clearly."
4. Define FYI output schema:
   - same-day continuity;
   - relevant wiki memory;
   - open continuity notes;
   - useful background;
   - source pointers;
   - warnings only when operationally relevant.
5. Add FYI validation:
   - original prompt hash must match input;
   - FYI must include the non-override disclaimer;
   - FYI must not contain tool execution instructions for the right brain;
   - output must be parseable if JSON mode is requested.
6. Enforce memory read boundaries:
   - allow `workspaces/<agent_id>/continuity/`;
   - allow configured `wiki_root/`;
   - allow configured `same_day_event_sources`;
   - deny arbitrary paths, `private/secrets.json`, SSH directories, and system
     paths unless explicitly whitelisted for memory use.

Acceptance checks:

- prompts with no relevant history produce a small "no useful prior context"
  FYI instead of junk;
- prompts with same-day continuity include it;
- prompts needing old project memory trigger wiki recall;
- contradictory or sudden user prompt still passes unchanged;
- left brain diagnostics show exactly what memory sources were consulted.

### Phase D: Right-Brain Handoff Contract

Goal: define how the original prompt and FYI reach the execution model without
prompt rewriting.

Steps:

1. Define the handoff envelope:

   ```markdown
   <left_brain_fyi>
   ...
   </left_brain_fyi>

   <original_user_prompt>
   ...
   </original_user_prompt>
   ```

2. Ensure the original prompt is byte-for-byte preserved inside the envelope.
3. Add `handoff_preview.py`:
   - takes prompt + FYI artifact;
   - renders the final right-brain input;
   - prints prompt hash and FYI hash;
   - supports `--check-only`.
4. Use this first product injection path:
   - direct right-brain backend runner from the sidecar;
   - the sidecar renders the FYI + original prompt handoff envelope;
   - the sidecar invokes the configured right-brain CLI/backend adapter;
   - this avoids HASHI core edits while proving dual-brain cognition.
5. Keep these non-goals for the first product path:
   - do not inject through Telegram/HChat delivery;
   - do not require Workbench API `extra_context`;
   - do not modify runtime message routing.
6. Record handoff metadata:
   - request id;
   - agent id;
   - prompt hash;
   - FYI artifact path;
   - right-brain backend/model if known.

Acceptance checks:

- handoff preview proves original prompt preservation;
- FYI remains clearly labeled as background;
- sidecar path does not require editing central runtime files;
- failure to build FYI falls back to original prompt execution.

### Phase E: After-Action Update

Goal: ensure the left brain keeps continuity current after every meaningful
turn.

Steps:

1. Add `after_action.py`:
   - accepts right-brain final answer, optional tool summary, request metadata,
     and original prompt hash;
   - invokes the left-brain LLM to decide what belongs in same-day continuity;
   - writes one or more notepad events through `update_notepad.py`.
   - accepts `--result-max-chars`, defaulting to the configured
     `after_action_input_max_chars`;
   - marks truncated inputs explicitly in pending artifacts and notepad events.
2. Define what should be remembered:
   - completed actions;
   - changed files or state;
   - promises made to the user;
   - handoffs sent or received;
   - unresolved blockers;
   - important user decisions.
3. Define what should not be remembered:
   - transient filler;
   - raw tool logs unless explicitly important;
   - secrets;
   - long evidence dumps already available elsewhere.
4. Add retry support:
   - if after-action update fails, write a pending update artifact;
   - `check.py` warns about pending updates.
5. Store pending update artifacts in:
   - `workspaces/<agent_id>/continuity/pending_updates/{request_id}.pending.json`

Acceptance checks:

- meaningful turns add useful notepad events;
- trivial turns do not bloat notepad;
- failed updates are visible and retryable;
- after-action update never changes the already delivered user answer.

### Phase F: Wiki Sleep/Reset Integration

Goal: connect daily wiki publishing to continuity reset safely.

Steps:

1. Add a structured wiki success marker reader:
   - read latest publish manifest;
   - confirm publish id;
   - confirm generated zones only;
   - confirm summary status is usable.
2. Implement `reset_after_wiki_publish.py`:
   - reads latest successful wiki marker/manifest itself;
   - refuses stale or failed publish states;
   - checks notepad locks and running-turn markers;
   - archives current JSONL and markdown render;
   - starts a fresh notepad;
   - writes reset manifest.
3. Add safe-window behavior:
   - if a right-brain turn is active, defer reset;
   - do not delete continuity under active execution.
4. Add cron/task integration externally:
   - run after successful Lily wiki daily update;
   - no core runtime edits required.

Acceptance checks:

- failed wiki publish never resets notepad;
- stale publish marker is rejected;
- active turn delays reset;
- reset creates archive and fresh notepad with clear manifest.

### Phase G: Thin Sidecar Product Path

Goal: automate the full dual-brain flow without yet creating a runtime mode.

Steps:

1. Implement `run_dual_brain_turn.py`:
   - receive agent id and prompt;
   - call `build_fyi.py`;
   - render handoff envelope;
   - forward to an existing execution path or command runner;
   - capture right-brain result;
   - call `after_action.py`;
   - print all artifact paths and statuses.
2. Own the turn-active lock lifecycle:
   - create `continuity/.turn_active.lock` before right-brain execution;
   - include `pid`, `started_at`, `request_id`, `agent_id`, and backend/model;
   - remove the lock after right-brain execution and after-action handling;
   - preserve stale lock diagnostics for `check.py`.
3. Add check/dry-run:
   - `--check-only` validates paths and config;
   - `--dry-run` builds FYI and handoff without executing right brain.
4. Add failure fallback:
   - if left brain fails, forward original prompt only;
   - if right brain fails, still record failure context when useful;
   - if after-action fails, preserve pending update artifact.
5. Add observability:
   - one turn manifest per request;
   - logs for left-brain call, handoff, right-brain call, and after-action.

Acceptance checks:

- complete turn works end-to-end for one test agent;
- all artifacts are inspectable;
- no central runtime file is modified;
- privacy-sensitive outputs remain under ignored workspace/private paths.

### Phase H: Runtime Mode Design Gate

Goal: decide whether `/mode dual-brain` is worth implementing in core.

Steps:

1. Review sidecar results.
2. Confirm injection path requirements that cannot be satisfied externally.
3. Document exactly which runtime files would need changes.
4. Explain restart/reload requirements.
5. Provide rollback plan.
6. Ask for explicit approval before touching core.

Acceptance checks:

- no core edits happen without approval;
- external alternatives are documented;
- runtime integration scope is small and reversible.

## 21. First Build Order

Build in this exact order:

1. `.gitignore` privacy coverage for continuity paths.
2. `scripts/dual_brain/check.py`.
3. `private/dual_brain_config.json` schema/sample.
4. JSONL notepad source-of-truth tools.
5. `notepad_status.json` and pending update paths.
6. Memory read/search tool allowlists.
7. `build_fyi.py` with LLM-based left brain and memory tools.
8. FYI artifact validation.
9. handoff preview with original prompt hash check.
10. direct right-brain backend runner path for sidecar handoff.
11. `after_action.py` for continuity update with result input bounds.
12. turn-active lock lifecycle.
13. wiki reset script and archive manifest.
14. thin sidecar `run_dual_brain_turn.py`.
15. external cron/task hook for post-wiki reset.
16. runtime mode proposal only if the sidecar path proves insufficient.

## 22. Resolved Technical Decisions Before Coding

These decisions close the second-pass technical review issues.

### 22.1 First Injection Path

The first product path uses a direct right-brain backend runner controlled by
the sidecar.

Rationale:

- no HASHI core runtime edit is required;
- the original prompt can be preserved inside a handoff envelope;
- left-brain and after-action behavior can be tested end to end;
- runtime integration can remain a later explicit approval gate.

Tradeoff:

- this path does not exercise Telegram/HChat routing, retry, or runtime memory
  persistence. Those remain Phase H concerns.

### 22.2 JSONL Write Modes

Normal event append:

- append to `current_notepad.jsonl`;
- use `fcntl.flock`;
- update `notepad_status.json` atomically after successful append.

Rewrite operations:

- compaction;
- reset;
- archive;
- markdown render.

Rewrite operations use tmp + rename and never share the append path.

### 22.3 Fast Status File

`notepad_status.json` is the fast health marker for `check.py`.

Required fields:

```json
{
  "last_successful_update_at": "2026-05-15T10:20:00+10:00",
  "entry_count": 12,
  "total_chars": 4820,
  "last_event_id": "evt_...",
  "updated_at": "2026-05-15T10:20:00+10:00"
}
```

### 22.4 Left-Brain Read Boundary

Every read/search tool exposed to the left-brain LLM must enforce path
allowlists. Initial allowed roots:

- `workspaces/<agent_id>/continuity/`;
- configured `wiki_root/`;
- configured `same_day_event_sources`.

Anything outside these roots is denied by the tool wrapper before content is
returned to the model.

### 22.5 After-Action Input Bound

`after_action.py` accepts `--result-max-chars`. Config key:

```json
{
  "after_action_input_max_chars": 8000
}
```

If the result is truncated, the artifact records truncation explicitly.

### 22.6 Turn-Active Lock

`run_dual_brain_turn.py` owns:

```text
workspaces/<agent_id>/continuity/.turn_active.lock
```

Minimum lock content:

```json
{
  "pid": 12345,
  "started_at": "2026-05-15T10:20:00+10:00",
  "request_id": "req_...",
  "agent_id": "lily",
  "backend": "codex-cli",
  "model": "gpt-5.5"
}
```

Reset scripts must defer when this lock is present and fresh.

### 22.7 Pending Updates

Failed after-action updates are stored at:

```text
workspaces/<agent_id>/continuity/pending_updates/{request_id}.pending.json
```

`check.py` must report pending update count and latest pending update time.

## 23. Current Sidecar Implementation (2026-05-15)

Implemented without HASHI core runtime edits:

- Shared helper: `scripts/dual_brain_common.py`
- Script: `scripts/dual_brain_context.py`
- Turn runner: `scripts/run_dual_brain_turn.py`
- Config: `private/dual_brain_config.json`
- Tests: `tests/test_dual_brain_sidecar.py`

Available commands:

```bash
python3 scripts/dual_brain_context.py --agent lily diagnose
python3 scripts/dual_brain_context.py --agent lily preflight --prompt "..."
python3 scripts/dual_brain_context.py --agent lily after-action --prompt "..." --result "..."
python3 scripts/run_dual_brain_turn.py --agent lily --check --prompt "..."
python3 scripts/run_dual_brain_turn.py --agent lily --prompt "..."
```

Behavior:

- `diagnose`: prints mode, config path, resolved backend/model, and resolved
  continuity/wiki paths. It also reports whether generated sidecar paths are
  protected by `.gitignore`.
- `preflight`: first reads the full same-day continuity notebook and calls the
  left-brain LLM. The implementation must not silently truncate by entry count.
  Wiki candidates are not loaded or injected on every turn. If the first pass
  returns `wiki_needed: true`, a second wiki-recall pass retrieves candidates and
  lets the left brain produce final FYI. It writes:
  - `workspaces/<agent>/memory/left_brain_artifacts/left_brain_preflight_latest.json`
  - `workspaces/<agent>/memory/left_brain_artifacts/left_brain_fyi_latest.md`
  - `workspaces/<agent>/memory/left_brain_artifacts/left_brain_events.jsonl`
- `after-action`: summarizes right-brain result into continuity update, appends
  to:
  - `workspaces/<agent>/memory/left_brain_continuity.jsonl`
  only when the updater returns `should_write: true`, so routine turns can be
  skipped without polluting the notebook,
  and writes:
  - `workspaces/<agent>/memory/left_brain_artifacts/left_brain_after_action_latest.json`
  - `workspaces/<agent>/memory/left_brain_artifacts/left_brain_events.jsonl`
- `reset_dual_brain_notepads.py`: archives and clears all agents'
  `left_brain_continuity.jsonl` files after successful wiki publish. The active
  daily wiki pipeline calls this automatically after `--publish-vault` succeeds.
  The reset does not run on dry-run, rollback, or failed wiki publish.
  Each reset also writes a JSONL audit event to
  `workspaces/lily/logs/dual_brain_notepad_reset.jsonl` in every configured
  HASHI root, including every checked agent and its `cleared`/`skipped` status.
- `run_dual_brain_turn.py`: runs preflight, right-brain execution, and
  after-action as one external turn with `--check`, atomic lock creation, stale
  lock cleanup, pending update artifacts, and long prompt/result temp-file
  handoff.

Review hardening completed:

- backend/model resolution reads `workspaces/<agent>/state.json` and supports
  optional `left_brain_backend`/`left_brain_model` and
  `right_brain_backend`/`right_brain_model` config overrides;
- JSONL continuity appends use `flock`;
- after-action LLM input is truncated by `after_action_result_max_chars`;
- long prompt/result subprocess handoff uses temp files after
  `prompt_inline_max_chars`;
- JSON extraction finds the first balanced object instead of slicing from first
  `{` to last `}`;
- wiki recall reads configured generated vault roots from `wiki_roots` instead
  of the local dry-run wiki by default;
- if after-action fails after right-brain success, the user still receives the
  right-brain result and a pending update artifact is written.
- `/verbose` and `/think` apply to the right-brain foreground generation path.
  Left-brain sidecar calls remain silent and are audited separately through
  `left_brain_events.jsonl` and sidecar token-audit records.
- `/token` includes right-brain foreground usage and sidecar left-brain usage.
  Runtime sidecar calls are counted in the active runtime session and are
  identifiable in `token_audit.jsonl` with `completion_path=sidecar`. Direct
  sidecar scripts may fall back to a standalone `sidecar` session id.

This is a runnable external sidecar path that works without runtime core
changes.

## 24. Runtime Product Integration

Dual-brain is now a HASHI runtime mode:

```text
/mode dual-brain
```

Aliases:

```text
/mode dualbrain
/mode brain
```

Runtime behavior:

- left brain runs before each normal user turn as a pre-turn context provider;
- left brain emits an FYI context section only;
- right brain remains the normal HASHI runtime execution path;
- active `/sys` slots stay in the normal right-brain system prompt path;
- after a successful right-brain response, left brain records an after-action
  continuity update in the background;
- no HASHI process restart is needed after switching mode, but a `/reboot` is
  enough to load the feature code after deployment.

The runtime integration is backed by:

- `orchestrator/dual_brain_mode.py`
- `orchestrator/runtime_mode.py`
- `/brain` command and `bcfg:` callback menu wiring.

## 25. Dual-Brain Configuration Manual

### 25.1 Switch Mode

```text
/mode dual-brain
```

To leave dual-brain mode:

```text
/mode flex
```

### 25.2 Open The Configuration Menu

```text
/brain
```

The menu exposes:

- left-brain backend selection, then model selection;
- right-brain backend selection, then model selection;
- prompt configuration instructions;
- refresh/status.

Model selection must be a two-stage menu:

```text
/brain
  -> Left brain | Right brain
      -> backend list from this agent's allowed_backends
          -> all registered HASHI models for the selected backend
```

The backend screen stays short. The model screen only shows models for the
selected backend. Callback data should use backend + model index rather than the
full model id, because OpenRouter model ids can exceed Telegram callback limits.

### 25.3 Configure Models By Command

Left brain:

```text
/brain left backend=<backend> model=<model>
```

Right brain:

```text
/brain right backend=<backend> model=<model>
```

Example:

```text
/brain left backend=codex-cli model=gpt-5.4-mini
/brain right backend=codex-cli model=gpt-5.5
```

If right brain is changed while dual-brain mode is active, HASHI switches the
active backend/model immediately.

### 25.4 Configure Prompt Controls

Memory briefing prompt:

```text
/brain prompt memory <text>
```

Notepad update prompt:

```text
/brain prompt notepad <text>
```

Show a prompt:

```text
/brain prompt memory show
/brain prompt notepad show
```

Clear a prompt:

```text
/brain prompt memory clear
/brain prompt notepad clear
```

Aliases remain supported for compatibility:

```text
left = memory
after = notepad
```

Prompt semantics:

- memory prompt controls the pre-turn FYI context generator;
- notepad prompt controls the continuity update call after a successful turn;
- `/sys` is unchanged and still belongs to the right-brain runtime system prompt
  path via `workspaces/<agent>/sys_prompts.json`.
- right-brain execution instructions are handled by `agent.md` and `/sys`, not
  by dual-brain prompt controls.
