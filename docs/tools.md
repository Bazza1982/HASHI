# Tools

## Universal Multi-Agent Telegram Orchestrator (`bridge-u-f`)
The `bridge-u-f` project located at `<project_root>` is a local multi-agent bridge that connects Telegram bots (and optionally WhatsApp) to multiple AI backends, with an optional browser workbench.

Two operating modes:
- **Fixed agents:** one Telegram bot, one backend, one workspace.
- **Flex agents:** one Telegram bot, one workspace, one shared identity, switchable backend via `/backend`.

- **Supported backends:** `gemini-cli`, `claude-cli`, `codex-cli`, and `openrouter-api`.
- **Adding agents:** Add a new block to `<project_root>\agents.json`. Required fields:
  - `name`, `engine` (fixed) or `type: "flex"` (flex), `workspace_dir`, `system_md`, `model`, `is_active`
  - Optional: `display_name`, `emoji`, `typing_message`, `typing_parse_mode`, `effort`, `resume_policy`
  - `access_scope` — filesystem boundary: `"workspace"` (agent dir only), `"project"` (repo root), `"drive"` (full `C:\`)
  - `process_timeout` — hard kill timeout in seconds (default 120)
  - `background_mode` — detach to background with escalating placeholders (`true`/`false`)
  - `background_detach_after` — seconds before detaching
  - `escalation_thresholds` — array of seconds for placeholder messages (e.g. `[30, 60, 90, 150]`)
- **Tokens and secrets:** Telegram bot tokens and API keys are stored in `<project_root>\secrets.json`, keyed by agent name. Never put them in `agents.json`.
- **Memory isolation:** Each agent runs inside its own `workspace_dir`. Session/history state depends on backend:
  - `gemini-cli`: resumes with `--resume latest`
  - `claude-cli`: resumes with `--continue`
  - `codex-cli`: resumes with `resume --last`
  - `openrouter-api`: bridge-managed local history in workspace
- **Per-agent logs and files:** Logs under `<project_root>\logs\<agent>\<session>`. Media under `<project_root>\media\<agent>`.

## Telegram Commands

**Common (all agents):**
- `/help` — list available commands
- `/new` — fresh session (re-primes with FYI catalog)
- `/clear` — clear workspace context files
- `/handoff` — restore recent continuity from bridge transcript into a fresh session
- `/fyi [prompt]` — refresh bridge environment awareness; optionally append a follow-up prompt
- `/status` — agent state, workspace, last activity
- `/debug` — detailed debug info (backend, PID, process state)
- `/start` — inline keyboard to start a stopped agent
- `/terminate` — shut down this agent
- `/stop` — cancel current processing
- `/retry` — retry last request
- `/model` — switch model (inline keyboard)
- `/think [on|off]` — toggle thinking trace display (periodic italic messages, ~60s intervals, independent of `/verbose`)
- `/verbose [on|off]` — toggle real-time streaming display
- `/skill` — browse, toggle, and run skills (inline keyboard)
- `/active [on|off] [minutes]` — toggle bridge-managed proactive heartbeat (default 10 min)
- `/voice [status|on|off|provider|providers|voices|use <alias>]` — control native bridge-owned voice replies
- `/reboot [min|max|number|help]` — hot restart agents with live Python code reload; preserves scheduler, workbench API, WhatsApp

**Flex-only:**
- `/backend` — switch active backend (inline keyboard; `+` variant carries continuity handoff)
- `/effort` — reasoning effort (when active backend is Claude or Codex)

**Backend-specific (fixed):**
- `/effort` — Claude, Codex
- `/credit` — OpenRouter

## Skills System
- Skills live under `skills/` as `skills/<id>/skill.md` with YAML frontmatter.
- Three types: `toggle` (on/off bridge behavior), `action` (runnable script), `prompt` (template with optional backend routing).
- Skill state persisted per agent in `workspace/skill_state.json`.
- Built-in skills: `cron`, `heartbeat`, `debug`, `recall`.
- `recall` — toggle for one-shot automatic session restore after unexpected restart (not after `/new`).
- Delegation skills: `/skill codex <task>`, `/skill claude <task>`, `/skill gemini <task>` for cross-backend delegation.

## Browser Tool

All agents can control a real web browser through Playwright, regardless of their backend type.

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
                "browser_click", "browser_fill", "browser_evaluate"],
    "max_loops": 10
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
      "allowed": ["telegram_send_file"],
      "max_loops": 5
    }
  }
}
```

## Bridge Memory System
- `orchestrator/bridge_memory.py` — SQLite with WAL mode, local hashed embeddings (256-dim), FTS5 full-text search.
- `BridgeContextAssembler` builds the final prompt sent to backends: system identity + skill sections + top-6 long-term memory + last-10 conversation turns.
- Per-backend token budgets: Codex 24k, Gemini 24k, Claude 50k, OpenRouter 35k.

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

## Important Behavior Notes
- Bridge owns continuity; backends are treated as stateless.
- Backend capabilities are not identical — session model, file handling, tool use, and streaming vary per backend.
- `/think` is a working bridge feature — thinking trace displayed as periodic italic messages. It is NOT limited to any single backend.
- `/handoff` restores continuity from bridge-owned transcript history, not CLI resume state.
- Model and effort changes at runtime are not automatically persisted back to `agents.json`.
- Backend-specific behaviors must be labeled as such, not described as universal.
