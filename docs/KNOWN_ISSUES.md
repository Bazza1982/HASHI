# Known Issues

This document describes known issues and their workarounds in HASHI.

---

## Memory Contamination from CLI Integration

**Status:** Known Issue (By Design)  
**Severity:** Low  
**Affected Users:** Developers with heavy CLI customization

### Description

HASHI integrates directly with CLI-based AI coding agents (Claude Code, Gemini CLI, Codex CLI) by design. This tight integration enables powerful development workflows but has a side effect: **CLI memory systems may leak into HASHI agent conversations**.

### How It Happens

CLI tools like Claude Code maintain their own persistent memory systems:
- `~/.claude/projects/<project>/memory/` — Claude Code
- `GEMINI.md` / `.gemini/` — Gemini CLI  
- `AGENTS.md` / `.codex/` — Codex CLI

If you've used these tools to develop HASHI (or any project in the same directory) and stored personal preferences, those preferences may flow through to HASHI agents — even agents designed with "no memory" personas.

### Who Is Affected?

**Most users are NOT affected.** This only impacts users who:
1. Use CLI AI tools with auto-memory features
2. Have stored personal preferences or customizations in those tools
3. Run HASHI agents in the same project directory where they develop

Typical end users installing HASHI fresh will not have pre-existing CLI memories.

### Why This Is Not a Bug

HASHI's direct integration with CLI coding agent systems is intentional. It enables:
- Seamless development workflows
- Shared context between development and runtime
- Unified agent capabilities across environments

The trade-off is that CLI-level memory operates at a higher priority layer than HASHI's application-level memory.

### Solutions

**Option 1: Use HASHI's Memory System (Recommended)**

Rely on HASHI's well-designed memory architecture (`bridge_memory.py`) instead of CLI memory. HASHI's memory is:
- Controllable per-agent
- Scoped to workspaces
- Designed for multi-agent isolation

**Option 2: Clean Up CLI Memories**

Ask your HASHI agent to inspect and remove any CLI memories or preferences that shouldn't persist:

```
Claude Code:    ~/.claude/projects/<your-project>/memory/
Gemini CLI:     <project>/GEMINI.md or .gemini/
Codex CLI:      <project>/AGENTS.md or .codex/
```

Review these locations and remove any personal preferences you don't want shared with HASHI agents.

**Option 3: Separate Development and Runtime Directories**

Clone HASHI to a separate directory for running agents, keeping your development environment (with CLI memories) isolated from the runtime environment.

---

## Reporting New Issues

Found a new issue? Please report it at:  
**[GitHub Issues](https://github.com/Bazza1982/HASHI/issues)**

---

*Last updated: 2026-03-15*
