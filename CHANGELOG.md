# Changelog

All notable changes to HASHI will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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


