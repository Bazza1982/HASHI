# Minato MCP Server Plan

## 1. Document Status

- Status: Implemented through Tier 8; this document now serves as a living architecture and rollout ledger
- Scope: Minato project-management MCP layer over the HASHI agent bridge
- Intended audience: system architect, API implementers, AI agents operating within Minato
- Last updated: 2026-04-05

---

## 2. Purpose

This document defines the complete architecture for a Minato MCP Server — a structured tool and resource surface that gives AI agents and developers programmatic access to all Minato project-management capabilities.

Minato is a project management system layered over HASHI. It owns:

- active project context (which project an agent is working within)
- Shimanto phases (ordered project lifecycle stages)
- Nagare workflows (multi-agent DAG execution graph)
- artefacts (both filesystem files and KASUMI objects)
- activity log (persistent record of actions, decisions, milestones, and chat exchanges)
- agent chat (hchat send and conversation history)
- system reference docs (agent-readable Minato documentation)

The purpose of exposing these as an MCP surface is to give agents the same power over the project layer that a human product manager has — without requiring agents to know the internal file paths, REST endpoint shapes, or logging conventions of the underlying system.

---

## 3. Executive Summary

### Decision 1: one server, multiple namespaces

The Minato MCP server exposes a single logical server surface, not separate servers for each capability domain.

Reason:

- real agent workflows span project context, workflow state, artefacts, and chat in a single task
- shared auth, session context, and audit should exist once
- agents should issue one `tools/list` call to discover everything Minato can do
- future capabilities (calendar, tickets, team members) can register into the same surface

The external namespace shape is:

- `minato://project/*`
- `minato://shimanto/*`
- `minato://nagare/*`
- `minato://artefacts/*`
- `minato://log/*`
- `minato://chat/*`
- `minato://docs/*`

### Decision 2: existing REST endpoints are the implementation layer

Minato already has a functioning REST API across two servers:

- `workbench/server/index.js` (Node.js, port 3001) — Workbench API
- `orchestrator/workbench_api.py` (Python aiohttp, port 18800) — Bridge API

The MCP server does not duplicate this logic. It wraps these endpoints as MCP tools and resources, adding:

- canonical tool names
- validated input/output schemas
- audit records per tool call
- unified error envelopes

### Decision 3: the MCP surface is delivered in tiers and tracked here

The first seven tiers of the Minato MCP surface are now implemented in `workbench/server/minato_mcp.js`. This document keeps the architecture contract, current implementation status, and remaining next-step work in one place so agents and engineers can see what is already live versus what still belongs to future tiers.

---

## 4. Architecture Overview

```text
AI Agent / HASHI Runtime
        |
        |  JSON-RPC / MCP-style requests
        v
+--------------------------------------------------+
|           Minato MCP Server                      |
|--------------------------------------------------|
| Common Middleware                                |
| - session context (active project)               |
| - schema validation                              |
| - audit log                                      |
| - error normalisation                            |
|--------------------------------------------------|
| Registry                                         |
| - resources                                      |
| - tools                                          |
| - prompt templates                               |
|--------------------------------------------------|
| Namespaces                                       |
| - project                                        |
| - shimanto                                       |
| - nagare                                         |
| - artefacts                                      |
| - log                                            |
| - chat                                           |
| - docs                                           |
+--------------------------------------------------+
        |
        +--> Workbench API (Node.js :3001)
        +--> Bridge API (Python aiohttp :18800)
        +--> workbench/data/projects/ (JSONL + Markdown logs)
        +--> workspaces/<agent>/projects/<slug>/ (per-agent chat logs)
        +--> filesystem artefacts
        +--> KASUMI MCP Server (for KASUMI object artefacts)
```

### Transport

- Protocol: JSON-RPC 2.0 over HTTP
- Base path: `/api/minato/mcp/v1`
- Endpoints:
  - `POST /api/minato/mcp/v1/tools/call`
  - `GET  /api/minato/mcp/v1/tools/list`
  - `POST /api/minato/mcp/v1/resources/read`
  - `GET  /api/minato/mcp/v1/resources/list`
  - `GET  /api/minato/mcp/v1/prompts/list`
  - `POST /api/minato/mcp/v1/prompts/read`
  - `POST /api/minato/mcp/v1/prompts/render`

---

## 5. Tool Catalog

Tools are mutating or procedural operations. All tool calls are logged in the project audit record.

---

### 5.1 `minato://project/*`

#### `project_list`

List all known projects with their slugs and display names.

**Maps to:** `GET /api/project-log/list` (Workbench API)

**Status:** Built

**Inputs:** none

**Output:**
```json
{
  "projects": [
    { "slug": "audit_shire_council", "name": "Audit — Shire Council" }
  ]
}
```

---

#### `project_get_state`

Get the current active context for a named project, including its known Shimanto phases and Nagare workflows derived from the most recent log entries.

**Maps to:** `GET /api/project-log?project=...&limit=1` (Workbench API), interpreted server-side

**Status:** Built

**Inputs:**
```json
{
  "project": "string (required) — display name or slug"
}
```

**Output:**
```json
{
  "project": "Audit — Shire Council",
  "slug": "audit_shire_council",
  "shimanto_phases": ["planning", "fieldwork"],
  "nagare_workflows": ["audit-report-v1"],
  "scope": "Q3 2026 performance audit",
  "last_activity": "2026-04-05T09:12:00Z"
}
```

---

#### `project_switch`

Set the active project context for subsequent tool calls in the session. Writes a session-scoped variable that the MCP server attaches to tool calls that require a project context.

**Maps to:** session state (no REST endpoint — MCP server manages this)

**Status:** Built

**Inputs:**
```json
{
  "project": "string (required) — display name or slug"
}
```

**Output:**
```json
{ "ok": true, "active_project": "Audit — Shire Council" }
```

---

### 5.2 `minato://shimanto/*`

Shimanto is the project phase model. Phases represent major lifecycle stages for a project (e.g. scoping, fieldwork, review, delivery). Each conversation message carries a `shimanto_phases` array in the MINATO CONTEXT header.

#### `shimanto_get_current_phase`

Return the Shimanto phases active in the most recent logged activity for the project.

**Maps to:** derived from `GET /api/project-log?project=...&limit=5`

**Status:** Built

**Inputs:**
```json
{
  "project": "string (required)"
}
```

**Output:**
```json
{
  "project": "Audit — Shire Council",
  "phases": ["planning", "fieldwork"]
}
```

---

#### `shimanto_list_phases`

Return the full ordered list of known phases for the project, derived from all log history.

**Status:** Built

**Inputs:**
```json
{
  "project": "string (required)"
}
```

**Output:**
```json
{
  "project": "Audit — Shire Council",
  "all_phases_seen": ["scoping", "planning", "fieldwork"],
  "current_phases": ["planning", "fieldwork"]
}
```

---

#### `shimanto_transition_phase`

Append a log entry recording a phase transition and update the session context. Does not enforce a fixed phase sequence — Minato treats phases as tags, not a strict state machine.

**Maps to:** `POST /api/project-log/entry` with `type: "milestone"` and updated `shimanto_phases`

**Status:** Built (via log/append tool — this is a convenience wrapper)

**Inputs:**
```json
{
  "project": "string (required)",
  "from_phases": ["string"],
  "to_phases": ["string"],
  "note": "string (optional)"
}
```

**Output:**
```json
{ "ok": true, "phases": ["fieldwork"] }
```

---

### 5.3 `minato://nagare/*`

Nagare is the HASHI multi-agent workflow orchestration engine. Workflows are defined as YAML DAGs, executed by `flow/engine/flow_runner.py`. Each step is assigned to an agent worker with a specific backend and model. Steps can run sequentially or in parallel.

#### `nagare_list_workflows`

List all known Nagare workflow IDs referenced in the project log.

**Maps to:** derived from `GET /api/project-log?project=...`

**Status:** Built

**Inputs:**
```json
{
  "project": "string (required)"
}
```

**Output:**
```json
{
  "project": "Audit — Shire Council",
  "workflows": ["audit-report-v1", "fieldwork-analysis-v2"]
}
```

---

#### `nagare_get_workflow_dag`

Return the full YAML schema definition of a Nagare workflow, including its steps, agent assignments, dependency graph, and pre-flight questions.

**Maps to:** filesystem read of `flow/workflows/<workflow_id>.yaml`

**Status:** Built

**Inputs:**
```json
{
  "workflow_id": "string (required)"
}
```

**Output:**
```json
{
  "workflow_id": "audit-report-v1",
  "name": "Audit Report",
  "version": "1.0.0",
  "steps": [
    {
      "id": "analyse",
      "name": "Analyse fieldwork notes",
      "agent": "analyst_01",
      "depends": [],
      "status": "completed"
    },
    {
      "id": "draft",
      "name": "Draft report",
      "agent": "writer_01",
      "depends": ["analyse"],
      "status": "running"
    }
  ],
  "current_run_id": "run_abc123"
}
```

---

#### `nagare_get_run_status`

Return the execution state of the most recent (or specified) run for a workflow, including per-step status and artifact paths.

**Maps to:** filesystem read of `flow/runs/<run_id>/state.json`

**Status:** Built

**Inputs:**
```json
{
  "workflow_id": "string (required)",
  "run_id": "string (optional — defaults to most recent run)"
}
```

**Output:**
```json
{
  "run_id": "run_abc123",
  "workflow_id": "audit-report-v1",
  "started_at": "2026-04-05T07:00:00Z",
  "status": "running",
  "steps": [
    { "id": "analyse", "status": "completed", "completed_at": "2026-04-05T07:15:00Z" },
    { "id": "draft", "status": "running", "started_at": "2026-04-05T07:16:00Z" }
  ]
}
```

---

#### `nagare_update_step_status`

Manually update the status of a workflow step (e.g. mark as completed after human review, or force a retry). Intended for human-in-the-loop quality gates.

**Maps to:** filesystem write to `flow/runs/<run_id>/state.json`

**Status:** Built

**Inputs:**
```json
{
  "run_id": "string (required)",
  "step_id": "string (required)",
  "status": "string (required) — completed | failed | pending | skipped",
  "note": "string (optional)"
}
```

**Output:**
```json
{ "ok": true, "step_id": "draft", "status": "completed" }
```

---

### 5.4 `minato://artefacts/*`

Artefacts are outputs produced during project work. They can be:

- **Filesystem files**: plain documents, reports, YAML definitions, code
- **KASUMI objects**: Nexcel workbooks, Wordo documents (accessed via KASUMI MCP Server)

#### `artefacts_list`

List known artefacts linked to a project.

**Status:** Built

**Inputs:**
```json
{
  "project": "string (required)",
  "type": "string (optional) — file | kasumi | any"
}
```

**Output:**
```json
{
  "project": "Audit — Shire Council",
  "artefacts": [
    {
      "id": "art_001",
      "name": "Audit Report Draft v1.docx",
      "type": "file",
      "path": "/home/lily/projects/audit/report_draft_v1.docx",
      "linked_at": "2026-04-04T11:20:00Z",
      "nagare_step": "draft"
    },
    {
      "id": "art_002",
      "name": "Fieldwork Data",
      "type": "kasumi",
      "kasumi_id": "workbook_xyz",
      "kasumi_module": "nexcel"
    }
  ]
}
```

---

#### `artefacts_create`

Register a new artefact record linked to the current project. For filesystem artefacts, this records the path. For KASUMI artefacts, this records the KASUMI object ID and module.

**Status:** Built

**Inputs:**
```json
{
  "project": "string (required)",
  "name": "string (required)",
  "type": "string (required) — file | kasumi",
  "path": "string (required if type=file)",
  "kasumi_id": "string (required if type=kasumi)",
  "kasumi_module": "string (required if type=kasumi) — nexcel | wordo",
  "nagare_step": "string (optional)",
  "note": "string (optional)"
}
```

**Output:**
```json
{ "ok": true, "artefact_id": "art_003" }
```

---

#### `artefacts_read`

Read the contents of a filesystem artefact. For KASUMI artefacts, this delegates to the KASUMI MCP Server.

**Status:** Built

**Inputs:**
```json
{
  "artefact_id": "string (required)"
}
```

**Output:**
```json
{
  "artefact_id": "art_001",
  "name": "Audit Report Draft v1.docx",
  "type": "file",
  "content": "string (text content for readable files)",
  "size_bytes": 42000,
  "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
}
```

---

#### `artefacts_link`

Link an existing artefact to an additional project, Nagare step, or Shimanto phase.

**Status:** Built

**Inputs:**
```json
{
  "artefact_id": "string (required)",
  "project": "string (optional)",
  "nagare_step": "string (optional)",
  "shimanto_phase": "string (optional)"
}
```

**Output:**
```json
{ "ok": true }
```

---

#### `artefacts_kasumi_call`

Delegate a mutation or read-style tool call to the KASUMI MCP layer for an already-registered KASUMI artefact. This keeps the project context, artefact registry, and action log on the Minato side while allowing Nexcel or Wordo operations to run in the downstream KASUMI service.

**Maps to:** KASUMI MCP `/tools/call` via Minato-side HTTP delegation

**Status:** Built

**Inputs:**
```json
{
  "artefact_id": "string (required) — must refer to a type=kasumi artefact",
  "tool_name": "string (required) — must start with nexcel_, wordo_, or system_ depending on module",
  "arguments": "object (optional)",
  "note": "string (optional)"
}
```

**Output:**
```json
{
  "ok": true,
  "artefact_id": "art_001",
  "kasumi_id": "wb_006",
  "tool_name": "nexcel_new_sheet",
  "result": {}
}
```

---

### 5.5 `minato://log/*`

The activity log is the authoritative, persistent record of all project work. It has two physical representations:

- **JSONL** (`workbench/data/projects/<slug>/conversations/<YYYY-MM-DD>.jsonl`) — machine-readable, queryable
- **Markdown** (`workbench/data/projects/<slug>/log/<YYYY-MM-DD>.md`) — human-readable daily log

#### `log_query`

Query the project JSONL log. Returns entries in chronological order.

**Maps to:** `GET /api/project-log?project=...&limit=...` (Workbench API)

**Status:** Built

**Inputs:**
```json
{
  "project": "string (required)",
  "limit": "integer (optional, default 100, max 1000)",
  "since": "string (optional) — ISO date string YYYY-MM-DD"
}
```

**Output:**
```json
{
  "entries": [
    {
      "ts": "2026-04-05T09:00:00Z",
      "session_id": "sess_abc",
      "direction": "outbound",
      "agent": "akane",
      "user": "user",
      "text": "Draft the executive summary",
      "project": "Audit — Shire Council",
      "shimanto_phases": ["fieldwork"],
      "nagare_workflows": ["audit-report-v1"],
      "scope": "Q3 2026 performance audit"
    }
  ],
  "count": 1
}
```

---

#### `log_append`

Append a structured activity entry to the project log. This writes both the JSONL machine record and the Markdown human-readable log entry.

**Maps to:** `POST /api/project-log/entry` (Workbench API)

**Status:** Built

**Inputs:**
```json
{
  "type": "string (required) — chat | action | decision | milestone | note",
  "project": "string (required)",
  "agent": "string (optional)",
  "user": "string (optional, default 'user')",
  "ts": "string (optional) — ISO timestamp",
  "shimanto_phases": ["string"],
  "nagare_workflows": ["string"],
  "summary": "string (required unless details or excerpt provided)",
  "details": "string | string[] (optional)",
  "excerpt": {
    "from": "string (optional)",
    "to": "string (optional)"
  }
}
```

**Output:**
```json
{ "ok": true }
```

**Entry type semantics:**

| type | when to use |
|------|-------------|
| `chat` | a logged conversation exchange between user and agent |
| `action` | an agent completed a concrete task (file written, tool called) |
| `decision` | a project or design decision was made |
| `milestone` | a major phase or step was reached |
| `note` | a freeform observation, reminder, or context record |

---

#### `log_project_chat`

Return the per-agent, per-project chat log stored in the agent's workspace directory. This is the agent-side record maintained by `project_chat_logger.py`.

**Maps to:** `GET /api/project-chat/{name}/{project}` (Bridge API, port 18800)

**Status:** Built

**Inputs:**
```json
{
  "agent": "string (required) — agent name",
  "project": "string (required) — project display name or slug",
  "limit": "integer (optional, default 100)"
}
```

**Output:**
```json
{
  "entries": [
    {
      "ts": "2026-04-05T09:00:00Z",
      "source": "hchat",
      "project": "Audit — Shire Council",
      "shimanto_phases": ["fieldwork"],
      "nagare_workflows": [],
      "scope": "Q3 2026",
      "user": "Draft the executive summary",
      "assistant": "Here is a draft executive summary..."
    }
  ],
  "count": 1
}
```

---

### 5.6 `minato://chat/*`

#### `chat_send`

Send a message to a named HASHI agent via hchat. Optionally injects a MINATO CONTEXT header so the exchange is automatically tagged and logged against the active project.

**Maps to:** `POST /api/chat` (Workbench API)

**Status:** Built

**Inputs:**
```json
{
  "agent_id": "string (required)",
  "text": "string (required)",
  "inject_context": "boolean (optional, default true) — whether to prepend MINATO CONTEXT header",
  "project": "string (optional) — overrides session active project"
}
```

**Output:**
```json
{ "ok": true, "queued": true }
```

Note: hchat is fire-and-forget. Use `chat_get_history` to read responses.

---

#### `chat_get_history`

Retrieve recent conversation messages from an agent's transcript.

**Maps to:** `GET /api/transcript/:agentId` (Workbench API)

**Status:** Built

**Inputs:**
```json
{
  "agent_id": "string (required)",
  "limit": "integer (optional, default 50, max 200)"
}
```

**Output:**
```json
{
  "messages": [
    {
      "role": "user",
      "content": "Draft the executive summary",
      "source": "hchat",
      "timestamp": "2026-04-05T09:00:00Z"
    },
    {
      "role": "assistant",
      "content": "Here is a draft executive summary...",
      "source": "",
      "timestamp": "2026-04-05T09:01:30Z"
    }
  ],
  "offset": 4096
}
```

---

#### `chat_poll`

Poll for new messages from an agent transcript since a byte offset. Used for incremental updates after sending a message.

**Maps to:** `GET /api/transcript/:agentId/poll?offset=...` (Workbench API)

**Status:** Built

**Inputs:**
```json
{
  "agent_id": "string (required)",
  "offset": "integer (required) — byte offset from previous response"
}
```

**Output:**
```json
{
  "messages": [...],
  "offset": 5120
}
```

---

### 5.7 `minato://docs/*`

#### `docs_read`

Read a named Minato system reference document. Agents use this to re-read architectural contracts, prompt templates, and system rules without having filesystem access to the docs directory.

**Maps to:** filesystem read of `/home/lily/projects/hashi2/docs/<doc_name>.md`

**Status:** Built

**Inputs:**
```json
{
  "doc": "string (required) — one of: MINATO_MCP_SERVER_PLAN | KASUMI_MCP_SERVER_PLAN | NAGARE_FLOW_SYSTEM | README"
}
```

**Output:**
```json
{
  "doc": "NAGARE_FLOW_SYSTEM",
  "content": "string — full Markdown text of the document",
  "path": "/home/lily/projects/hashi2/docs/NAGARE_FLOW_SYSTEM.md",
  "size_bytes": 18432
}
```

---

#### `docs_list`

List all available Minato system reference documents.

**Status:** Built

**Inputs:** none

**Output:**
```json
{
  "docs": [
    { "name": "MINATO_MCP_SERVER_PLAN", "path": "docs/MINATO_MCP_SERVER_PLAN.md" },
    { "name": "KASUMI_MCP_SERVER_PLAN", "path": "docs/KASUMI_MCP_SERVER_PLAN.md" },
    { "name": "NAGARE_FLOW_SYSTEM", "path": "docs/NAGARE_FLOW_SYSTEM.md" }
  ]
}
```

---

## 6. Resource Catalog

Resources are read-only views that agents can inspect without triggering mutations or audit entries. They are addressable by URI.

| Resource URI | Description | Status |
|---|---|---|
| `minato://project/list` | All known projects with slugs | Built |
| `minato://project/{slug}/state` | Current phase/workflow/scope state for a project | Built |
| `minato://shimanto/{slug}/phases` | All phases seen and current phases for a project | Built |
| `minato://nagare/workflows` | List of all known workflow YAML files | Built |
| `minato://nagare/workflow/{workflow_id}` | Full DAG definition of a named workflow | Built |
| `minato://nagare/run/{run_id}/state` | Execution state of a workflow run | Built |
| `minato://log/{slug}/today` | Today's JSONL log entries for a project | Built |
| `minato://log/{slug}/markdown/today` | Today's Markdown activity log | Built |
| `minato://artefacts/{slug}` | Artefact index for a project | Built |
| `minato://chat/{agent_id}/recent` | Recent transcript messages for an agent | Built |
| `minato://docs/list` | Available system reference documents | Built |
| `minato://docs/{doc_name}` | Full content of a named system doc | Built |
| `minato://prompts/list` | Prompt catalog summary for Minato operators and agents | Built |
| `minato://prompt/{prompt_name}` | Full prompt definition with template and operator guide | Built |

---

## 7. Implementation Plan

### Rollout status today

The Minato MCP server has now been implemented through Tier 8:

- Tier 1: JSON-RPC skeleton, audit envelope, tool registry
- Tier 2: project session context, Shimanto tools, Nagare read tools, docs/resources/chat wrappers
- Tier 3: artefact index, artefact CRUD surface, human Nagare step intervention
- Tier 4: expanded resources, prompt catalog, KASUMI artefact read delegation
- Tier 5: automatic project action logging for mutating MCP actions
- Tier 6: delegated KASUMI tool execution through registered artefacts
- Tier 7: documentation and README surface brought back into sync with the shipped server
- Tier 8: prompt read/render endpoints, prompt resources, and stronger operator handoff guides

The following REST endpoints still matter because Minato wraps or extends them:

| Capability | REST endpoint | MCP tool |
|---|---|---|
| List projects | `GET /api/project-log/list` | `project_list` |
| Query project log | `GET /api/project-log?project=...` | `log_query` |
| Append log entry | `POST /api/project-log/entry` | `log_append` |
| Get agent chat history | `GET /api/transcript/:agentId` | `chat_get_history` |
| Poll agent transcript | `GET /api/transcript/:agentId/poll` | `chat_poll` |
| Send hchat | `POST /api/chat` | `chat_send` |
| Per-agent project chat log | `GET /api/project-chat/{name}/{project}` | `log_project_chat` |

Supporting infrastructure that exists:

- `project_log.js` — JSONL + Markdown log writer with MINATO CONTEXT parsing
- `project_chat_logger.py` — per-agent workspace chat log
- `parseMinatoContext()` in both JS and Python — context header extraction
- Agent transcript read/poll with project log capture on inbound replies

### Next tiers from here

The foundation server is in place. The next useful tiers are now about depth, not existence:

- Tier 9: more complete KASUMI round-trip syncing so delegated mutations can update artefact metadata and richer project log payloads
- Tier 10: optional UI/operator integration in the workbench so active project and phase state can be driven visually rather than only through MCP and raw context headers

---

## 8. Integration with KASUMI MCP

Minato MCP and KASUMI MCP are complementary servers that operate at different layers:

| Concern | Minato MCP | KASUMI MCP |
|---|---|---|
| Project context | yes | no |
| Phase and workflow state | yes | no |
| Activity log | yes | no |
| Agent chat | yes | no |
| Spreadsheet read/write | no | yes (Nexcel) |
| Document read/write | no | yes (Wordo) |
| Semantic analysis | no | yes |
| Artefact contents (KASUMI objects) | delegates | yes |

### Cross-server artefact access

When an agent calls `artefacts_read` on a KASUMI artefact, the Minato MCP server:

1. looks up the artefact record to get `kasumi_id` and `kasumi_module`
2. delegates the read call to the KASUMI MCP server at `kasumi://nexcel/workbook/{id}` or `kasumi://wordo/document/{id}`
3. returns the result to the calling agent transparently

The agent does not need to know which server handled the read.

### Context enrichment in KASUMI operations

When an agent is working within a Minato project context and calls a KASUMI tool that mutates an artefact:

- Minato MCP should append a log entry of type `action` recording the KASUMI tool call
- This keeps the project activity log complete even for operations handled by the KASUMI layer

### Namespacing rule

To avoid confusion, tools are always prefixed by system:

- `project_*`, `shimanto_*`, `nagare_*`, `log_*`, `chat_*`, `artefacts_*`, `docs_*` — Minato
- `nexcel_*`, `wordo_*`, `system_*` — KASUMI

---

## 9. Agent Usage Guide

This section explains how an agent should work with Minato MCP in practice.

### Starting a project session

At the beginning of a task, call `project_list` to discover available projects, then `project_switch` to set the active context:

```json
{
  "method": "tools/call",
  "params": {
    "name": "project_list",
    "arguments": {}
  }
}
```

```json
{
  "method": "tools/call",
  "params": {
    "name": "project_switch",
    "arguments": { "project": "Audit — Shire Council" }
  }
}
```

### Checking project state

Before starting work, read the current phase and workflow context:

```json
{
  "method": "tools/call",
  "params": {
    "name": "project_get_state",
    "arguments": { "project": "Audit — Shire Council" }
  }
}
```

### Logging decisions and actions

After completing a meaningful unit of work, write a log entry. Use the correct `type`:

```json
{
  "method": "tools/call",
  "params": {
    "name": "log_append",
    "arguments": {
      "type": "decision",
      "project": "Audit — Shire Council",
      "shimanto_phases": ["fieldwork"],
      "nagare_workflows": ["audit-report-v1"],
      "summary": "Decided to scope report to Q3 only — Q4 data not yet available",
      "details": "Confirmed with Barry at 09:30. Q4 fieldwork deferred to next cycle."
    }
  }
}
```

### Sending a message to an agent

```json
{
  "method": "tools/call",
  "params": {
    "name": "chat_send",
    "arguments": {
      "agent_id": "akane",
      "text": "Please review the executive summary draft in artefact art_001",
      "inject_context": true
    }
  }
}
```

Then poll for the response:

```json
{
  "method": "tools/call",
  "params": {
    "name": "chat_poll",
    "arguments": {
      "agent_id": "akane",
      "offset": 4096
    }
  }
}
```

### Reading a Nagare workflow

```json
{
  "method": "tools/call",
  "params": {
    "name": "nagare_get_workflow_dag",
    "arguments": { "workflow_id": "audit-report-v1" }
  }
}
```

### Reading a system reference doc

When you need to re-read the Nagare schema or Minato architecture:

```json
{
  "method": "tools/call",
  "params": {
    "name": "docs_read",
    "arguments": { "doc": "NAGARE_FLOW_SYSTEM" }
  }
}
```

### General rules for agents

1. Always set an active project context at the start of a session. Do not assume a default.
2. Always log decisions and milestones. The log is the project's persistent memory across sessions and agents.
3. Use `log_query` to catch up on recent activity before starting work, especially after a gap.
4. When working with KASUMI artefacts, use `artefacts_read` through Minato — do not bypass the artefact index by calling KASUMI directly unless the artefact is not registered.
5. Use `docs_read` to refresh your understanding of system contracts rather than relying on memory.
6. When a Nagare workflow is in progress, check `nagare_get_run_status` before appending duplicate work.

---

## 10. Data Storage Reference

### Workbench log store (Node.js owned)

Location: `workbench/data/projects/<slug>/`

```text
<slug>/
  conversations/
    YYYY-MM-DD.jsonl     — machine-readable JSONL log entries
  log/
    YYYY-MM-DD.md        — human-readable Markdown activity log
```

JSONL entry shape:
```json
{
  "ts": "ISO timestamp",
  "session_id": "sess_<id>",
  "direction": "outbound | inbound",
  "agent": "agent id",
  "user": "user",
  "text": "message text (MINATO CONTEXT stripped)",
  "project": "display name",
  "shimanto_phases": ["string"],
  "nagare_workflows": ["string"],
  "scope": "string"
}
```

### Per-agent workspace log (Python owned)

Location: `workspaces/<agent>/projects/<slug>/chat_log.jsonl`

Entry shape:
```json
{
  "ts": "ISO timestamp",
  "source": "hchat",
  "project": "display name",
  "shimanto_phases": ["string"],
  "nagare_workflows": ["string"],
  "scope": "string",
  "user": "full user message text",
  "assistant": "full assistant response text"
}
```

### Nagare run state

Location: `flow/runs/<run_id>/state.json`

Shape (per workflow YAML schema):
- `run_id`
- `workflow_id`
- `started_at`
- `status` — pending | running | completed | failed
- per-step status entries

---

## 11. Error Model

All tool call errors return a JSON-RPC 2.0 error envelope:

```json
{
  "jsonrpc": "2.0",
  "id": "req-001",
  "error": {
    "code": -32010,
    "message": "NOT_FOUND",
    "data": {
      "field": "project",
      "reason": "No project found with name or slug 'xyz'"
    }
  }
}
```

Error categories:

| Code | Name | When to use |
|---|---|---|
| -32600 | `INVALID_REQUEST` | Malformed JSON-RPC envelope |
| -32601 | `METHOD_NOT_FOUND` | Tool name does not exist |
| -32602 | `INVALID_PARAMS` | Missing required field or wrong type |
| -32010 | `NOT_FOUND` | Project, agent, workflow, or artefact does not exist |
| -32011 | `PERMISSION_DENIED` | Caller not authorised for this operation |
| -32012 | `UPSTREAM_ERROR` | Downstream REST call to Workbench or Bridge API failed |
| -32013 | `CONFLICT` | State conflict — e.g. step already completed |
| -32099 | `INTERNAL_ERROR` | Unexpected server error |

---

## 12. Open Questions

These items should be resolved before implementation hardens:

- Whether the MCP server runs inside the existing Node.js Workbench server (same process, new route prefix) or as a separate process
- Whether `project_switch` session state is stored per-connection or per-agent-id
- Whether artefact IDs are local (scoped to Minato store) or global (shared with KASUMI IDs)
- Whether the MINATO CONTEXT header injection in `chat_send` should happen at the MCP layer or remain a responsibility of the calling agent
- How the `docs_read` tool handles documents that exist only on Windows paths (`/mnt/c/...`) vs WSL paths
- Whether Nagare workflow YAML definitions live in the HASHI Windows repo or should be mirrored into the hashi2 Linux repo

---

## 13. Final Recommendation

Build the Minato MCP server as a JSON-RPC 2.0 route group added to the existing Node.js Workbench server (`workbench/server/index.js`). This minimises deployment complexity — Workbench is already running at `:3001` and already owns the project log store.

Start with the seven tools that wrap existing REST endpoints (Priority 1). This gives agents immediate, structured access to the most-used capabilities — project list, log query, log append, and chat — with correct input validation and audit records.

The most important non-functional rule for Minato MCP is:

**every agent action that affects a project should produce a log entry** — either automatically via middleware or explicitly via `log_append`. The project log is the only durable shared memory across all agents, sessions, and reboots. Any action not in the log is invisible to future agents and to the human operator.
