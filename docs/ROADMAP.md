# HASHI — Roadmap

> High-level roadmap only. Keep it lightweight and current.

---

## v1.1 (Status)

- `v1.1-debugging` is now considered **completed** (stabilization + semantics fixes).
- Ongoing work can continue on separate feature branches (e.g., packaging), without redefining v1.1 semantics.

---

## v2 Upgrade Roadmap (Target Outcomes)

> v2 defines what the product should become. Not “do it now”, but “what good looks like”.

### V2.1 — CLI-first continuity for execution backends (speed + token savings)
- Restore the earliest best-practice behavior for each local coding CLI backend (Gemini/Claude/Codex):
  - continuity relies primarily on the **CLI’s own continuous sessions**
  - Bridge sends **incremental prompts** by default (no large compressed context blocks)
  - role/habits are defined by the CLI’s native system mechanism (e.g., `GEMINI_SYSTEM_MD`, project-level `claude.md`, etc.)
- Bridge-managed transcript/handoff remains available but is **explicit** (user-triggered), not always injected.

### V2.2 — Toolbox for OpenRouter/API agents (local actions)
- Add a controlled, auditable **tool execution layer** for API-only backends (e.g., OpenRouter):
  - file read/write, directory listing, command execution (with allowlists/confirmations), log collection, etc.
  - mirror OpenClaw-style tool calls: model proposes actions → bridge executes → results returned.

### V2.3 — Mode switching (fixed ↔ flexible)
- Introduce a `/mode` command to switch an agent between:
  - **fixed** mode (continuous CLI session for local work)
  - **flex** mode (multi-backend switching for conversation + breadth)
- Goal: “conversation brain” and “local executor” feel like one coherent assistant.

---

## Notes
- This roadmap is outcome-based; implementation details live in dedicated design docs when work begins.
