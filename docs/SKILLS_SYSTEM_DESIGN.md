# Skills System Design — Bridge-U-F
## Revised Design (2026-03-10)

A **skill** is a reusable, invokable capability module. Skills are invoked explicitly by the user
via Telegram commands, referenced in cron/heartbeat job definitions, or toggled on to persist
across a session. There is no magic activation — skills are always user-driven.

---

## Design Principles

1. **Explicit over automatic.** Skills never activate on their own. The user invokes them.
2. **Markdown-first.** Every skill's definition is a `skill.md` file readable by any model.
3. **File-based, git-friendly.** Skills live in the filesystem — diffable, versionable, reviewable.
4. **Universal unit of automation.** Cron tasks and heartbeat jobs invoke skills. Skills are the
   action layer for all scheduled work.
5. **Backend-agnostic.** Skills work across Claude, Gemini, Codex, OpenRouter.

---

## Skill Types

Three types based on behavior:

| Type | Command Pattern | Behavior |
|------|----------------|----------|
| **Action** | `/skill restart_pc` | One-shot execution, runs once and done |
| **Prompt** | `/skill codex fix the login bug` | Passes the user's prompt to a skill-specific handler |
| **Toggle** | `/skill TTS on` / `/skill TTS off` | Stays active until explicitly turned off |

- **Action skills** run a script or system command and report back.
- **Prompt skills** route the user's input to a specific backend, tool, or workflow. The text after
  the skill name is the prompt.
- **Toggle skills** inject their instructions into the prompt context while `on`, persist in session
  state, and stop injecting when turned `off`. This is the only case where skill content enters the
  prompt automatically — and it's always user-initiated.

---

## Folder Structure

```
bridge-u-f/
  skills/
    restart_pc/
      skill.md              # type: action, description, run script
      run.py                # or run.sh — executed on invocation
    codex/
      skill.md              # type: prompt, routes to codex CLI backend
    TTS/
      skill.md              # type: toggle, injects TTS instructions while on
    cron/
      skill.md              # type: action, built-in: list/manage cron jobs
    heartbeat/
      skill.md              # type: action, built-in: list/manage heartbeat tasks
    carbon-accounting/
      skill.md              # type: toggle, injects carbon accounting expertise
      standards/
        ghg-protocol-summary.md
        iso14064-notes.md
    academic-writing/
      skill.md              # type: toggle, injects academic writing instructions
    code-review/
      skill.md              # type: prompt, runs code review workflow
```

Each skill folder contains:
- **`skill.md`** (required) — frontmatter + skill definition/instructions
- **`run.py` / `run.sh`** (action skills) — script executed on invocation
- **Supporting files** (optional) — reference docs, templates, data files

---

## Skill Definition Format (`skill.md`)

Frontmatter is minimal — no keywords, no priorities, no token budgets.

```markdown
---
id: restart_pc
name: Force Restart PC
type: action
description: Force restart the local machine immediately
run: run.py
---

Executes a forced system restart. Use with care — unsaved work will be lost.
```

```markdown
---
id: codex
name: Codex CLI
type: prompt
description: Run a task using Codex CLI as the backend
backend: codex-cli
---

Routes your prompt directly to the Codex CLI backend with full file access.
Use for coding tasks that benefit from Codex's tool use and file editing.
```

```markdown
---
id: TTS
name: Text to Speech
type: toggle
description: Enable spoken audio responses via TTS pipeline
---

When TTS is on, all responses are also sent as voice messages.
- Use natural, speakable language — avoid bullet lists and code blocks in main response
- Keep responses under 200 words when TTS is active
- Emit a text response first, then trigger TTS pipeline
```

```markdown
---
id: carbon-accounting
name: Carbon Accounting Expert
type: toggle
description: Activate deep carbon accounting expertise (GHG Protocol, ISO 14064)
---

You now have deep expertise in carbon accounting and GHG reporting.

## Standards
- GHG Protocol Corporate Standard — default framework for Scope 1, 2, 3
- ISO 14064-1:2018 — organizational-level GHG quantification
- TCFD for climate-related financial disclosure

## When answering carbon questions
- Always distinguish Scope 1 (direct), Scope 2 (purchased energy), Scope 3 (value chain)
- Use tCO2e as standard unit unless user specifies otherwise
- State emission factor source and vintage year
- Distinguish location-based vs market-based Scope 2
- Flag data quality: primary vs secondary data

## Reference files in this skill folder
- `standards/ghg-protocol-summary.md`
- `standards/iso14064-notes.md`
```

### Frontmatter Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes | Unique identifier, matches folder name |
| `name` | string | Yes | Human-readable display name |
| `type` | enum | Yes | `action`, `prompt`, `toggle` |
| `description` | string | Yes | One-line description shown in `/skill help` |
| `run` | string | Action only | Script filename to execute (`run.py`, `run.sh`) |
| `backend` | string | Prompt only | Backend to route to (`codex-cli`, `claude-cli`, etc.) |

---

## Command Interface

```
/skill                          → Telegram inline button grid of all skills
/skill help                     → List all skills: name + type + description
/skill <name>                   → Action: run it | Toggle: show current state | Prompt: show usage
/skill <name> <prompt>          → Prompt skill: run with this input
/skill <name> on                → Toggle skill on (persists until off)
/skill <name> off               → Toggle skill off
/skill cron                     → List all cron jobs with management buttons
/skill heartbeat                → List all heartbeat tasks with management buttons
```

### `/skill` with no arguments

Shows an inline keyboard grid — one button per skill, grouped by type:

```
━━━ Actions ━━━
[restart_pc]  [backup_workspace]  [system_status]

━━━ Toggles ━━━
[TTS • off]  [carbon-accounting • off]  [academic-writing • off]

━━━ Prompt Skills ━━━
[codex]  [gemini]  [code-review]

━━━ Jobs ━━━
[cron]  [heartbeat]
```

Tap a skill to get its info and available actions as a follow-up button menu.

---

## Integration with Cron and Heartbeat

Cron tasks and heartbeat jobs reference skills as their action:

```json
{
  "id": "daily-backup",
  "schedule": "0 3 * * *",
  "action": "skill:backup_workspace",
  "args": ""
}
```

```json
{
  "id": "morning-briefing",
  "schedule": "0 8 * * *",
  "action": "skill:briefing",
  "args": "include weather, markets, and pending tasks"
}
```

- `skill:` prefix routes the scheduler to `SkillManager.invoke(skill_id, args)`
- Action skills run their associated script
- Prompt skills route `args` as the prompt to the configured backend
- Toggle skills cannot be used in cron (they require session state)

This means cron and heartbeat don't need their own action system — skills are the action layer.

---

## Toggle Skills: Prompt Injection

When a toggle skill is `on`, its `skill.md` body (below the frontmatter) is appended to the
`--- ACTIVE SKILLS ---` section of the prompt. This is the only mechanism by which skill content
enters the prompt.

Prompt structure with toggle skills active:

```
Bridge-managed context follows...

--- SYSTEM IDENTITY ---
{agent.md contents}

--- ACTIVE SKILLS ---
## [carbon-accounting] Carbon Accounting Expert
You now have deep expertise in carbon accounting and GHG reporting.
...

## [TTS] Text to Speech
When TTS is on, all responses are also sent as voice messages.
...

--- RELEVANT LONG-TERM MEMORY ---
{retrieved memories}

--- RECENT CONTEXT ---
{last N turns}

--- NEW REQUEST ---
{user message}
```

Active toggle skills are stored in session state (e.g., `state.json`) under the agent's entry:

```json
{
  "active_skills": ["carbon-accounting", "TTS"]
}
```

They persist across messages in the same session. They are cleared on `/stop`, `/restart`, or
explicit `/skill <name> off`.

---

## Built-in Skills: `cron` and `heartbeat`

These are **action skills** with no `run.py` — they are handled natively by the runtime.

`/skill cron` output:

```
📅 Cron Jobs (3 active)

1. daily-backup — runs at 03:00 daily
   Last run: 2026-03-09 03:00 ✓
   [Pause] [Edit] [Delete]

2. morning-briefing — runs at 08:00 daily
   Last run: 2026-03-10 08:00 ✓
   [Pause] [Edit] [Delete]

3. weekly-report — runs every Monday 09:00
   Last run: 2026-03-09 09:00 ✓
   [Pause] [Edit] [Delete]

[+ Add Job]
```

`/skill heartbeat` shows the same format for heartbeat tasks.

---

## New Module: `orchestrator/skill_manager.py`

```python
@dataclass
class Skill:
    id: str
    name: str
    type: str           # "action", "prompt", "toggle"
    description: str
    content: str        # skill.md body (below frontmatter)
    path: Path
    run: str | None     # script filename for action skills
    backend: str | None # backend id for prompt skills

class SkillManager:
    def __init__(self, skills_dir: Path):
        """Scan skills/ dir, load all skill.md files."""

    def list_skills(self) -> list[Skill]:
        """Return all loaded skills."""

    def get(self, skill_id: str) -> Skill | None:
        """Return a skill by ID."""

    def invoke_action(self, skill: Skill, args: str) -> str:
        """Run an action skill's script, return output."""

    def route_prompt(self, skill: Skill, prompt: str, session) -> Coroutine:
        """Route a prompt skill to its configured backend."""

    def render_active_skills(self, active_ids: list[str]) -> str:
        """Render the --- ACTIVE SKILLS --- section for toggle skills."""

    def build_skill_keyboard(self, active_ids: list[str]) -> InlineKeyboardMarkup:
        """Build Telegram inline keyboard for /skill command."""
```

---

## Telegram Command Implementation

Add `cmd_skill` to both `agent_runtime.py` and `flexible_agent_runtime.py`:

```python
async def cmd_skill(self, update: Update, context: Any):
    args = context.args or []

    if not args:
        # Show inline keyboard grid
        kb = self.skill_manager.build_skill_keyboard(self.session.active_skills)
        await update.message.reply_text("Skills:", reply_markup=kb)
        return

    if args[0] == "help":
        # List all skills with type + description
        ...
        return

    skill_name = args[0]
    skill = self.skill_manager.get(skill_name)

    if not skill:
        await update.message.reply_text(f"Unknown skill: {skill_name}")
        return

    if skill.type == "action":
        result = await self.skill_manager.invoke_action(skill, " ".join(args[1:]))
        await update.message.reply_text(result)

    elif skill.type == "prompt":
        prompt = " ".join(args[1:])
        if not prompt:
            await update.message.reply_text(f"Usage: /skill {skill_name} <your prompt>")
            return
        await self.skill_manager.route_prompt(skill, prompt, self)

    elif skill.type == "toggle":
        if len(args) > 1 and args[1] in ("on", "off"):
            state = args[1]
            if state == "on":
                self.session.active_skills.add(skill_name)
            else:
                self.session.active_skills.discard(skill_name)
            self._save_session_state()
            await update.message.reply_text(f"{skill.name}: {state}")
        else:
            # Show current state + on/off buttons
            current = "on" if skill_name in self.session.active_skills else "off"
            await update.message.reply_text(
                f"{skill.name} is currently **{current}**",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Turn On", callback_data=f"skill:{skill_name}:on"),
                    InlineKeyboardButton("Turn Off", callback_data=f"skill:{skill_name}:off"),
                ]])
            )
```

---

## File Changes Summary

| File | Change |
|------|--------|
| `skills/` (new dir) | Skill library — one folder per skill |
| `orchestrator/skill_manager.py` (new) | `SkillManager` class |
| `orchestrator/bridge_memory.py` | `BridgeContextAssembler.build_prompt()` — inject `--- ACTIVE SKILLS ---` from active toggles |
| `orchestrator/flexible_agent_runtime.py` | Add `cmd_skill`, initialize `SkillManager`, wire toggle state |
| `orchestrator/agent_runtime.py` | Same for fixed agents |
| `orchestrator/scheduler.py` | Route `skill:` prefixed actions through `SkillManager` |

---

## Starter Skills to Build

| Skill ID | Type | Description |
|----------|------|-------------|
| `codex` | prompt | Route to Codex CLI backend |
| `gemini` | prompt | Route to Gemini CLI backend |
| `claude` | prompt | Route to Claude CLI backend |
| `restart_pc` | action | Force restart the local machine |
| `system_status` | action | Report CPU, memory, disk, uptime |
| `TTS` | toggle | Enable text-to-speech voice responses |
| `carbon-accounting` | toggle | GHG Protocol + ISO 14064 expertise |
| `academic-writing` | toggle | Formal academic writing mode |
| `cron` | action (built-in) | List and manage cron jobs |
| `heartbeat` | action (built-in) | List and manage heartbeat tasks |

---

## What This Doesn't Do (Intentional Scope Limits)

- **No auto-activation.** Skills never trigger on keywords or heuristics. User-driven only.
- **No runtime code execution from the prompt pipeline.** Action skills run scripts; the model
  doesn't execute arbitrary code from skill definitions.
- **No inter-skill dependencies.** Skills are independent. If you need two skills together,
  activate both or combine them.
- **No remote skill registry.** Skills are local files. Sharing is via git.

---

## Implementation Order

| Step | What | Risk |
|------|------|------|
| 1 | Create `skills/` dir, write starter `skill.md` files | None |
| 2 | `SkillManager` — load, list, get, render_active_skills | None |
| 3 | Integrate toggle injection into `BridgeContextAssembler.build_prompt()` | Low — additive |
| 4 | `cmd_skill` in both runtimes | Low |
| 5 | Action skill execution (`invoke_action`) | Low |
| 6 | Prompt skill routing (`route_prompt`) | Medium |
| 7 | Scheduler `skill:` prefix routing | Low |
| 8 | Inline keyboard (`build_skill_keyboard`) | Low |
