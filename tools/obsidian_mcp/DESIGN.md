# Obsidian Tool Pack — Design Document

**Version:** v1.0
**Date:** 6 April 2026
**Author:** 小颖 (Ying) — HASHI Academic Research Agent
**Status:** Design / Ready for Implementation

---

## 1. Purpose

This tool pack gives any HASHI agent direct, programmatic access to an Obsidian vault via the **Obsidian Local REST API** plugin.

Key design principle: **backend-agnostic**. These tools are registered in HASHI's tool registry and can be called by any LLM backend (Claude, GPT-4, Gemini, local models, etc.). The tool pack has no dependency on Claude Code, Claude Desktop, or any specific MCP client.

---

## 2. Architecture

```
HASHI Agent (any LLM backend)
        │
        │  tool_call: obsidian_read_note(path=...)
        ▼
  HASHI ToolRegistry
        │
        ▼
  tools/obsidian_mcp/executor.py
        │  HTTP GET /vault/{path}
        ▼
  Obsidian Local REST API Plugin
  (running on Windows, port 27123)
        │
        ▼
  Obsidian Vault (plain Markdown files)
```

**Network path (WSL → Windows):**
WSL2 with mirrored networking accesses Windows services at `127.0.0.1`. The Obsidian REST API plugin listens on Windows localhost, so no special routing is needed.

---

## 3. Prerequisites

### On Windows (Obsidian side)
1. Obsidian installed and vault open
2. Community plugin **"Local REST API"** installed and enabled
   (Settings → Community Plugins → Browse → search "Local REST API")
3. In the plugin settings: generate an **API Key**, note the **port** (default: 27123)

### On HASHI (Linux/WSL side)
1. Add credentials to `secrets.json`:
   ```json
   {
     "obsidian_api_key": "your-api-key-here",
     "obsidian_base_url": "http://127.0.0.1:27123"
   }
   ```
2. Install Python dependency: `pip install httpx` (already in most HASHI environments)
3. Register tool schemas in `tools/schemas.py` (see Section 6)
4. Register executors in `tools/registry.py` (see Section 7)
5. Add `"obsidian_*"` to the agent's allowed tools in `agents.json`

---

## 4. Tool Inventory

| Tool Name | Method | Description |
|-----------|--------|-------------|
| `obsidian_read_note` | GET `/vault/{path}` | Read a note's full content |
| `obsidian_write_note` | PUT `/vault/{path}` | Create or overwrite a note |
| `obsidian_append_note` | POST `/vault/{path}` | Append content to an existing note |
| `obsidian_list_folder` | GET `/vault/{folder}/` | List notes and subfolders in a directory |
| `obsidian_search` | POST `/search/simple/` | Full-text search across the vault |
| `obsidian_get_active` | GET `/active/` | Get the currently open note in Obsidian |
| `obsidian_open_note` | POST `/open/{path}` | Tell Obsidian to open a specific note |

**Phase 2 (future):**
- `obsidian_dataview_query` — execute a Dataview DQL query (requires Dataview plugin API)
- `obsidian_create_paper_note` — create a note using the Paper Naming Protocol v1.1

---

## 5. Tool Schemas (OpenAI function-calling format)

See `schemas.py` in this folder.

---

## 6. Integration with tools/schemas.py

To activate, import and extend HASHI's global schema list:

```python
# In tools/schemas.py, add at the bottom:
from tools.obsidian_mcp.schemas import OBSIDIAN_TOOL_SCHEMAS
TOOL_SCHEMAS.extend(OBSIDIAN_TOOL_SCHEMAS)
```

---

## 7. Integration with tools/registry.py

In `ToolRegistry.dispatch()`, add the obsidian cases:

```python
from tools.obsidian_mcp.executor import ObsidianClient

# In __init__:
self._obsidian = None  # lazy-initialized

# In dispatch():
if tool_name.startswith("obsidian_"):
    if self._obsidian is None:
        self._obsidian = ObsidianClient(
            base_url=self.secrets.get("obsidian_base_url", "http://127.0.0.1:27123"),
            api_key=self.secrets.get("obsidian_api_key", ""),
        )
    return await self._obsidian.dispatch(tool_name, args)
```

---

## 8. Security Notes

- API key is stored in HASHI `secrets.json` (not in agent config or code)
- All vault access goes through the REST API — no direct filesystem access from HASHI
- The REST API plugin enforces its own access control
- Note contents are sent to the LLM backend as part of context — consistent with how HASHI handles all tool output

---

## 9. Agent Configuration Example

In `agents.json`, for the academic research agent (小颖):

```json
{
  "name": "ying",
  "tools": ["file_read", "file_write", "bash", "web_search",
            "obsidian_read_note", "obsidian_write_note",
            "obsidian_append_note", "obsidian_list_folder",
            "obsidian_search", "obsidian_get_active"]
}
```

---

## 10. Workflow Example

**Task:** Create a paper note following Paper Naming Protocol v1.1

```
Agent calls: obsidian_list_folder(folder="Papers/")
→ checks for filename collision

Agent calls: obsidian_write_note(
    path="Papers/Smith_2023_carbon_emission_accounting_audit.md",
    content="---\ntitle: ...\nauthors: ...\n---\n\n![[Smith_2023_...pdf]]\n\n# Notes\n"
)
→ note created, ready for PDF attachment
```
