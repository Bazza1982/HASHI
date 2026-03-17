# HASHI — Roadmap

> High-level roadmap only. Keep it lightweight and current.

---

## v1.1 (Current)

- Stabilization and bugfixes continue on `v1.1-debugging`.
- Packaging work (Windows offline installer / desktop UX) is tracked separately from core stability.

---

## v2 Upgrade Roadmap (Target Outcomes)

> v2 is not “do it now”; it defines what the product should become.

### V2.1 — CLI-first continuity for execution backends (speed + token savings)
- Rewind/restore the *earliest best-practice* behavior for each local coding CLI backend (Gemini/Claude/Codex):
  - continuity should rely primarily on the **CLI’s own continuous sessions**
  - Bridge should send **incremental prompts** instead of large compressed context blocks by default
  - role/habits should be defined by the CLI’s native system mechanism (e.g., `GEMINI_SYSTEM_MD`, project-level `claude.md`, etc.)
- Bridge-managed transcript/handoff remains available, but is **explicit** (user-triggered) rather than always injected.

### V2.2 — Toolbox for OpenRouter/API agents (local actions)
- Add a controlled, auditable **tool execution layer** for API-only backends (e.g., OpenRouter):
  - file read/write, directory listing, command execution (with allowlists/confirmations), log collection, etc.
  - design can mirror OpenClaw-style tool calls: model proposes actions → bridge executes → results returned.

### V2.3 — Mode switching (fixed ↔ flexible)
- Introduce a `/mode` command to let an agent switch between:
  - **fixed** mode (best-practice continuous CLI session for local work)
  - **flex** mode (multi-backend switching for conversation + broad capability)
- The goal is to make “conversation brain” and “local executor” feel like one coherent assistant.

---

## Notes
- This roadmap is outcome-based; implementation details live in dedicated design docs when work begins.
