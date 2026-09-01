# Tools

## Universal Multi-Agent Telegram Orchestrator (`bridge-u-f`)
The `bridge-u-f` project located at `<project_root>` is a local multi-agent bridge that connects Telegram bots (and optionally WhatsApp) to multiple AI backends, with an optional browser workbench.

Two working modes are available to a Flex Agent:
- **Fixed mode (default):** keep one selected session-capable backend and its native session.
- **Flex mode:** one bot, one workspace, one shared identity, and a switchable backend via `/backend`.

Wrapper, Audit, and Dual Brain are retired and cannot be selected. Persisted
legacy mode values migrate to the configured default without deleting their
historical configuration blocks.

See [Fixed and Flex Working Modes](FIXED_FLEX_WORKING_MODES.md) for the
configuration, transition, migration, and regression contract.

- **Memory+ continuity:** an independent optional layer that can stay enabled in any execution mode.
- **Selectable backends:** `gemini-cli`, `claude-cli`, `codex-cli`, `grok-cli`,
  `her-v2` (with `her` as a migration alias), `ollama-api`, and `xai-api`.
  `openrouter-api` and `deepseek-api` remain provider-only engines for HER v2
  and internal rendering; they are hidden from `/backend`.
- **Adding agents:** Add a new block to `<project_root>\agents.json`. Always set `type` explicitly. New agents should normally use `type: "flex"`; omitted `type` is rejected so HASHI cannot accidentally fall back to the retired legacy fixed runtime.
  - Flex required fields: `name`, `type: "flex"`, `workspace_dir`, `allowed_backends`, `active_backend`, `is_active`; the workspace must contain a strict lower-case `agent.md`
  - `default_mode` may be `fixed` or `flex`. If omitted, session-capable backends default to `fixed`; stateless backends use `flex`.
  - Legacy `type: "fixed"` and `system_md` values are one-time migration inputs only; successful startup converts the row to Flex shape, validates/writes `agent.md`, and removes `system_md`
  - Optional: `display_name`, `emoji`, `typing_message`, `typing_parse_mode`, `effort`, `resume_policy`
  - `access_scope` — filesystem boundary: `"workspace"` (agent dir only), `"project"` (repo root), `"drive"` (full `C:\`)
  - `idle_timeout_sec` — maximum seconds without meaningful backend activity;
    this is not a total execution clock
  - `background_mode` — detach to background with escalating placeholders and an `agent.md`-authored transition status (`true`/`false`)
  - `background_detach_after` — seconds before detaching
  - `escalation_thresholds` — array of seconds for placeholder messages (e.g. `[30, 60, 90, 150]`)
- **HER v2 tools and authority:** every explicit `her-v2` backend row defaults
  to the personal-instance YOLO policy: `permission_mode` is
  `"danger-full-access"`, `access_scope` is `"drive"`, and
  `tools.allowed` is `["*"]`. The normalizer fills only missing fields, so a
  user can restrict any Agent by setting those fields explicitly in that
  Agent's HER v2 row. HASHI never adds a HER v2 backend row that the user did
  not configure. Scheduled prompts run through their owning Agent's current
  backend and inherit that Agent runtime's authority; a Cron does not create a
  separate low-privilege Agent.
- **Tokens and secrets:** Telegram bot tokens and API keys are stored in `<project_root>\secrets.json`, keyed by agent name. Never put them in `agents.json`.
- **Memory isolation:** Each agent runs inside its own `workspace_dir`. Fixed mode enables persistent sessions for session-capable Codex, Claude, Grok CLI, and HER v2 backends. Flex uses bridge-managed context.
- **Per-agent logs and files:** Logs under `<project_root>\logs\<agent>\<session>`. Media under `<project_root>\media\<agent>`.

## Telegram Commands

**Common (all agents):**
- `/help` — list available commands
- `/new` — create and select a new HASHI Session for the originating channel; session-capable fixed backends also clear their native session so the next request starts fresh
- `/fresh` — clean API context for non-CLI backends; HER v2 persists a boundary across every prior turn source while preserving logs, searchable memory, and archives
- `/memory [status|on|pause|saved on|saved off|plus on|plus off]` — inspect or change normal memory injection and independent Memory+ continuity
- `/notepad [today|carryover|history|find <query>|edit <text>|replace <text>|compact|clear]` — inspect or maintain the compact Memory+ work card and archive index
- `/clear` — clear workspace context files
- `/handoff` — restore the latest 10 completed Bridge exchanges across retained HASHI Sessions into a fresh backend session
- `/fyi [prompt]` — refresh bridge environment awareness; optionally append a follow-up prompt
- `/usecomputer [on|off|status|examples|task]` — load managed GUI-aware computer-use guidance; unified shortcut for desktop/browser/Windows interaction when needed
- `/browser [status|examples|1-4 task]` — route an internet task through HASHI headless browser, CLI-native browsing, Brave search, or the logged-in browser extension
- `/exp <task>` — run a task after consulting text-first EXP guidebooks under `exp/`; large training/evidence assets restore from checksum-pinned packs only when needed
- `/status` — agent state, workspace, last activity
- `/debug` — detailed debug info (backend, PID, process state)
- `/start` — inline keyboard to start a stopped agent
- `/terminate` — shut down this agent
- `/stop` — cancel current processing
- `/steer <direction>` — interrupt the active turn, preserve progress, and enqueue an immediate redirected continuation; FUTURE delayed messages remain scheduled
- `/focus` — re-focus the active or most recent task without expanding its scope; FUTURE delayed messages remain scheduled
- `/delay <minutes> <message>` — persist a FUTURE message and append it to this agent's normal FIFO when due; `/delay list` and `/delay cancel <id>` inspect or cancel records
- `/queue [list|show <id>|cancel <id>|clear|history]` — inspect or manage READY and FUTURE pending requests without interrupting the active task
- `/recall [count]` — remove all or the newest `count` pending requests across READY and FUTURE without changing cron, heartbeat, nudge, or automation jobs
- `/resend` — replay the previous model or Bridge output without model work
- `/retry` — stop stale execution, reset context, restore recent handoff continuity, and rerun the last request; see [RETRY_RESEND_COMMANDS.md](RETRY_RESEND_COMMANDS.md)
- `/model` — switch model (inline keyboard), then optionally choose or keep effort when the model supports it
- `/mode [fixed|flex]` — switch working mode; `/mode memory+` only enables Memory+ and keeps the current mode
- `/language [en|zh|default]` — choose the HASHI interface language for this user across all agents. The setting applies to Telegram command menus, buttons, common cards, and system notices, without translating agent replies, terminal output, transcripts, or logs.
- `/terminal [quiet|activity|debug|raw]` — control instance-wide terminal stdout. `quiet` is the default and shows lifecycle/failures/operator attention; `activity` adds content-free phases, timing, tool counts, and token counts; `debug` adds sanitised technical events and failure clues without chat or reasoning text; `raw` restores the historical plaintext console. This never filters Workbench, TUI chat, Telegram, transcripts, or file logs.
- `/think [on|off]` — show the current backend's reasoning presentation; for HER this is only genuine provider-returned reasoning chunks or explicit provider-redaction notices, independent of `/verbose` and `/typing`
- `/commentary [on|off]` — HER only: show explicitly model-authored Persona acknowledgements and interim reports once each; independent of `/think`, `/verbose`, and raw reasoning
- `/verbose [on|off]` — show one temporary deterministic activity digest grouped by lifecycle stage, inspected/changed files, commands, checks, external work, recovery, and status. The same Telegram card is edited as work advances; raw technical events remain in logs, while Persona speech, reasoning, and answer drafts stay excluded.
- `/typing [on|off|status]` — control both the temporary `Agent is typing...` bubble and Telegram's native typing indicator
- `/notify [on|quiet|off]` — `on` notifies for every message; `quiet` silences interim activity but not final results, errors, warnings, recovery, or important alerts; `off` delivers every message silently
- `/stream` and `/preview` — retired compatibility commands that point to the display controls above; Telegram answers are delivered only when complete
- `/skill` — browse and apply standard instruction Skills (inline keyboard)
- `/active [on|off] [minutes]` — toggle bridge-managed proactive heartbeat (default 10 min)
- `/nudge [list]` — show idle continuation jobs. `/nudge <minutes> <exit condition>` creates an idle-only continuation job; `/nudge max <id-fragment> <+100|-100|number|unlimited>` adjusts the optional fire limit. Telegram nudge panels also include `Max -100`, `Max +100`, and `Max ∞` buttons.
- `/voice [status|on|off|provider|providers|voices|use <alias>]` — control native bridge-owned voice replies
- `/reboot [min|max|number|help]` — preflight and hot-reload Python code,
  rebuild managers, restart the exact selected lifecycle scope, and
  warm-recreate Workbench/API gateway/scheduler/watchers; `min` and numbered
  targets are never widened or rejected because a valid class interface
  changed, while the process lock and live WhatsApp transport remain intact
- `/rebuild` — one-version compatibility notice for the retired native HER build workflow; performs no build, reload, or restart
- Alias: `/usercomputer`

The `/verbose` digest uses one stable, backend-neutral vocabulary. Lifecycle
headers are `🧭 Planning`, `🛠️ Execution`, `🔄 Replanning`, `🧐 Review`,
`🔬 Verification`, `✍️ Finalisation`, and `✅ Completed`, with `⏳ Preparing`,
`⛔ Blocked`, and `❌ Error` for control states. Body rows are grouped as
`🔎 Inspect`, `📝 Change`, `⚙️ Execute`, `🧪 Check`, `🌐 External`,
`🔁 Recovery`, and `⏳ Waiting`. Outcomes use `✅` success, `⚠️` warning,
`❌` failure, and `⛔` blocked. Classification is programmatic from canonical
events and known command/tool shapes; no model generates or paraphrases it.

**Backend configuration:**
- `/backend` — switch active backend in Flex (inline keyboard; `+` variant carries continuity handoff). In another mode it first asks whether to switch to Flex, preserves saved mode configuration and Memory+, then continues directly to the backend picker. Selecting `her-v2` switches only the backend; it never asks the user to select the internal `role-configured` sentinel.
- `/provider [name|hybrid]` — HER v2-only routing-mode picker. A named provider keeps the immediate Single-provider flow; `hybrid` opens a draft with independent Quick and Pro provider/model targets.
- `/model` — for HER v2, edit complete Quick/Pro targets and let each effective task route follow Quick, follow Pro, or use a Custom provider/model target. The Direct route is fixed to Quick and defaults to provider reasoning `high`; `/model reasoning direct <value|inherit>` may override or restore that reasoning default. Use `/model quick|pro [provider] <model>`, `/model route <route> <quick|pro>`, `/model route <route> custom <provider> <model>`, `/model reasoning <route> <value|inherit>`, and `/model apply|discard`. Other backends retain their existing single-model `/model [name]` behaviour.
- `/compact [status|cancel]` — HER v2-only context maintenance with two independent phases. Any active WIP Journal is first converted without a model into bounded quoted Session context and cleared only after a verified durable write; this phase runs even below 64,000 tokens and failure preserves the Journal. Ordinary conversation compaction still uses the active Quick/Light provider and model at fixed high HER effort: below 64,000 effective tokens it reports the exact not-needed reason, from 64,000 tokens upward it executes, and above 128,000 tokens main Execution schedules the non-blocking automatic path. Active WIP and automatic compaction failures both produce mandatory warnings independently of `/verbose`.
- `/model compact inherit_quick [auto|tier_2|tier_3]` or `/model compact off` — enable the approved inherited Quick/Light Compact policy, choose its isolated watchdog tier, or turn it off. Legacy inherited-Pro and explicit Compact records migrate to `inherit_quick`.
- Non-HER backend/model selection continues to the existing optional effort step when supported. HER v2 keeps backend, provider, models/reasoning, and effort as independent controls.
- `/effort [level]` — HER v2 opens the **HER execution mode** control: Direct (`zero`), Strategic (`low`), and Planned (`medium`). Direct invokes one fully capable Quick agent and skips HER orchestration. Strategic selects task-matched Strategy Cards before fully capable Execution. Planned uses no-tool Strategy, mechanically read-only Planning, then fully capable Execution; Planning may investigate but cannot mutate artifacts or perform the downstream implementation. Descriptive aliases persist as canonical wire values, while legacy `fast` and `fast_path` still select Strategic. Retired saved HER values `high`, `xhigh`, and `max` migrate to Planned; their Replanning/Review designs remain internal and are not selectable. HER effort never reads or writes provider reasoning. Other backends retain their model-aware effort behaviour.

### HER v2 provider and model configuration

HER v2 is provider-neutral. Provider connection metadata lives under
`global.her_providers.providers`; an enabled instance provider is sufficient for
HER routing and does not need to be repeated in each Agent's
`allowed_backends`. Concrete non-HER rows still control ordinary direct backend
selection. API-key values stay in `secrets.json`.

```json
{
  "global": {
    "her_providers": {
      "max_permission_mode": "danger-full-access",
      "providers": {
        "openrouter": {
          "engine": "openrouter-api",
          "base_url": "https://openrouter.ai/api/v1",
          "secret": "openrouter-api_key",
          "status": "stable"
        },
        "deepseek": {
          "engine": "deepseek-api",
          "base_url": "https://api.deepseek.com/v1",
          "secret": "deepseek-api_key",
          "status": "stable"
        }
      }
    }
  },
  "agents": [
    {
      "name": "hashiko",
      "type": "flex",
      "allowed_backends": [
        {
          "engine": "openrouter-api",
          "models": [
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4-pro",
            "openai/gpt-4.1-mini"
          ],
          "default_model": "deepseek/deepseek-v4-flash"
        },
        {
          "engine": "deepseek-api",
          "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
          "default_model": "deepseek-v4-flash"
        },
        {
          "engine": "her-v2",
          "access_scope": "drive",
          "model": "role-configured",
          "effort": "high",
          "permission_mode": "danger-full-access",
          "tools": {"allowed": ["*"]},
          "her_v2": {
            "review_limits": {"xhigh": 1, "max": 1},
            "profiles": {
              "lightweight": {
                "engine": "deepseek-api",
                "model": "deepseek-v4-flash",
                "reasoning": "high"
              },
              "triage": {
                "engine": "deepseek-api",
                "model": "deepseek-v4-flash",
                "reasoning": "high"
              },
              "premium": {
                "engine": "deepseek-api",
                "model": "deepseek-v4-pro",
                "reasoning": "high"
              },
              "reviewer": {
                "engine": "deepseek-api",
                "model": "deepseek-v4-pro",
                "reasoning": "max"
              }
            }
          }
        }
      ]
    }
  ]
}
```

Provider choices are built from enabled instance provider profiles and the
installed adapter model catalogue, with Agent backend rows retained as optional
model/default hints. A one-model provider uses that model for both slots.
Disabled providers remain visible but locked.

Single mode keeps one provider for Quick and Pro. Hybrid mode stores full
`provider + model` targets for Quick and Pro. Immediate response, Triage,
Meditation, Dream, and Simple execution follow Quick by default; Planning,
Complex execution, High-volume execution (including its sub-agents),
Replanning, Review, and Finalisation follow Pro. Any task route may instead use
a Custom target. This phase adds no
automatic cross-provider failover and no picture/media-specific routing.

### Dormant higher-mode internals

The following Replanning and Review behaviour remains implemented for internal
regression coverage, but Adaptive, Reviewed, and Assured are not exposed by the
current three-mode production selector. It must not be treated as an available
day-to-day `/effort` choice.

Triage independently records each work turn as `STANDARD` or `HIGH_RISK` risk
metadata. Adaptive (`high`) and above Execution, regardless of that label,
unconditionally enters tool-free Replanning at the next safe boundary after 10
completed tool results or 300 seconds. Each Replan sends one fact-preserving
progress message. This does not cancel active work or cap the tool loop,
Replans, or whole workflow; ordinary denial, approval, permissions, `/stop`,
Review, and Finalisation keep their existing authority.

Independent Review follows the reviewer/Pro route by default and receives only
the validation capabilities needed to assess the latest Execution result.
`workspace_inspect`
provides read-only snapshots, status, diff, search, and artifact hashes.
Its search operation uses ripgrep when available and falls back to the system
grep binary, so it does not depend on the service inheriting a developer-shell
PATH.
`verification_run` runs a configured recipe or direct process `argv` in the
authoritative current workspace. It does not copy or sandbox the workspace and
does not invoke an implicit shell. The command inherits HASHI's process
identity, filesystem access, environment, `HOME`, and network. Its effective
timeout is the maximum of its configured/requested values, five minutes, and
cumulative Execution time multiplied by 1.5 plus five minutes; requested values
can raise but never shorten that budget.
Passing claims require exact completed receipts from the current Review
invocation and matching before/after snapshots.

Runtime selections persist in the agent workspace as a dedicated
`her_v2_configuration` block. It contains `routing_mode`, full Quick/Pro
`targets`, per-route target selection, optional Custom `route_targets`, and
per-route provider reasoning. Hybrid edits first persist in
`her_v2_configuration_draft`; `/model apply` validates and activates the whole
draft in one state update. The last Single and Hybrid configurations are kept
under `her_v2_configuration_presets`. In-flight turns keep their starting
routing snapshot, and queued Meditation jobs durably keep the turn's target.
Legacy provider/model and profile/stage reasoning fields remain readable as a
migration fallback. The internal `role-configured` model remains an adapter
sentinel only and is never presented as a user choice.

**Retired working modes:** `/mode wrapper`, `/mode audit`, and
`/mode dual-brain` return a compatibility notice. Their former configuration commands
and old inline buttons do the same; none can reactivate a retired mode.

**Backend-specific (fixed):**
- `/effort` — Claude, Codex, and Grok CLI
- `/credit` — OpenRouter

Memory+ stores a bounded today card, a short cross-day carryover, and archive
pointers without injecting old prompts or full answers. Pausing it preserves
all files. See [Memory+ v2 — Compact Work Continuity](MEMORY_PLUS_V2.md) for
backend routing, rollover, migration, and writer ownership.

## Skills System
- Skills live under `skills/` as `skills/<kebab-case-name>/SKILL.md` packages.
- Frontmatter requires standard `name` and `description`; Agent Skills optional metadata fields are accepted, while the Markdown body supplies request instructions.
- `/skill` maintains standard instruction packages with catalog, validation, install/link, per-agent enable/disable, search, rescan, and recoverable delete/uninstall controls. It does not restore legacy action or toggle package types.
- Built-in and installed packages move to `state/skill_recovery/` when removed; linked packages are unlinked without touching their source. Structured Job references block removal.
- Skill detail cards show a cumulative project-wide counter backed by the privacy-bounded `state/skill_usage.jsonl` invocation log plus compatible historical `token_audit.jsonl` records.
- Cron, heartbeat, nudge, and deterministic automation remain under `/jobs`. `/delay` uses a separate FUTURE message store and never creates or edits `/jobs` records. Debug, recall state, and Dream remain runtime controls.
- Native HER/Claw Skill discovery and execution are disabled, so HASHI is the only `/skill` owner.
- Legacy underscore IDs remain accepted for scheduled-job compatibility while new definitions use kebab-case.
- Delegation skills: `/skill codex <task>`, `/skill claude <task>`, `/skill gemini <task>` for cross-backend delegation.

## Browser Tool

All agents can control a real web browser through Playwright, regardless of their backend type.

### `/browser` Route Command

Use `/browser` when the operator wants to choose the internet route explicitly:

| Route | Command | Intended path |
|---:|---|---|
| `1` | `/browser 1 <task>` | HASHI standalone/headless browser tools for public or JavaScript-heavy pages |
| `2` | `/browser 2 <task>` | CLI backend native browsing/search where supported by Codex CLI, Claude CLI, or Gemini CLI |
| `3` | `/browser 3 <task>` | Brave Search (`web_search`) plus public page fetches (`web_fetch`) |
| `4` | `/browser 4 <task>` | HASHI browser extension bridge for the logged-in Windows browser |

Route 2 is instruction-only from HASHI's perspective because the browsing capability lives inside the selected CLI backend. Route 4 uses the real logged-in browser and should confirm before destructive actions, submissions, purchases, account changes, or bulk edits.

`/browser` and `/browser status` show the same route dashboard. Traffic lights
are status indicators: green means confirmed online and usable, yellow means
unknown/not checked, and red means offline or misconfigured. The built-in
HEADLESS route is available with Hashi itself; Brave and extension routes are
reported from their live configuration checks.

### Actions

| Action | Description |
|--------|-------------|
| `screenshot` | Navigate to URL, return PNG screenshot (base64 or saved file) |
| `get_text` | Render page with JS, return visible text content |
| `get_html` | Return fully-rendered HTML after JS execution |
| `click` | Click an element by CSS selector |
| `fill` | Fill a form field; optionally press Enter to submit |
| `evaluate` | Run custom JavaScript and return the result |

### Two Modes

**Standalone mode** (default) — launches a clean headless Chromium. No login state.

**CDP mode** — attaches to the user's already-running Chrome, inheriting all cookies and login sessions:
```bash
# Start Chrome once with debugging port (login state persists in --user-data-dir)
google-chrome --remote-debugging-port=9222 --user-data-dir=~/.chrome-hashi
```

### Usage by Backend Type

**CLI backends (Claude CLI, Gemini CLI, Codex CLI)** — call via `bash` tool:
```bash
python tools/browser_cli.py screenshot --url https://example.com --out /tmp/shot.png
python tools/browser_cli.py get_text   --url http://localhost:3000 --cdp-url http://localhost:9222
python tools/browser_cli.py fill       --url https://site.com --selector "#q" --text "hello" --submit
python tools/browser_cli.py evaluate   --url https://site.com --script "() => document.title"
```

**HER v2 / provider tool path** — enable the required Tool Registry entries on
the explicit HER v2 row or the provider row consumed by HER v2:
```json
{
  "engine": "her-v2",
  "model": "role-configured",
  "tools": {
    "allowed": ["browser_screenshot", "browser_get_text", "browser_get_html",
                "browser_click", "browser_fill", "browser_evaluate"]
  }
}
```

### Prerequisites

```bash
python -m pip install -e ".[browser]"
playwright install chromium
```

Playwright is isolated in the `browser` dependency profile.

### Cross-Platform

Chrome/Chromium auto-detected on Linux, macOS, and Windows (including WSL). Falls back to Playwright's bundled Chromium if system Chrome is not found.

## Telegram File Sending

Agents can send photos, documents, videos, and audio files directly to the user's Telegram chat.

### CLI Script (all backends)

```bash
python tools/telegram_send_file_cli.py --path /tmp/chart.png --caption "Caption text" --agent <agent_name>
python tools/telegram_send_file_cli.py --path /tmp/report.pdf --type document
```

Parameters:
- `--path` (required): absolute path to the file
- `--caption` (optional): message caption
- `--type` (optional): `photo | document | video | audio` (default: auto-detect from extension)
- `--agent` (optional): agent name for token resolution (defaults to first available)
- `--chat-id` (optional): override target chat ID

Auto-detection: `.jpg/.jpeg/.png/.webp` → photo, `.mp4/.mov/.avi/.mkv` → video, `.mp3/.ogg/.flac/.wav/.m4a` → audio, everything else → document.

### Native Tool Call (HER v2 provider/tool paths)

`telegram_send_file` can be supplied through `global.default_tools` and becomes
model-visible when the selected runtime/provider exposes the HASHI Tool
Registry. It is not proof that every backend supports native tools.

```json
{
  "tool": "telegram_send_file",
  "path": "/tmp/chart.png",
  "caption": "Optional caption",
  "file_type": "auto"
}
```

### Global Default Tools

Tools listed in `agents.json` → `global.default_tools.allowed` merge with each
backend/provider tool configuration. The selected runtime still applies its
own capability and permission checks before advertising or executing them.

```json
{
  "global": {
    "default_tools": {
      "allowed": ["telegram_send_file"]
    }
  }
}
```

## Bridge Memory System
- `orchestrator/bridge_memory.py` — SQLite with WAL mode, local hashed embeddings (256-dim), FTS5 full-text search.
- `BridgeContextAssembler` builds the final prompt sent to backends: system identity + skill sections + top-6 long-term memory + last-10 conversation turns.
- Per-request bridge-context capacities (not cumulative task limits): Codex 24k,
  Gemini 24k, Claude 50k, OpenRouter 35k.

## Voice (Outbound Speech Replies)
- Bridge-owned, not model-owned. Models return text; bridge synthesizes speech locally.
- Audio converted to OGG/Opus via `ffmpeg` and delivered as Telegram voice notes.
- TTS providers: `windows` (default, no extra install), `edge`, `piper`, `kokoro`, `coqui`.
- Provider config: `orchestrator/voice_manager.py`.
- Incoming voice/audio is still transcribed to text via faster-whisper before dispatch.

## Bridge-U-F Task Scheduler (Heartbeat & Cron)
- Tasks defined in `<project_root>\tasks.json`.
- Task types: **heartbeats** (interval-based, `interval_seconds`) and **crons** (time-of-day, `HH:MM`).
- Scheduler checks every 15 seconds; injects prompt into target agent's async queue when due.
- Hot reload: `tasks.json` is re-read on each loop — no restart needed for task changes.
- Cron actions: enqueue a prompt or perform a built-in action (e.g. transcript export to markdown journal).
- On the first pass after a restart, due cron and heartbeat jobs for the same agent are persisted as one recovery batch. HASHI directly sends a fixed notice showing affected task IDs, total missed occurrences, purpose, due-time range, and replay limit; it does not ask an agent to generate the notice.
- Pending and recently resolved recovery batches are injected into later user turns for that agent. The bridge directly handles `run all` / `全部补跑`, `task-id=N` / `补跑 N 次`, and `skip all` / `全部跳过`, and persists the result across restarts.
- Recovery defaults to one execution per task. Set `"recovery": {"max_replay": N}` on a job to permit bounded repeated catch-up; partial counts select the most recent N occurrences and execute them in chronological order.
- A single recent job keeps automatic catch-up behavior. A cron missed by more than one hour still waits for user confirmation, and normal heartbeat ticks after startup are not grouped.
- HER v2 prompt/skill jobs always use Direct (`zero`) for scheduled, recovery,
  and manual Run invocations. This bypasses Triage so the authoritative job
  instruction reaches one fully capable Quick-model agent without
  pre-processing. It does not change provider reasoning settings or the
  Agent's saved `/effort` value.
- Per-job HER effort overrides are retired. Older `her_v2_effort` fields remain
  loadable but are ignored and removed when the job is next updated,
  transferred, or imported; they cannot bypass the Direct policy.
- Nudge continuations keep the Agent's configured effort. Built-in automation,
  transcript export, and HER Dream actions bypass this prompt-only policy.

## Dynamic Agent Lifecycle
Agents can be started and stopped without restarting the bridge process.
- **BAT:** `start-agent.bat <agent>`, `stop-agent.bat <agent>`
- **Workbench API:** `POST /api/admin/start-agent {"agent": "coder"}`, `POST /api/admin/stop-agent {"agent": "coder"}`
- **Telegram:** `/start` (inline keyboard), `/terminate` in agent chat
- Implementation in `main.py`: `start_agent()` / `stop_agent()` methods.

## Workbench
- Browser frontend + local Node API + bridge integration API (`orchestrator/workbench_api.py`).
- Runs at `127.0.0.1:18800`.
- Telegram and workbench share the same agent queue — commands affect the same underlying state.
- Start: `workbench.bat`. Control: `workbench_ctl.ps1`.

## WhatsApp Transport
- Optional; uses the complete WhatsApp profile
  (`python -m pip install -e ".[whatsapp]"`).
- Per-chat routing layer: `/agent <name>` routes to a specific agent; `/all` broadcasts to all.
- Config in `agents.json` global section under `"whatsapp"`: `enabled`, `allowed_numbers`, `default_agent`, `session_dir`.
- Credentials in `wa_session/` (gitignored, never commit).
- Incoming voice transcribed to text before dispatch.

## Agent-to-Agent Messaging
- `bridge_protocol.py` — `bridge-agent-v1` envelope format; intents: `ask`, `notify`.
- `agent_directory.py` — capability-based access control (`can_talk_to`, `can_receive_from`, allowed intents, granted scopes).
- `conversation_router.py` — routes between agents, renders bridge prompts, enqueues work, captures replies.
- `conversation_store.py` — SQLite (WAL), stores threads, messages, permission audits.
- API surfaces: `POST /api/bridge/message`, `POST /api/bridge/reply`, `GET /api/bridge/message/{id}`, `GET /api/bridge/thread/{id}`, `GET /api/bridge/capabilities/{agent}`.

## Local OpenAI-Compatible API Gateway
- Optional HTTP server at `http://127.0.0.1:18801` exposing OpenAI-compatible endpoints backed by CLI adapters.
- Enable: press `[A]` in `bridge-u.bat` menu, or run `python main.py --api-gateway`.
- Endpoints: `GET /health`, `GET /v1/models`, `POST /v1/chat/completions` (sync or streaming).
- Session cache: pass `session_id` in `extra_body`; sessions expire after 30 min inactivity.
- Caller-owned OpenAI function tools are supported by Codex CLI models and
  compatible xAI Chat Completions models. HASHI returns `tool_calls` but never
  executes them; the client must append matching `role: "tool"` results and
  resend the complete structured history without `session_id`.
- Codex uses an isolated ephemeral app-server turn with local/MCP tools
  disabled. See `docs/CODEX_API_TOOL_CALL_BRIDGE.md` for the Agent contract,
  supported choices, limits, and failure semantics.
- Smoke test: `python test_api_gateway.py`.

## Bridge Runtime Control
```
bridge-u.bat              # main launcher
.\bridge_ctl.ps1 status   # view runtime status
.\bridge_ctl.ps1 stop     # graceful stop
.\bridge_ctl.ps1 restart  # restart
.\bridge_ctl.ps1 kill     # force kill
```

## GitHub CLI

`gh` is installed and authenticated with two accounts on `github.com`:

| Account | Handle | Active | Notes |
|---|---|---|---|
| Primary | `<your-github-handle>` | **Default** | Has `workflow` scope |
| Secondary (optional) | `<secondary-handle>` | Inactive | Narrower scopes |

**Switching accounts:**
```bash
gh auth switch --user <secondary-handle>   # switch to secondary account
gh auth switch --user <your-github-handle>   # switch back to primary account
```

**Default account for tasks:** Use your primary account unless instructed otherwise.

**Protocol:** HTTPS (not SSH). Tokens stored in Windows keyring.

**Common operations:**
```bash
gh repo list                            # list repos for active account
gh pr create --title "..." --body "..." # create PR on current repo
gh repo create <name> --public          # create new repo
gh issue list                           # list issues
gh run list                             # list workflow runs
```

---

## Hardware & Environment

### Windows Host
- Primary development environment: `<projects_dir>\`
- Windows 11 Pro with WSL2 enabled
- Shell: bash (Unix syntax — use `/dev/null`, forward slashes, etc.)

### Docker
- Docker Desktop 29.x installed and available from Windows
- Use `docker compose` (not `docker-compose` — v2 syntax)
- Docker WSL2 integration is **not yet enabled** in Docker Desktop settings — `docker` command is unavailable inside WSL2 distros. If asked to run Docker from Linux, either enable WSL integration in Docker Desktop first, or run Docker commands from the Windows host shell.

### GPU (<GPU model>)
- DirectML hardware acceleration available via `onnxruntime-directml`
- Use for ONNX model inference on Windows

### NPU (<NPU model>)
- Ryzen AI SDK installed at `<RyzenAI install path>`
- Conda environment: `<ryzen-ai-conda-env>`
- Use for NPU-accelerated inference via Ryzen AI toolchain

### WSL2 Linux Environment (Ubuntu 22.04)
- Distro: `Ubuntu-22.04` (WSL2)
- Linux projects directory: `~/projects/` (WSL2 filesystem — fast Docker I/O)
- Current WSL2 projects: `Agent-B-Research`, `Veritas`, `gnosiplexio`
- Windows `C:\` drive mounted at `/mnt/c/` inside WSL2
- Bridge-u-f repo accessible from WSL2 at `/mnt/c/path/to/bridge-u-f/`

**When asked to do something in the Linux environment:**
1. Run commands via `wsl -d Ubuntu-22.04 -- bash -c "<command>"` from Windows, or prefix tool calls with the WSL context.
2. For file work inside WSL2, use paths like `~/projects/<name>/` (not `/mnt/c/` unless accessing Windows files).
3. For Docker work in Linux, check if Docker WSL integration is enabled first (`docker --version` inside WSL2). If not, run Docker from Windows shell instead.
4. Python/pip/node inside WSL2 are separate installs from Windows — don't assume packages installed on Windows are available in WSL2.
5. When building or running Linux-native services (e.g., Docker Compose stacks), prefer working from `~/projects/` inside WSL2 for best performance.

### Windows Use Tools

`/usecomputer` sits above the raw GUI tool tiers. It does not replace `desktop_*` or `windows_*`; it tells the agent when and how to use them coherently.

- prefer non-GUI methods first when available
- use `desktop_*` for Linux/X11 virtual desktop work
- use `windows_*` for the real Windows desktop
- inspect environment, focus, and screenshots before acting
- re-check after important actions instead of assuming UI state

The Linux virtual-desktop tier (`desktop_*`) now follows the same stability pattern more closely:

- prefers native `xdotool` actions for mouse / click / key / scroll when available
- keeps `usecomputer` as the screenshot and fallback path
- exposes `desktop_window_list` / `desktop_window_focus` for WSL/X11 window introspection and targeting
- makes `desktop_info` more explicit about live display sockets and active-window state

HASHI also supports a separate `windows_*` tool tier for controlling the real Windows desktop.

- Intended for agents running on Windows directly, or inside WSL using `powershell.exe` interop
- Current backends:
  - Windows-host `usecomputer`
  - `windows-mcp` for screenshot + input actions
- Best reliability when the Windows desktop is unlocked
- Separate from `desktop_*`, which targets the Linux virtual desktop
- Treat multi-display setups as the default case
- Before any screenshot-driven action, call `windows_info` and inspect `displays`
- Use `windows_screenshot(display=N)` on the chosen monitor before input actions
- Use `windows_window_list` / `windows_window_focus` alongside screenshots; screenshots alone do not guarantee focus

Suggested tier config:

```json
"tools": {
  "tiers": ["core", "windows_use"]
}
```

Recommended protocol for Windows UI work:

1. `windows_info` — inspect `displays` and current layout.
2. Decide which display should contain the target app.
3. `windows_screenshot(display=N)` — verify that screen visually.
4. `windows_window_list` / `windows_window_focus` — find and target the window.
5. `windows_screenshot(display=N)` again before typing or clicking if focus matters.

## Important Behavior Notes
- Bridge owns continuity; backends are treated as stateless.
- Backend capabilities are not identical — session model, file handling, tool use, and streaming vary per backend.
- `/think` follows each non-HER backend's existing presentation rules. For HER,
  it accepts only genuine provider reasoning or an explicit provider-redaction
  notice. HER acknowledgement/commentary, generic progress, tools, final text,
  and answer deltas never enter the HER think buffer. If HER exposes no
  reasoning, `/think on` remains quiet.
- `/handoff` restores continuity from bridge-owned transcript history, not CLI resume state.
- Model and effort changes at runtime are not automatically persisted back to `agents.json`.
- Backend-specific behaviors must be labeled as such, not described as universal.

### Telegram display event contract

| Backend | `/think` input | `/verbose` progress and tool summaries |
|---|---|---|
| Codex CLI | Full intermediate `agent_message` commentary. Raw provider reasoning text is not exposed by current `codex exec --json`; reported reasoning-token usage is still recorded | Task start, command start/exit code, edited paths, and todo updates |
| Grok CLI | `thought`, `thinking`, or reasoning events when emitted by Grok | Generic progress plus mapped shell, file, search, tool-start, and tool-result events; result detail depends on the CLI payload |
| HER | Actual reasoning text, explicit redaction notices, or legacy reasoning summaries when `stream-json` is supported | Task start, tool start/end, usage summary; JSON fallback can still report completed tool use but not live reasoning |
| Claude CLI | Actual `thinking_delta` content when the model emits it | Tool/file/shell start, streamed tool input, and completion markers; result output is not always exposed |
| Gemini CLI | Not currently exposed by Gemini's parsed stream schema | Task start, tool use, short tool-result previews, and errors |
| OpenRouter provider adapter | `reasoning` or `reasoning_details`, model/provider dependent | HASHI tool-gateway start/action/end events, short output previews, policy blocks, and tool-loop warnings |
| DeepSeek provider adapter | `reasoning_content` and reported reasoning-token usage on reasoning models | Same HASHI tool-gateway summaries as OpenRouter |
| xAI API | Provider reasoning fields when the selected model/endpoint returns them | HASHI tool-gateway summaries where local tool execution is used |
| Ollama API | The model's `reasoning` field when available | HASHI tool-gateway summaries for locally enabled tools |

Assistant answer deltas remain available to local activity observers, but they are never displayed as Telegram previews. Codex commentary is kept distinct from both generic progress and private/provider reasoning, then routed to `/think` as Codex's user-visible substitute. Long commentary is split across Telegram messages rather than truncated. `/typing` is backend-independent.

HER `/commentary` is enabled by default as a durable workspace preference.
Effort controls which commentary events HER generates; the display router does
not repeat that effort decision. Medium may generate the initial model-authored
Persona acknowledgement. High and above may additionally generate a
model-authored Replan or progress report when something material changes. Every
such event carries one stable identity and is sent as its own durable message at
most once. A deterministic TaskFrame summary or neutral long-wait lease is
technical telemetry, not Persona speech, and therefore belongs only to
`/verbose`. Technical activity and lease timing never reset or delay the Persona
commentary clock. HER retains only the newest material commentary while cadence
is pending; an older pending update is explicitly recorded as coalesced. Turn
finalization does not flush a batch of stale commentary. If the final answer
supersedes the one remaining pending update, its audit status is
`superseded_by_final`, not silently discarded or falsely delivered. On non-HER
backends, `/commentary` reports that the setting is unavailable and changes
nothing.

HER assigns every stream event exactly one presentation owner. `/verbose`
accepts only `technical`, `/commentary` only `user_commentary`, and `/think`
only `reasoning`. Final answers, direct responses, required permission/control
messages, fatal errors, and feature-owned final results use mandatory lanes and
remain visible when all three optional switches are off. Raw events are written
to audit and bounded local activity before presentation filtering. HER
`/commentary` changes apply to future commentary events in the active request.
The `/verbose` and `/think` presenter resources are selected when a request
starts, so changing either setting takes full effect on the next request.
Suppressed or delivered events are never replayed merely because a switch later
changes.
