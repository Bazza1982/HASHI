# HASHI — Roadmap

> High-level roadmap only. Keep it lightweight and current.

---

## v1.1 (Completed)

- `v1.1-debugging` is now considered **completed** (stabilization + semantics fixes).

---

## v2 Upgrade Roadmap — ALL COMPLETED ✅

> All v2 target outcomes have been delivered as of 2026-03-20.

### V2.1 — CLI-first continuity for execution backends ✅
- CLI backends (Gemini/Claude/Codex) now rely on their own continuous sessions for continuity.
- Bridge sends incremental prompts by default; no large compressed context blocks.
- Role/habits defined via CLI-native system mechanisms (`GEMINI_SYSTEM_MD`, `claude.md`, etc.).
- Bridge-managed transcript/handoff remains available but is explicit (user-triggered).

### V2.2 — Toolbox for OpenRouter/API agents ✅
- Full tool execution layer implemented for OpenRouter-backed agents.
- Model proposes tool calls → bridge executes locally → results returned → model continues.
- **11 built-in tools:**
  - `bash` — run shell commands (sandboxed, timeout + blocklist controls)
  - `file_read` — read files with offset/limit pagination
  - `file_write` — write/create files (size-capped)
  - `file_list` — list directories with glob filter and recursive option
  - `apply_patch` — apply unified diff patches (dry-run validated)
  - `process_list` — list running processes by name (requires `psutil`)
  - `process_kill` — send SIGTERM/SIGKILL to a process by PID
  - `telegram_send` — send Telegram messages by chat_id or HASHI agent_id
  - `http_request` — arbitrary HTTP calls (GET/POST/PUT/DELETE/PATCH)
  - `web_search` — Brave Search API (requires `brave_api_key` in `secrets.json`)
  - `web_fetch` — fetch any URL and return content as Markdown
- Tool access is per-agent via `tools.allowed` in `agents.json`. No `tools` key = backward compatible.

### V2.3 — Mode switching (fixed ↔ flexible) ✅
- `/backend` command switches an agent between fixed CLI backends and flex OpenRouter backends.
- Backend switching is atomic: not committed until a valid model is selected.
- Rollback exists: previous backend restored if new one fails to initialize.
- Flex backend state (active backend, selected model, effort level) persists in `state.json`.

### V2.4 — Interactive TUI wrapper ✅
- `tui.py` launcher provides a split-panel terminal UI wrapping `main.py` as a subprocess.
- Log panel (upper ~80%): real-time stdout/stderr streaming with auto-scroll.
- Chat input bar (lower ~20%): sends messages to agents via HTTP API Gateway.
- Agent selector and status bar (agent name, backend, uptime, gateway reachability).
- Built with [Textual](https://github.com/Textualize/textual); `main.py` unchanged.
- Graceful degradation when API Gateway unavailable.

---

## Additional Features Delivered (v1.2-alpha)

### /dream Skill — Nightly Memory Consolidation
- Agents “dream” at 01:30 daily: LLM reflects on the day’s transcript, extracts memories into `bridge_memory.sqlite`, optionally updates `AGENT.md` with behavioral insights.
- Snapshot-based `/skill dream undo` for morning rollback (no LLM required).
- Persistent `dream_log.md` and on/off toggle via `tasks.json` cron.

### Process-Tree Force Stop
- `/stop` now kills the entire subprocess tree (not just the main PID) via `os.killpg()`.
- Prevents zombie child processes from holding stdout/stderr pipes open.

### /retry Persistence
- `/retry` resends the last prompt or reruns the last response across sessions.

---

## Notes
- This roadmap is outcome-based; implementation details live in dedicated design docs.
- Design docs: `docs/V2.2_TOOL_EXECUTION_PLAN.md`
