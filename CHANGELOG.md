# Changelog

All notable changes to HASHI will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.0.0] - 2026-03-23

### ✨ Added

- **Pack & Go — USB zero-install deployment for Windows and macOS**
  - `windows/prepare_usb.bat` — one-click USB builder for Windows: downloads Python 3.13 embeddable, installs all dependencies, copies project files. Run once on any Windows machine with internet; resulting USB runs on any Windows PC with no Python installation required.
  - `mac/prepare_usb.sh` — equivalent builder for macOS: downloads `python-build-standalone` (auto-detects arm64/x86_64), installs all dependencies, sets permissions.
  - `windows/fix_usb_path.bat` — one-click repair tool for existing USB drives (patches Python `._pth` to include project root).
  - `mac/start_tui.command`, `mac/start_main.command`, `mac/start_workbench.command` — double-clickable Finder launchers for macOS, no terminal required.
  - `windows/start_tui.bat`, `windows/start_main.bat`, `windows/start_workbench.bat` — Windows launchers with auto-embedded-Python detection, fallback to `.venv` for dev machines.

- **`/memory` command — surgical long-term memory control**
  - `/memory` or `/memory status` — show injection state and stored counts (turns + memories).
  - `/memory pause` — stop injecting long-term memories into context without deleting any data; resume instantly with `/memory on`.
  - `/memory wipe` — permanently delete all stored turns and memories while preserving the database structure (surgical alternative to `/wipe` which nukes the entire workspace).
  - Implemented via `BridgeContextAssembler.memory_injection_enabled` flag and new `BridgeMemoryStore.clear_all()` method.

### 🐛 Fixed

- **Agent starts in LOCAL MODE when Telegram token is missing** — previously a missing/empty bot token caused a hard crash at startup. Now the agent starts cleanly in Workbench + TUI only mode, allowing onboarding to guide the user through token setup without re-launching.
- **`web_search` returning "Unknown error"** — when a tool call succeeds but the model returns empty text (e.g. `brave_api_key` missing), the runtime now surfaces a clear diagnostic message instead of a generic "Unknown error".
- **TUI connecting to wrong HASHI instance** — TUI now reads `workbench_port` from `agents.json` instead of using a hardcoded port (18800), preventing cross-instance contamination when multiple HASHI instances run on the same machine.
- **`rich` and `textual` missing from requirements** — added to `requirements.txt`; TUI now installs cleanly from a fresh checkout.
- **Python embedded runtime cannot find project modules** — fixed `._pth` file to include `..` (project root), resolving `ModuleNotFoundError: No module named 'orchestrator'` on USB deployments.

---

## [1.2.0-beta] - 2026-03-21

### ✨ Added

- **Browser Tool — all agents can now control a real web browser** (`tools/browser.py`, `tools/browser_cli.py`)
  - **6 browser actions** available to every agent regardless of backend:
    - `browser_screenshot` — navigate to any URL and capture a PNG screenshot (base64)
    - `browser_get_text` — render page with full JS execution and extract visible text (handles SPAs, dashboards)
    - `browser_get_html` — return fully-rendered post-JS HTML for DOM inspection
    - `browser_click` — click any element by CSS selector
    - `browser_fill` — fill form fields by CSS selector, with optional Enter-to-submit
    - `browser_evaluate` — run arbitrary JavaScript and return the result
  - **Two browser modes:**
    - *Standalone mode* (default) — launches a clean headless Chromium via Playwright
    - *CDP mode* — attaches to the user's already-running Chrome (`--cdp-url http://localhost:9222`), reusing all existing cookies, sessions, and login state
  - **Universal access via CLI wrapper** (`tools/browser_cli.py`):
    - Claude CLI, Gemini CLI, and Codex CLI agents invoke the browser through their `bash` tool
    - `python tools/browser_cli.py screenshot --url <url> [--cdp-url ...] [--out file.png]`
    - All 6 actions supported; `--out` saves screenshots as PNG files
  - **OpenRouter API agents** use the native tool schema (`browser_screenshot` etc.) via `ToolRegistry` — add to `agents.json` `tools.allowed` list
  - **Cross-platform**: auto-detects Chrome/Chromium on Linux, macOS, and Windows/WSL
  - Playwright listed in `requirements.txt` (optional dependency); run `playwright install chromium` once

---

## [1.2.0-alpha] - 2026-03-20

### ✨ Added

- **V2.2 Tool Execution Layer — OpenRouter/API agents now have local action capabilities**
  - New `tools/` package: `schemas.py` (JSON Schema definitions), `builtins.py` (executors), `registry.py` (`ToolRegistry` dispatcher), `__init__.py`.
  - **11 built-in tools** available to OpenRouter-backed agents:
    - `bash` — run shell commands (sandboxed to workspace, timeout + blocked-pattern controls)
    - `file_read` — read files with offset/limit pagination
    - `file_write` — write/create files (size-capped, parent dirs auto-created)
    - `file_list` — list directories with glob filter and recursive option
    - `apply_patch` — apply unified diff patches to files (dry-run validated before apply)
    - `process_list` — list running processes filtered by name (requires `psutil`)
    - `process_kill` — send SIGTERM/SIGKILL to a process by PID
    - `telegram_send` — send Telegram messages by chat_id or HASHI agent_id
    - `http_request` — arbitrary HTTP requests (GET/POST/PUT/DELETE/PATCH) for external API calls
    - `web_search` — Brave Search API integration (requires `brave_api_key` in `secrets.json`)
    - `web_fetch` — fetch any URL and return content as Markdown
  - `adapters/openrouter_api.py`: full tool loop — model proposes tool calls → bridge executes → results returned → model continues, up to `max_loops` iterations. Tool call streaming accumulated correctly across chunks.
  - `adapters/base.py`: `BackendResponse` gains `tool_calls` and `stop_reason` fields.
  - `orchestrator/flexible_backend_manager.py`: auto-attaches `ToolRegistry` when backend config contains a `tools` key.
  - Tool enablement is per-agent in `agents.json` via `tools.allowed` list and `tools.max_loops`. No `tools` key = fully backward compatible.

---

## [1.2.0-alpha] - 2026-03-20

### ✨ Added

- **`/dream` skill — nightly AI memory consolidation** (`skills/dream/`): agents can now "dream" at 01:30 daily, using an LLM to reflect on the day's transcript, extract important memories into `bridge_memory.sqlite`, and optionally update `AGENT.md` with behavioral insights. Includes snapshot-based `/skill dream undo` (no LLM required) for morning rollback, a persistent `dream_log.md`, and on/off toggle via `tasks.json` cron with `action: "skill:dream"`.

### 🔧 Fixed

- **Force-stop now kills entire process tree** — `/stop` previously only killed the main PID; child processes (e.g. Node.js workers spawned by Gemini CLI) stayed alive and held stdout/stderr pipes open, permanently blocking the queue processor.
  - `adapters/base.py`: `force_kill_process_tree` now uses `os.killpg()` on Linux to kill the whole process group.
  - `adapters/gemini_cli.py`: subprocess launched with `start_new_session=True`; active read tasks tracked in `self._active_read_tasks` and cancelled on `shutdown()`.
  - `adapters/claude_cli.py`: same read-task cancellation fix applied.
  - `adapters/codex_cli.py`: same `start_new_session=True` + read-task cancellation fix applied.

---

## [1.1.0] - 2026-03-18

### ✨ Highlights
- **/new is now truly bare** (stateless): no Bridge FYI injection and no automatic doc/README reading.
- A **clear v2 roadmap** has been documented under `docs/ROADMAP.md`.

### 🔧 Fixed
- `/new` semantics: fresh session starts without Bridge primer injection; agents follow only their workspace `agent.md`.

### 🗺️ Roadmap
- v2 upgrade outcomes are tracked in: `docs/ROADMAP.md`

---

## [1.0.1] - 2026-03-15

### 🔧 Fixed

- **Author Attribution** — Restored correct author credit in startup banner
  - Fixed: "© 2026 Barry Li" (was incorrectly showing "HASHI Team")
  - Fixed: "Designed by Barry Li" in both English and Japanese (デザインド・バイ・バリー・リー)

---

## [1.0.0] - 2026-03-15

### 🎉 Initial Release

First public release of HASHI (ハシ / 橋) — Universal AI Agent Orchestration Platform.

#### ✨ Added

**Core Features:**
- **Multi-Backend Support** — Gemini CLI, Claude CLI, Codex CLI, OpenRouter API
- **Multi-Agent Orchestration** — Run multiple specialized agents simultaneously
- **Universal Orchestrator** — Single process managing all agent runtimes
- **Flexible Backend Manager** — Switch backends mid-conversation
- **No Token Storage** — Privacy-first design using CLI authentication

**Onboarding System:**
- Multi-language guided setup (9 languages: EN, JP, CN-S, CN-T, KR, DE, FR, RU, AR)
- Automatic backend detection (Gemini, Claude, Codex)
- AI Ethics & Human Well-being Statement
- First-run agent creation wizard

**Transports:**
- Telegram bot integration with inline keyboards
- WhatsApp integration (multi-agent routing)
- Workbench (local React web UI)

**Skills System:**
- Three skill types: Action, Prompt, Toggle
- Markdown-first skill definitions
- Modular and extensible design
- Inline keyboard skill selector

**Scheduler (Jobs):**
- Heartbeat tasks (periodic checks)
- Cron jobs (scheduled tasks)
- Skill-based automation
- Per-agent job configuration

**Memory System:**
- Vector-based semantic search
- Long-term memory storage
- Context assembly with retrieval
- `/remember`, `/recall`, `/forget` commands

**Handoff System:**
- `/handoff` command for context recovery
- Project state preservation
- Session continuity across compressions

**Workbench UI:**
- Multi-agent chat interface
- Real-time transcript polling
- File and media upload support
- System status display

**Commands:**
- `/start`, `/stop`, `/restart` — Runtime control
- `/handoff` — Context restoration
- `/skill` — Skills management
- `/heartbeat`, `/cron` — Job management
- `/remember`, `/recall`, `/forget` — Memory commands
- `/export` — Daily transcript export
- `/status`, `/help` — Information commands

**Documentation:**
- Comprehensive README (human + AI readable)
- INSTALL.md (installation guide)
- SKILLS_SYSTEM_DESIGN.md (skills architecture)
- LICENSE (MIT)
- Multi-language onboarding prompts

**Development & Deployment:**
- Cross-platform support (Windows, Linux; macOS untested)
- Single-instance locking
- Hot restart capability
- API Gateway (optional external API)
- PyPI packaging (experimental)
- npm packaging (global CLI)

#### 🏗️ Architecture

- **Backend Adapters:** Unified interface for Gemini, Claude, Codex, OpenRouter
- **Transport Layer:** Telegram, WhatsApp, Workbench API
- **Orchestrator Pattern:** Central runtime with per-agent queues
- **Skills Manager:** Markdown-based modular capabilities
- **Task Scheduler:** Heartbeat + cron job automation
- **Memory Index:** Vector similarity search for context retrieval

#### 📦 Packaging

- PyPI-ready (setup.py + pyproject.toml + MANIFEST.in)
- npm-ready (package.json + CLI wrappers)
- Example configuration files included
- .gitignore for runtime files

#### 🛡️ Security & Privacy

- No OAuth token storage (uses CLI authentication)
- Local-only deployment by default
- API Gateway disabled by default
- Secrets file excluded from repository
- Runtime state excluded from version control

#### 🌍 Internationalization

Onboarding available in 9 languages:
- English
- 日本語 (Japanese)
- 简体中文 (Simplified Chinese)
- 繁體中文 (Traditional Chinese)
- 한국어 (Korean)
- Deutsch (German)
- Français (French)
- Русский (Russian)
- العربية (Arabic)

#### 🎨 Philosophy

> 「橋」は「知」を繋ぎ、「知」は未来を拓く。  
> _The Bridge connects Intellect; Intellect opens the future._

HASHI embodies the "Vibe-Coding" methodology:
- **Built with Vision** — Human-directed system design
- **Written by AI** — Every line generated by Claude, Gemini, Codex
- **Reviewed by AI** — Cross-reviewed by multiple AI systems
- **Directed by Human** — Operational judgment and iteration by the developer

#### ⚠️ Known Limitations

- Beta stability — expect edge cases
- Local deployment recommended (API Gateway lacks authentication)
- Not optimized for high-volume production use
- WhatsApp transport experimental on some platforms

#### 📚 Documentation Credits

Special thanks to [OpenClaw] by Peter Steinberg for inspiration and foundational concepts.

---

## [Unreleased] — v1.1-upgrades branch

### ✨ Added

- **Agent Modes: Flex and Fixed** — `orchestrator/flexible_agent_runtime.py`, `adapters/claude_cli.py`
  - Added `/mode [flex|fixed]` command to toggle between stateless context injection (flex) and continuous CLI session persistence (fixed).
  - In **Fixed Mode**, the bridge delegates context management to the native CLI backend (e.g., Claude CLI's `--resume`), reducing token overhead by passing only incremental prompts without re-injecting full system/memory context.
  - Added mode enforcement: Backend switching is disabled while in fixed mode to prevent context fragmentation.

- **Status Dashboard Upgrade** — `orchestrator/flexible_agent_runtime.py`
  - `/status` now displays the current mode (flex/fixed) and shortened session ID.
  - `/status full` now includes a dedicated monitoring row for `Mode` and full `Session ID`.

- **Time-Awareness (FYI Injection)** — `orchestrator/bridge_memory.py`
  - Added `get_last_user_turn_ts()` — retrieves timestamp of the user's last message from the `turns` table
  - Added `_build_time_fyi()` — computes current time + elapsed gap since last user message
  - Injected as a soft one-line note into every prompt just before the user message:
    ```
    [FYI: You received this message at 12:07 AM. Last message from user was at 11:52 PM — 15m ago.]
    ```
  - Gap formatting: seconds / minutes / hours / days — human-readable
  - Agents now feel the natural rhythm of conversation without being told explicitly

- **Delete Job Button** — `workbench/backend/api.py`, `skill_manager.py:303`
  - Implemented `delete_job()` method in SkillManager for job deletion
  - Added `/jobs` UI delete button with confirmation
  - Jobs can now be removed directly from Workbench

- **Jobs UI Global Redesign** — `workbench/frontend/Jobs.jsx`
  - Changed all button labels to English for global accessibility: `Run`, `ON`, `OFF`, `Delete`
  - Implemented responsive two-column grid layout for better scaling with multiple jobs
  - Optimized button spacing and text overflow handling


### 🔧 Fixed

- **Onboarding Agent Check** — `bin/bridge-u.sh`, `main.py`
  - Onboarding is now considered complete if *any* agent is configured, rather than strictly requiring `hashiko`.

- **System Prompt Slots Not Injecting** — `adapters/flexible_agent_runtime.py:131, 2425`
  - Fixed: `BridgeContextAssembler` was created without `sys_prompt_manager=` parameter
  - Result: Active slot texts from `/sys 1 on` were saved but never injected into the model's context
  - Solution: Added `sys_prompt_manager=self.sys_prompt_manager` to both instantiation points
  - Verification: System prompt slots now properly appear in the final assembled context

- **Backend switching silent failure** — `adapters/flexible_backend_manager.py`
  - Fixed: `/backend` → Gemini switch silently stayed on Claude due to unsupported parameter
  - Added missing parameter support so switching actually completes

- **Model change not persisting** — `adapters/flexible_backend_manager.py`
  - Fixed: `AttributeError` on `persist_state()` — method was named `_save_state` (private)
  - Added public `persist_state()` delegate so model selection survives restarts

---

## [1.0.1] - 2026-03-15


