# Changelog

All notable changes to HASHI will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [3.2.0-alpha] - 2026-05-02

### ✨ Added

- **Slim core architecture** — `main.py` is now a slim process bootstrap/kernel wrapper, with frequently changed bridge behavior moved into hot-reloadable managers under `orchestrator/`.
  - Added manager boundaries for skill management, config administration, backend preflight, agent lifecycle, runtime services, hot reboot, startup, shutdown, and WhatsApp control.
  - Hot reboot now rebuilds managers transaction-style after module reload: replacement managers are built first, then committed only if the full set initializes successfully.
  - Long-lived live handles remain on the kernel, including Workbench API, API Gateway, scheduler, WhatsApp transport, agent directory, and runtime list state.
  - Accepted with cold restart, `/reboot min`, `/reboot max`, Workbench/API health checks, and full `pytest` validation.
- **Wrapper Agent Mode** — new `wrapper` runtime mode pairs a functional core backend/model with a stateless wrapper backend/model for final user-facing persona/style rewriting.
  - Added `/mode wrapper`, `/core`, `/wrap`, and `/wrapper` configuration commands, with Telegram inline controls for core model, wrapper model, context window, and persona/style slots.
  - Default wrapper configuration remains `claude-cli / claude-haiku-4-5`; the wrapper picker also exposes Gemini, DeepSeek, and OpenRouter choices while avoiding expensive Claude Opus as a recommended wrapper option.
  - Foreground and background delivery paths now use wrapper final text for user-visible replies, request listeners, transfer suppression, handoff, project chat, voice replies, and HChat routing where appropriate.
  - Core prompt memory stores core raw assistant output, while `core_transcript.jsonl`, visible transcript writes, and audit payloads remain separated so wrapper persona does not drift back into the core model.
  - `/verbose on` now shows a labeled wrapper trace containing core raw output, wrapper final output, wrapper status, latency, and fallback reason; `/verbose off` shows only the final reply.
  - `/reset CONFIRM` preserves wrapper config and wrapper prompt slots, matching `/sys` preservation behavior; `/wipe CONFIRM` remains a hard workspace clear.
- **Browser gateway alpha** — local browser gateway package and test coverage for browser-facing bridge capabilities.
- **OLL HASHI Chrome extension scaffold** — extension files and implementation plan for browser bridge workflows.
- **Private wake-on-LAN tooling** — local helper and tests for private wake-on-LAN flows.
- **Workzone support** — project/workspace zone helper module and tests.

### 🔧 Fixed

- **Runtime list identity during reboot** — stopping an agent now mutates `kernel.runtimes` in place, preserving references held by Workbench API and AgentDirectory after `/reboot min` or `/reboot max`.
- **Startup task result logging** — unexpected exceptions while reading startup task results are now logged instead of being silently swallowed.
- **Codex CLI completion hang** — `adapters/codex_cli.py` now accepts a completed Codex turn as a valid finish signal even if the outer CLI process does not exit immediately.
  - Existing success path is preserved: normal subprocess exit still completes exactly as before.
  - Added a backward-compatible fallback: if Codex emits the final `agent_message` and `turn.completed`, HASHI gives the CLI a short grace window to exit, then force-closes it and returns the completed answer instead of hanging in `busy`.
  - Idle timeout enforcement is now active for Codex CLI requests, preventing stalled subprocesses from waiting until the hard timeout.
  - Added regression tests covering both the post-`turn.completed` forced-exit path and idle-timeout path.
- **Remote handshake alias false-positive** — `remote/protocol_manager.py` now verifies that a handshake success response comes from the expected `instance_id` before marking that peer healthy.
  - Prevents stale or duplicated bootstrap endpoints from making one instance appear online when a different instance answers on the same host/port.
  - Stops `/remote list` from inheriting another peer's agents and recent handshake timestamp through alias collisions.
- **Cross-instance Hchat auto-reply loop** — `agent_runtime.py` and `flexible_agent_runtime.py` now share the same explicit reply-body semantics and suppress automatic hchat replies when the incoming hchat body is already a reply payload.
  - Flex agents now tag cross-instance auto-replies as `[hchat reply from ...]` before routing through `send_hchat`, matching the legacy runtime behavior.
  - Prevents cross-instance reply traffic from being rewrapped as a fresh hchat and bounced back indefinitely.
  - Keeps first-hop hchat behavior unchanged while adding a hard stop for reply-on-reply ping-pong.

## [3.1.0] - 2026-04-29

### ✨ Added

- **Claude Opus 4.7 support** — `claude-opus-4-7` added to `claude-cli` backend in both fixed and flex agent runtimes; `opus` / `claude-opus` aliases updated to point to the latest model
- **GPT-5.5 (Codex) support** — `gpt-5.5` added as the newest model in `codex-cli` backend; available in both fixed and flex agents
- **`max` effort level for Claude** — `claude-cli` now exposes the `max` reasoning effort tier (previously undocumented); valid values: `low`, `medium`, `high`, `xhigh`, `max`
- **`xhigh` effort level unified** — both `claude-cli` and `codex-cli` now correctly expose `xhigh` as the top effort tier; HASHI API model list updated accordingly

### 🔧 Fixed

- **Codex effort `extra_high` → `xhigh`** — the Codex CLI has never accepted `extra_high`; HASHI was silently passing an invalid value. Registry and normalize logic corrected to use `xhigh`, with backward-compatible auto-remapping so any existing config using `extra_high` or `extra` is transparently upgraded
- **Claude effort list was incomplete** — `claude-cli` efforts were hardcoded as `[low, medium, high]`; the CLI actually supports `xhigh` and `max`, both now registered and selectable via `/model` or `/backend`

### ⬆️ Upgraded

- **Codex CLI** — upgraded from `0.116.0` to `0.125.0` (`npm install -g @openai/codex`)

---

## [3.0.0-beta] - 2026-04-18

### ✨ Added

- **DeepSeek API Backend** — direct API adapter (`adapters/deepseek_api.py`) connecting to `api.deepseek.com/v1/`, with streaming, tool calls, and reasoning_content support. More cost-effective than routing through OpenRouter.
- **SafeVoice** — voice confirmation system preventing accidental command execution from speech-to-text errors
  - Voice messages are transcribed, displayed as preview text with [✅ Send] / [❌ Cancel] buttons
  - Default ON for all agents, toggleable per-agent via `/safevoice`
  - Preview expanded to 3500 characters (was 300), with truncation notice for longer messages
  - 60-second auto-discard timeout
- **Remote Backend Policy** — API backends (OpenRouter, DeepSeek) automatically blocked for automated requests (scheduler, HChat, transfers), preventing runaway costs. Only user-initiated requests allowed on remote backends.
- **Cross-Instance Agent Messaging (HChat v2)** — agents communicate across HASHI instances via Workbench API endpoints, with automatic routing based on `instances.json`
- **Agent Behavior Audit** — `scripts/generate_agent_behavior_audit.py` and `/skill agent_audit` for local-only daily behavior reports
- **`/loop` Command Redesign** — replaced hardcoded parsing with skill injection pattern; LLM autonomously creates cron entries from natural language task descriptions
- **`/long` ... `/end`** — buffer long Telegram messages across multiple fragments, submit as single message (5-minute auto-submit timeout)
- **`/say` TTS** — text-to-speech with multiple providers (Windows, Edge, Piper, Kokoro, Coqui)
- **Telegram File Sending** — all agents can send photos, documents, video, audio via `telegram_send_file` tool
- **Job Transfer System** — `/jobs transfer` supports same-instance and cross-instance job migration
- **Skill Environment Variables** — action skills receive `BRIDGE_ACTIVE_BACKEND` and `BRIDGE_ACTIVE_MODEL` via environment
- **Wiki Organisation** — Obsidian knowledge vault integration with daily tagging and weekly LLM curation
- **Hashi Remote** — cross-network agent communication with LAN discovery, Tailscale support, TLS encryption, and pairing-based auth

### 🔧 Fixed

- **Dream duplicate prevention** — mtime gate + content hash dedup prevents redundant dream runs from duplicate cron triggers
- **Scheduler timeout** — skill timeout increased from 30s to 300s for long-running skills like dream
- **Scheduler loop prevention** — failed skill runs now update `last_run` timestamp, preventing infinite retry loops
- **Dream legacy transcript handling** — forward scan for dated entries, tail scan for trailing undated entries

---

## [3.0.0-alpha] - 2026-04-04

### ✨ Added

#### Core Features
- **Ollama Local LLM Support** — agents can now use locally-hosted LLMs via Ollama
  - Per-engine memory injection tuning — smaller models get smaller context (`ollama-api`: 4 recent turns + 2 memories vs. `claude-cli`: 10 + 6)
  - Intelligent context scaling based on model size and performance characteristics

- **TUI Onboarding — first-run setup inside the terminal UI** (`tui_onboarding.py`, `windows/TUI_onboarding.bat`)
  - New standalone entry point that replaces the old `onboarding/onboarding_main.py` flow for users with a pre-configured `agents.json`.
  - Runs a guided first-run sequence directly inside the TUI window — no separate window or UI switch required.
  - **First-run flow:**
    1. Language selection (9 languages: English, Japanese, Simplified Chinese, Traditional Chinese, Korean, German, French, Russian, Arabic)
    2. AI Ethics & Human Well-being disclaimer (Enter to confirm)
    3. Mental health & AI relationship reminder (Enter to confirm)
    4. API key connectivity check — auto-detects OpenRouter (`sk-or-v1-...`) or DeepSeek (`sk-...`) key in `secrets.json` with a live ping test; prompts user to paste a key if none found or ping fails
    5. Generates `agents.json` from `agents.json.sample` (Hashiko only) if not present
    6. Writes `workspaces/hashiko/tui_onboarding_complete` completion marker
    7. Starts the HASHI bridge and transitions seamlessly into normal TUI chat
    8. Injects a first-run wakeup prompt so Hashiko greets the user in their selected language, asks for their name, and guides Telegram setup — all within the same TUI session
  - `tui/light_onboarding.py` — new module containing the `LightOnboardingPhase` state machine and helpers (language loading, disclaimer/wellbeing text extraction, API key detection and validation, `agents.json` bootstrapping, completion marker management)
  - `tui/app.py` extended with `onboarding_mode` parameter; existing TUI behavior unchanged when launched via `start_tui.bat`
  - USB packaging (`prepare_usb.bat`) picks up all new files automatically — no packaging changes needed

#### Habit-Based Self-Improvement System
- **Phase 5 habit evaluation dashboard** — richer evaluation summaries across agents, classes, and backends
  - `orchestrator/habits.py` now computes Wilson-style evidence quality, aggregates task/class/backend dashboard buckets, tracks timestamp-source coverage, and exports dashboard artifacts.
  - `scripts/habit_recommendations.py` adds a `dashboard` command and now prints dashboard artifact paths from `report`.
  - `skills/habits/` now exposes `/skill habits dashboard` as a bridge-native read-only surface.
  - `workspaces/lily/habit_reports/dashboard.md` and `workspaces/lily/habit_reports/dashboard.json` are exported on each report refresh.

- **Phase 4 habit governance surfaces** — shared pattern / protocol registry added to the habit system
  - `orchestrator/habits.py` now persists `shared_patterns` and `shared_pattern_changes`, supports promotion/retirement workflows, and exports a stable registry document.
  - `scripts/habit_recommendations.py` adds `shared-list`, `shared-promote`, and `shared-retire` commands for CLI governance.
  - `skills/habits/` now exposes bridge-native shared registry operations, with Lily-only enforcement for `shared promote` and `shared retire`.
  - `workspaces/lily/habit_reports/shared_registry.md` is exported as the readable registry view for promoted shared patterns / protocols.

#### Workflow & Tooling
- **Minato MCP integration (8-tier architecture)**
  - KASUMI tool delegation, artefact tools, project & Nagare tools, project action logging
  - Workbench UI enhancements for workflow visualization and project logging

- **Token audit system** — accurate token consumption tracking and cost analysis
- **Dream system improvements** — mtime gate, habits instant-active processing
- **Nagare-viz** — interactive workflow canvas and configuration panel
- **Obsidian MCP** — knowledge vault integration

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
- **`/skill habits` — habit recommendation governance surface** (`skills/habits/`): bridge-native entry for regenerating Lily habit reports, listing copy recommendations, and Lily-only approve/reject/apply actions for cross-agent habit copying.

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

## [v1.1-upgrades branch snapshot]

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
