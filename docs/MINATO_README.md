# Minato — AI-Assisted Project Management

---

## Overview

Minato is the project management layer of the HASHI ecosystem. It provides a structured way to organise and track work across multiple projects, using a hierarchical model: projects contain phases (Shimanto), phases contain workflows (Nagare), and workflows produce artefacts.

The design philosophy is that AI agents are first-class participants in project work — not just tools. Every conversation between a human and an agent that happens in the context of a Minato project is logged, tagged with project metadata, and made available to future agents as persistent project context. The project log is the authoritative record of what happened, what was decided, and why.

Minato is not a traditional task-tracker. It does not enforce rigid process. Instead, it provides:

- A lightweight, auditable conversation and activity log
- A structured vocabulary (projects, phases, workflows) that both humans and agents understand
- A workbench UI for managing multiple agent conversations simultaneously
- An MCP server surface so agents can read and write project state programmatically

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        MINATO                               │
│                  (Project layer)                            │
│                                                             │
│   Project A          Project B          Project C           │
│       │                  │                  │               │
├───────▼──────────────────▼──────────────────▼───────────────┤
│                       SHIMANTO                              │
│                   (Phase / timeline)                        │
│                                                             │
│   Phase 1 → Phase 2 → Phase 3                               │
│       │         │         │                                 │
├───────▼─────────▼─────────▼─────────────────────────────────┤
│                        NAGARE                               │
│               (Workflow / DAG execution)                    │
│                                                             │
│   Step 1 → Step 2 → Step 3 (with parallel branches)        │
│       │         │         │                                 │
├───────▼─────────▼─────────▼─────────────────────────────────┤
│                      ARTEFACTS                              │
│                                                             │
│   Files on disk / Obsidian Vault  |  KASUMI objects         │
│   (Nexcel workbooks, Wordo docs)                            │
└─────────────────────────────────────────────────────────────┘
```

---

## Core Concepts

### Projects (Minato)

A project is the top-level unit of work. Each project has:

- A display name (e.g. "AIPM CLI", "HASHI v3 Release")
- A slug derived from the display name (e.g. `aipm_cli`, `hashi_v3_release`)
- A persistent activity log stored under `workbench/data/projects/{slug}/`
- A conversation log (JSONL) capturing every human-agent exchange in that project's context

Projects are independent of HASHI agent transcripts. The Minato project log is owned by the workbench server and survives agent restarts or backend changes.

**Project switching** in the Workbench UI is planned as a footer projects bar — a persistent bar at the bottom of the interface showing all known projects, allowing quick switching of the active project context. The active project name is injected into each outbound message as a `MINATO CONTEXT` header, so agents know which project the conversation belongs to.

**Status:** ✅ Core data model and log system working. 🚧 WIP — footer projects bar UI not yet built. The MINATO CONTEXT header is parsed and logged but the active-project selector in the UI is not implemented yet.

### Phases (Shimanto)

Shimanto (流れ — meaning "current" or "flow" in a river sense; the word is used in the sense of a timeline horizon) represents a phase within a project.

A project may have multiple named phases, each corresponding to a distinct period or milestone horizon. For example:

- "Discovery"
- "Alpha Build"
- "Beta Testing"
- "Public Launch"

Phase names are passed in the `MINATO CONTEXT` header alongside the active project. They appear as `shimanto_phases` in the JSONL log and in Markdown activity log entries. This allows filtering the project log by phase.

**Status:** ✅ Phase metadata is parsed and stored in the project log. 🚧 WIP — no UI for defining or managing phases explicitly. Currently phases are set manually in the context header.

### Workflows (Nagare)

Nagare (流れ) is the workflow execution layer. Workflows are Directed Acyclic Graphs (DAGs) of steps, each step executed by a dedicated AI agent. Nagare is designed to accomplish what no single prompt can: coordinated, multi-perspective, self-improving work at scale.

Key Nagare concepts:

- **YAML-defined workflows** — each workflow is a `.yaml` file declaring steps, dependencies, agent roles, and pre-flight questions
- **DAG execution** — the FlowRunner topologically sorts steps and executes independent ones in parallel
- **Pre-flight system** — all human decisions are collected once upfront; the workflow runs uninterrupted after that
- **Artifact-driven data flow** — each step writes outputs to named artefacts; downstream steps reference them via `{artifacts.key}`
- **Persistent state** — `state.json` is written atomically after every step; workflows survive crashes and can resume from the exact failed step
- **Multi-model** — any step can use any model (Claude, GPT, Gemini, Codex, DeepSeek) based on what the task requires
- **Self-improving** — an Evaluation Knowledge Base accumulates lessons from every run

Within Minato, active Nagare workflow names are tracked as `nagare_workflows` in the project log, so the activity log shows which workflow a given conversation was associated with.

**Nagare-Viz** — a visual DAG editor for workflows — is planned as a component in the Workbench. It would allow visual construction and inspection of workflow graphs.

**Status:** ✅ Nagare workflow engine (FlowRunner, WorkerDispatcher, PreFlightCollector, ArtifactStore) is fully operational. ✅ YAML workflow definition and execution. 🚧 WIP — Nagare-Viz visual editor not yet built. 🔧 Partial — Minato-Nagare integration in the UI (displaying active workflows per project) exists at the data level but the UI panel is not implemented.

### Artefacts

Artefacts are the outputs of project work. Two categories:

**1. Physical artefacts**

Files that live on the filesystem or in an Obsidian Vault:

- Source code, documents, reports, data files
- Nagare workflow outputs (each step writes to the ArtifactStore under the workflow run directory)
- Markdown logs (daily `.md` files in `workbench/data/projects/{slug}/log/`)

**2. Temporary / AI-native artefacts (KASUMI objects)**

KASUMI is the AI-native application layer. Its artefact types are designed to be more machine-friendly than standard office files:

- **Nexcel** — a spreadsheet-like object. More structured and AI-queryable than Excel. Supports semantic maps, named ranges, and structured read/write via the KASUMI MCP server.
- **Wordo** — a document-like object. Block-based structure, outline-aware, semantically parsed on demand.

These objects are "temporary" in the sense that they are designed to be edited by both humans and AI agents in real time and do not require export to be useful — they are the working medium, not an afterthought.

**Status:** ✅ Physical artefacts (filesystem files, Nagare run outputs). ✅ Minato artefact registry and KASUMI artefact delegation are implemented in the Minato MCP server. 🔧 Partial — full KASUMI module breadth still depends on the external KASUMI MCP service and its available tools.

### Agent Integration

AI agents are first-class participants in Minato projects. Two modes of agent participation:

**1. Direct LLM chat**

Any LLM-backed HASHI agent can participate in project conversations through the Workbench. The active project context is injected automatically into each outbound message.

**2. HASHI agents via hchat protocol**

The hchat (HASHI chat) protocol allows cross-instance agent communication. A message sent from one HASHI instance to an agent running in another instance carries the full MINATO CONTEXT header. The receiving agent's runtime (`flexible_agent_runtime.py`) parses this header and logs the exchange to the project's chat log via `ProjectChatLogger`.

Both the workbench server (Node.js, `project_log.js`) and the Python agent runtime (`project_chat_logger.py`) implement the same MINATO CONTEXT parsing logic independently, so logging works regardless of the message path.

**MINATO CONTEXT header format:**

```
[MINATO CONTEXT]
minato active project: My Project Name
shimanto phases: Phase 1, Phase 2
nagare workflows: workflow-name-1, workflow-name-2
scope: brief description of task scope
[END CONTEXT]
```

This header is stripped before the message text is stored or displayed, but the metadata is extracted and tagged on every log entry.

---

## Workbench

The HASHI Workbench is a browser-based UI for managing agent conversations and project activity. It runs as a Node.js Express server (`workbench/server/index.js`) with a React frontend (`workbench/src/App.jsx`).

**What it provides:**

- Multi-panel agent chat interface — display up to 9 agent panels simultaneously, each with a live-updating transcript
- Drag-and-drop panel reordering (via @dnd-kit)
- Two layout modes: Workbench (grid of panels) and Chat Mode (single fullscreen agent, Telegram-style)
- Theme support: Dark, Bright, CLI Retro
- Language support: English, Japanese, Simplified Chinese, Traditional Chinese, Korean, German, French, Russian, Arabic
- System resource display: CPU usage, RAM, GPU/NPU detection
- Bridge connectivity status (online/offline indicator for the HASHI bridge)
- Agent metadata editing (display name, emoji) — persisted to `agents.json`
- Backend/model/effort switching per agent (for flex-type agents)
- File and image attachment support in chat
- Project log loading on agent panel initialisation — recent project conversation entries are merged with the agent transcript to provide project context

**Project log integration in the UI:**

When a panel is initialised, the workbench loads entries from all known projects via `/api/project-log/list` and `/api/project-log`, merges them with the agent's JSONL transcript (deduplicating by content+role), and presents a unified conversation history. This means a new conversation starts with the context of prior project work visible in the chat panel.

**Status:** ✅ Core workbench UI is fully working. ✅ Project log loading on panel init. 🚧 WIP — footer projects bar (active project switcher) not yet built. 🚧 WIP — Shimanto phase selector not yet built. 🚧 WIP — Nagare-Viz component not yet built.

---

## Project Activity Log

The project activity log is the persistent, auditable record of everything that happens in a project. It is stored locally by the workbench server, completely independent of HASHI backend agent transcripts.

**Two formats, written in parallel:**

**1. JSONL machine-readable log**

Path: `workbench/data/projects/{slug}/conversations/{YYYY-MM-DD}.jsonl`

Each line is a JSON object:

```json
{
  "ts": "2026-04-05T09:23:11.000Z",
  "session_id": "sess_abc123",
  "direction": "outbound",
  "agent": "kasumi",
  "user": "user",
  "text": "Can you draft the project brief?",
  "project": "AIPM CLI",
  "shimanto_phases": ["Alpha Build"],
  "nagare_workflows": ["draft-brief"],
  "scope": "initial draft only"
}
```

Fields:
- `ts` — ISO timestamp
- `session_id` — groups paired outbound/inbound messages into one logical exchange
- `direction` — `outbound` (user to agent) or `inbound` (agent reply)
- `agent` — agent identifier
- `user` — human identifier (default `"user"`)
- `text` — message content (MINATO CONTEXT header and hchat headers stripped)
- `project` — project display name
- `shimanto_phases` — array of active phase names
- `nagare_workflows` — array of active workflow names
- `scope` — optional scope string

**2. Markdown human-readable log**

Path: `workbench/data/projects/{slug}/log/{YYYY-MM-DD}.md`

Each file has YAML frontmatter and a series of dated sections. Entry types:

| Type | Icon | Use |
|------|------|-----|
| `chat` | 💬 | Agent conversation excerpts |
| `action` | ⚙️ | System or agent actions |
| `decision` | ✅ | Recorded decisions |
| `milestone` | 📌 | Project milestones |
| `note` | 📝 | General notes |

Example entry:

```markdown
## 09:23 · 💬 Chat · kasumi

**Shimanto:** Alpha Build
**Nagare:** draft-brief

**Conversation with kasumi**

> **user →** Can you draft the project brief?
> **kasumi →** Sure. Here is a first draft...

---
```

The Markdown log is designed to be readable by humans in any text editor and importable into Obsidian.

**API endpoints (workbench server):**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/project-log/list` | List all known projects |
| `GET` | `/api/project-log?project=Name&limit=200&since=YYYY-MM-DD` | Read project log entries |
| `POST` | `/api/project-log/entry` | Write a structured activity entry |

**API endpoint (Python backend):**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/project-chat/{agent_name}/{project}` | Read per-agent project chat log |

The Python-side logger (`project_chat_logger.py`) writes to:
`workspaces/{agent}/projects/{slug}/chat_log.jsonl`

This is a separate file per agent per project, written by `flexible_agent_runtime.py` when processing messages that carry a MINATO CONTEXT header via hchat.

**Status:** ✅ Both JSONL and Markdown logs are fully implemented and writing correctly. ✅ Python-side `ProjectChatLogger` is integrated into `flexible_agent_runtime.py`. ✅ Read/write API endpoints are live.

---

## Agent Chat

Agents participate in Minato projects through the Workbench chat interface. Each conversation panel corresponds to one HASHI agent.

**Direct chat flow:**

1. User types a message in a Workbench panel
2. The workbench server intercepts the outbound message, parses the MINATO CONTEXT header if present
3. A session ID is generated; the outbound message is written to the JSONL log
4. The message is forwarded to the HASHI bridge (`bridge-u-f` API) and on to the agent
5. The workbench server polls the agent's transcript for new messages
6. When an inbound assistant reply arrives, it is written to the JSONL log (as `direction: inbound`) and a Markdown chat entry is appended to the daily log file
7. The reply is rendered in the Workbench panel

**hchat cross-instance flow:**

When an agent on one HASHI instance sends a message to an agent on another instance using the hchat protocol, the MINATO CONTEXT header travels with the message. The receiving agent's Python runtime parses the header and calls `ProjectChatLogger.log_exchange()`, writing the exchange to the per-agent project log.

**Context injection:**

The active project context (project name, phases, workflows, scope) is injected as a `MINATO CONTEXT` header prepended to each outbound message. This is handled by the Workbench frontend. Agents receive the full context and can use it to tailor their responses appropriately.

**Status:** ✅ Direct chat with context logging is fully working. ✅ hchat cross-instance logging via `ProjectChatLogger` is working. 🚧 WIP — the UI for setting active project, phases, and scope on a per-message basis is not yet built (context is currently set manually).

---

## MCP Server

The Minato MCP (Model Context Protocol) server is now live as the programmable project-management surface for HASHI. It lets agents and operators work with project context, Shimanto phases, Nagare workflows, artefacts, logs, chats, docs, resources, and prompt templates through one JSON-RPC server.

**Implemented location:**

- `workbench/server/minato_mcp.js`
- mounted from `workbench/server/index.js`
- base path: `/api/minato/mcp/v1`

**What is live now:**

- Project tools: `project_list`, `project_get_state`, `project_switch`
- Shimanto tools: `shimanto_get_current_phase`, `shimanto_list_phases`, `shimanto_transition_phase`
- Nagare tools: `nagare_list_workflows`, `nagare_get_workflow_dag`, `nagare_get_run_status`, `nagare_update_step_status`
- Artefact tools: `artefacts_list`, `artefacts_create`, `artefacts_read`, `artefacts_link`, `artefacts_kasumi_call`
- Log/chat/doc tools: `log_query`, `log_append`, `log_project_chat`, `chat_send`, `chat_get_history`, `chat_poll`, `docs_list`, `docs_read`
- Read-only surfaces: `resources/list`, `resources/read`, `prompts/list`, `prompts/read`, `prompts/render`

**Tier rollout completed:**

1. Tier 1: JSON-RPC skeleton, validation, audit, registry
2. Tier 2: project context memory, Shimanto tools, Nagare read tools, docs/resources/chat wrappers
3. Tier 3: artefact index, artefact CRUD, Nagare manual status write
4. Tier 4: richer resources, prompt catalog, KASUMI artefact read delegation
5. Tier 5: automatic project action logging for mutating MCP calls
6. Tier 6: delegated KASUMI tool execution via registered artefacts
7. Tier 7: documentation and README sync for the shipped server surface
8. Tier 8: prompt read/render endpoints, prompt resources, and stronger operator handoff guides

**Current boundary:**

- The Minato MCP server is implemented and tested.
- KASUMI delegation paths are implemented on the Minato side.
- Full KASUMI capability still depends on the external KASUMI MCP service being reachable through `KASUMI_MCP_API`.

For the detailed architecture and status ledger, see [`MINATO_MCP_SERVER_PLAN.md`](./MINATO_MCP_SERVER_PLAN.md). For the KASUMI-side architecture, see [`KASUMI_MCP_SERVER_PLAN.md`](./KASUMI_MCP_SERVER_PLAN.md).

---

## CLI

The HASHI CLI (`cli.js`, `main.py`) is the command-line entry point for the entire HASHI system. It launches the Python orchestrator, which manages agents, bridges, skills, and the Nagare workflow engine.

Project management via CLI currently means:

- Starting and stopping HASHI with specific configurations
- Running Nagare workflows directly from the command line via `flow/flow_trigger.py`
- Querying workflow status and run history

Minato-specific CLI commands (project creation, phase management, log querying from the terminal) are planned but not yet implemented as a dedicated subcommand set.

**Status:** ✅ Core HASHI CLI is working. ✅ Nagare workflow CLI (`flow_trigger.py`) is working. 🚧 WIP — Minato-specific project management CLI subcommands not yet built.

---

## File Structure

```
hashi2/                                  (WSL dev repo — release/v3.0-alpha branch)
├── workbench/
│   ├── server/
│   │   ├── index.js                     ✅ Express API server — chat, transcript, project log
│   │   ├── minato_mcp.js                ✅ Minato MCP server surface and tool/resource registry
│   │   ├── project_log.js               ✅ Project log read/write (JSONL + Markdown)
│   │   └── agents.js                    ✅ Agent config loader from agents.json
│   ├── src/
│   │   ├── App.jsx                      ✅ React frontend (multi-panel workbench)
│   │   └── styles.css                   ✅ Theming and layout
│   └── data/                            (created at runtime)
│       └── projects/
│           └── {slug}/
│               ├── conversations/
│               │   └── {YYYY-MM-DD}.jsonl    ✅ Machine-readable per-project log
│               └── log/
│                   └── {YYYY-MM-DD}.md       ✅ Human-readable Markdown activity log
│
├── orchestrator/
│   ├── workbench_api.py                 ✅ Python Workbench API server (bridge-u-f backend)
│   ├── project_chat_logger.py           ✅ Per-agent project chat logger (Python side)
│   ├── flexible_agent_runtime.py        ✅ Agent runtime (integrates ProjectChatLogger)
│   ├── agent_runtime.py                 ✅ Legacy agent runtime
│   └── skill_manager.py                 ✅ Skill loading and dispatch
│
├── flow/                                (Nagare workflow engine — in Windows HASHI)
│   ├── engine/
│   │   └── flow_runner.py               ✅ FlowRunner — DAG execution engine
│   └── flow_trigger.py                  ✅ Non-blocking workflow launcher
│
├── docs/
│   ├── MINATO_README.md                 (this file)
│   ├── KASUMI_MCP_SERVER_PLAN.md        🚧 Architecture plan (not yet implemented)
│   ├── NAGARE_FLOW_SYSTEM.md            ✅ Nagare complete technical reference
│   └── README.md                        ✅ Main HASHI README
│
└── agents.json                          ✅ Agent registry (shared by both runtimes)
```

**Windows HASHI** (`C:\Users\thene\projects\HASHI\`) is the production/Windows copy of the HASHI system. It shares the same agent runtime architecture and also contains `project_chat_logger.py` and the hchat project logging integration.

---

## Integration with Obsidian

The Markdown activity log files (`workbench/data/projects/{slug}/log/{YYYY-MM-DD}.md`) are designed to be compatible with Obsidian:

- Each file has YAML frontmatter with `project`, `date`, `agents`, `participants`, and `tags` fields
- Tags follow the Obsidian convention: `#project/{slug}`
- Entries use standard Markdown heading levels and blockquote formatting
- Files are named by date, matching Obsidian's daily note naming convention

To use in Obsidian: add `workbench/data/projects/` as a folder inside your Obsidian Vault, or symlink it. The daily Markdown files will appear as dated notes under each project subfolder.

Nagare workflow artefacts (output files from workflow runs) can similarly be placed in or symlinked into an Obsidian Vault, allowing the vault to serve as a unified repository of both conversation history and workflow outputs.

**Status:** 🔧 Partial — the Markdown log format is Obsidian-compatible by design, but there is no automatic sync or vault integration built. Manual symlink or folder inclusion is the current approach.

---

## WIP / Roadmap

The following features are planned or partially implemented. Items marked 🚧 do not yet have working code.

| Feature | Status | Notes |
|---------|--------|-------|
| Footer projects bar (active project switcher in UI) | 🚧 WIP | UI component not built; data model exists |
| Shimanto phase selector in UI | 🚧 WIP | Phases are parsed/logged but no UI to set them |
| Nagare-Viz visual workflow editor | 🚧 WIP | No implementation; planned as Workbench component |
| Minato MCP Server | ✅ | Implemented through Tier 8 with tools, resources, prompt read/render, docs, artefacts, and KASUMI delegation hooks |
| KASUMI MCP Server | 🚧 WIP | Architecture plan exists; external service still owns the downstream Nexcel/Wordo tool surface |
| KASUMI Nexcel artefact type | 🚧 WIP | Planned; no implementation |
| KASUMI Wordo artefact type | 🚧 WIP | Planned; no implementation |
| Minato CLI subcommands | 🚧 WIP | Project/phase management from terminal not built |
| Obsidian Vault auto-sync | 🚧 WIP | Markdown is compatible; no sync mechanism |
| Active project context injection in UI | 🚧 WIP | MCP-layer session context and chat injection work; visual UI selector is still not built |
| Nagare workflow status display in Workbench | 🚧 WIP | No UI panel for this |
| Project creation/deletion via UI | 🚧 WIP | Projects are created implicitly by first log entry |

**What is fully working today:**

| Feature | Status |
|---------|--------|
| Multi-agent Workbench UI | ✅ |
| Project activity log (JSONL + Markdown) | ✅ |
| MINATO CONTEXT header parsing | ✅ |
| Agent conversation logging per project | ✅ |
| Python-side ProjectChatLogger (hchat path) | ✅ |
| Project log read/write API endpoints | ✅ |
| Minato MCP server | ✅ |
| Nagare FlowRunner DAG execution engine | ✅ |
| Nagare YAML workflow definition | ✅ |
| Nagare pre-flight system | ✅ |
| Nagare artefact store and state persistence | ✅ |
| HASHI bridge and agent runtime | ✅ |
| hchat cross-instance agent communication | ✅ |

---

*Last updated: 2026-04-05*
