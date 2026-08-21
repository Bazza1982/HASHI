# Tools

## Universal Multi-Agent Telegram Orchestrator (`bridge-u-f`)
The `bridge-u-f` project located at `<project_root>` is a local multi-agent bridge that connects Telegram bots (and optionally WhatsApp) to multiple AI backends, with an optional browser workbench.

Five execution modes:
- **Fixed agents:** one Telegram bot, one backend, one workspace.
- **Flex agents:** one Telegram bot, one workspace, one shared identity, switchable backend via `/backend`.
- **Wrapper agents:** one shared identity with a functional core backend/model plus a stateless wrapper backend/model that rewrites only the final user-facing response.
- **Audit agents:** a functional core response plus a separate auditor pass and findings.
- **Dual Brain agents:** a left-brain continuity/planning pass plus right-brain execution.

- **Memory+ continuity:** an independent optional layer that can stay enabled in any execution mode.
- **Supported backends:** `gemini-cli`, `claude-cli`, `codex-cli`, `her`, `grok-cli`, `openrouter-api`, `deepseek-api`, `ollama-api`, and `xai-api`.
- **Adding agents:** Add a new block to `<project_root>\agents.json`. Always set `type` explicitly. New agents should normally use `type: "flex"`; omitted `type` is rejected so HASHI cannot accidentally fall back to the retired legacy fixed runtime.
  - Flex required fields: `name`, `type: "flex"`, `workspace_dir`, `system_md`, `allowed_backends`, `active_backend`, `is_active`
  - Legacy fixed emergency fields: `name`, `type: "fixed"`, `engine`, `workspace_dir`, `system_md`, `model`, `is_active`; startup also requires `HASHI_ENABLE_LEGACY_FIXED_RUNTIME=1`
  - Optional: `display_name`, `emoji`, `typing_message`, `typing_parse_mode`, `effort`, `resume_policy`
  - `access_scope` — filesystem boundary: `"workspace"` (agent dir only), `"project"` (repo root), `"drive"` (full `C:\`)
  - `idle_timeout_sec` — maximum seconds without meaningful backend activity;
    this is not a total execution clock
  - `background_mode` — detach to background with escalating placeholders and an `agent.md`-authored transition status (`true`/`false`)
  - `background_detach_after` — seconds before detaching
  - `escalation_thresholds` — array of seconds for placeholder messages (e.g. `[30, 60, 90, 150]`)
- **HER tools and permissions:** `her` exposes all upstream Claw-native tools by
  default. Set `allowed_tools` only to create an explicit tool allowlist.
  Tool visibility does not grant authority: `permission_mode` still controls
  read-only, workspace-write, or danger-full-access execution. Scheduled
  prompts run through their owning Agent's current backend and inherit that
  Agent runtime's access scope; a Cron does not create a separate low-privilege
  Agent.
- **Tokens and secrets:** Telegram bot tokens and API keys are stored in `<project_root>\secrets.json`, keyed by agent name. Never put them in `agents.json`.
- **Memory isolation:** Each agent runs inside its own `workspace_dir`. Fixed mode enables persistent sessions only for session-capable Codex, Claude, and Grok CLI backends. Other modes use one-shot backend turns with bridge-managed context.
- **Per-agent logs and files:** Logs under `<project_root>\logs\<agent>\<session>`. Media under `<project_root>\media\<agent>`.

## Telegram Commands

**Common (all agents):**
- `/help` — list available commands
- `/new` — fresh CLI session reset; non-CLI backends should use `/fresh`
- `/fresh` — clean API context for non-CLI backends; clears recent turns and preserves saved memories without auto-injecting them
- `/memory [status|on|pause|saved on|saved off|plus on|plus off]` — inspect or change normal memory injection and independent Memory+ continuity
- `/notepad [today|carryover|history|find <query>|edit <text>|replace <text>|compact|clear]` — inspect or maintain the compact Memory+ work card and archive index
- `/clear` — clear workspace context files
- `/handoff` — restore recent continuity from bridge transcript into a fresh session
- `/fyi [prompt]` — refresh bridge environment awareness; optionally append a follow-up prompt
- `/usecomputer [on|off|status|examples|task]` — load managed GUI-aware computer-use guidance; unified shortcut for desktop/browser/Windows interaction when needed
- `/browser [status|examples|1-4 task]` — route an internet task through HASHI headless browser, CLI-native browsing, Brave search, or the logged-in browser extension
- `/exp <task>` — run a task after consulting context-specific EXP guidebooks under `exp/`
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
- `/mode [fixed|flex|wrapper|audit|dual-brain]` — switch execution mode; `/mode memory+` only enables Memory+ and keeps the current mode
- `/think [on|off]` — show the current backend's reasoning presentation; for HER this is only genuine provider-returned reasoning chunks or explicit provider-redaction notices, independent of `/verbose` and `/typing`
- `/commentary [on|off]` — HER only: show explicitly model-authored Persona acknowledgements and interim reports once each; independent of `/think`, `/verbose`, and raw reasoning
- `/verbose [on|off]` — show a temporary technical activity card with planning, tools, tests, validation, retries, and runtime status; Persona speech, reasoning, and answer drafts are excluded
- `/typing [on|off|status]` — control both the temporary `Agent is typing...` bubble and Telegram's native typing indicator
- `/stream` and `/preview` — retired compatibility commands that point to the display controls above; Telegram answers are delivered only when complete
- `/skill` — browse and apply standard instruction Skills (inline keyboard)
- `/active [on|off] [minutes]` — toggle bridge-managed proactive heartbeat (default 10 min)
- `/nudge [list]` — show idle continuation jobs. `/nudge <minutes> <exit condition>` creates an idle-only continuation job; `/nudge max <id-fragment> <+100|-100|number|unlimited>` adjusts the optional fire limit. Telegram nudge panels also include `Max -100`, `Max +100`, and `Max ∞` buttons.
- `/voice [status|on|off|provider|providers|voices|use <alias>]` — control native bridge-owned voice replies
- `/reboot [min|max|number|help]` — preflight and hot-reload Python code, rebuild managers, restart selected agents, and warm-recreate Workbench/API gateway/scheduler/watchers; the process lock and live WhatsApp transport remain intact
- `/rebuild [status [job-id]]` — authorized-owner HER development workflow: fingerprint integrated Rust source, run/reuse a supervised incremental Cargo build, verify an immutable candidate, wait for this Agent to become idle, adopt it through targeted hot restart, and automatically roll back on adoption failure; it never promotes or edits the certified HER package
- Alias: `/usercomputer`

**Backend configuration:**
- `/backend` — switch active backend in Flex (inline keyboard; `+` variant carries continuity handoff). In another mode it first asks whether to switch to Flex, preserves saved mode configuration and Memory+, then continues directly to the backend picker. Selecting `her-v2` switches only the backend; it never asks the user to select the internal `role-configured` sentinel.
- `/provider [name]` — HER v2-only call-provider picker. Choosing a provider atomically assigns valid defaults to both Quick and Pro slots while preserving task-route reasoning and HER effort.
- `/model` — for HER v2, define Quick/Pro models and independently configure each effective task route's model slot and provider reasoning. Use `/model quick|pro <model>`, `/model route <route> <quick|pro|inherit>`, or `/model reasoning <route> <value|inherit>`. `inherit` as a model slot is valid only for structure repair. Other backends retain their existing single-model `/model [name]` behaviour.
- Non-HER backend/model selection continues to the existing optional effort step when supported. HER v2 keeps backend, provider, models/reasoning, and effort as independent controls.
- `/effort [level]` — HER v2 effort controls orchestration depth, Planning, Replanning, Review, and sub-agent availability. It never reads or writes provider reasoning. Other backends retain their model-aware effort behaviour.

### HER v2 provider and model configuration

HER v2 is provider-neutral. Provider connection metadata lives under
`global.her_providers.providers`; concrete non-HER backend rows remain the exact
provider/model grants. API-key values stay in `secrets.json`.

```json
{
  "global": {
    "her_providers": {
      "max_permission_mode": "workspace-write",
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
          "model": "role-configured",
          "effort": "high",
          "her_v2": {
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

Each provider row is an exact allowlist. The first and last allowed models are
the default Quick and Pro choices unless `her_v2_fast_model` (the internal
compatibility key for Quick) and `her_v2_pro_model` explicitly select other
granted models. A one-model provider uses that model for both slots. Disabled
providers remain visible but locked.

Runtime selections persist in the agent workspace as a dedicated
`her_v2_configuration` block. It contains the call-provider engine, Quick/Pro
models, per-route model slots, and per-route provider reasoning. Legacy
profile/stage reasoning remains readable as a migration fallback. The internal
`role-configured` model remains an adapter sentinel only and is never presented
as a user choice. Provider/model grants are revalidated before every update.

**Wrapper-mode:**
- `/mode wrapper` — switch a flex-capable agent into wrapper mode.
- `/core [backend=<engine> model=<model>]` — show or change the functional core backend/model. Default: `codex-cli / gpt-5.5`.
- `/wrap [backend=<engine> model=<model> context=<n>]` — show or change the stateless wrapper backend/model and recent visible context window. Default: `claude-cli / claude-haiku-4-5 / context=3`.
- `/wrapper` — show wrapper configuration, persona/style slots, and navigation buttons.
- `/wrapper set <slot> <text>` — set a wrapper persona/style slot.
- `/wrapper clear <slot>` or `/wrapper clear all` — clear wrapper persona/style slots.
- `/model` guides wrapper agents to `/core` or `/wrap`; `/backend` offers the explicit switch-to-Flex confirmation instead of silently changing modes.
- `/reset CONFIRM` preserves wrapper mode config and wrapper slots; `/wipe CONFIRM` remains a hard workspace clear.

Wrapper model picker buttons currently group recommended choices by provider: Claude Haiku/Sonnet, Gemini Flash/Lite, DeepSeek Flash/Pro, and OpenRouter DeepSeek Flash/Gemini. Claude Opus is intentionally omitted from the picker because it is too expensive for routine wrapping.

**Backend-specific (fixed):**
- `/effort` — Claude, Codex, Grok CLI, Claw
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

**OpenRouter API backend** — native tool schema via `ToolRegistry`. Enable in `agents.json`:
```json
{
  "engine": "openrouter-api",
  "tools": {
    "allowed": ["browser_screenshot", "browser_get_text", "browser_get_html",
                "browser_click", "browser_fill", "browser_evaluate"]
  }
}
```

### Prerequisites

```bash
pip install playwright
playwright install chromium
```

Playwright is listed as an optional dependency in `requirements.txt`.

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

### Native Tool Call (OpenRouter/DeepSeek API backends)

`telegram_send_file` is auto-injected for all agents via `global.default_tools` in `agents.json`. No per-agent configuration needed.

```json
{
  "tool": "telegram_send_file",
  "path": "/tmp/chart.png",
  "caption": "Optional caption",
  "file_type": "auto"
}
```

### Global Default Tools

Tools listed in `agents.json` → `global.default_tools.allowed` are automatically available to all agents when using OpenRouter or DeepSeek API backends. Per-backend `tools` config merges with (not replaces) the global defaults.

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
- HER v2 prompt/skill jobs default to `low` execution effort for scheduled,
  recovery, and manual Run invocations. This controls HER orchestration only;
  it does not lower provider reasoning and does not change the Agent's saved
  `/effort` value.
- Add optional `"her_v2_effort": "medium"` to a cron or heartbeat definition
  to override that one job. Allowed values are `low`, `medium`, `high`, `xhigh`,
  and `max`. Invalid values are rejected before prompt work is queued.
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
- Optional; uses neonize (`pip install neonize`).
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
| OpenRouter API | `reasoning` or `reasoning_details`, model/provider dependent | HASHI tool-gateway start/action/end events, short output previews, policy blocks, and tool-loop warnings |
| DeepSeek API | `reasoning_content` and reported reasoning-token usage on reasoning models | Same HASHI tool-gateway summaries as OpenRouter |
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
