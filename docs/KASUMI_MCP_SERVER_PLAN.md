# KASUMI MCP Server Upgrade Plan

## 1. Document Status

- Status: Draft architecture plan
- Scope: KASUMI application-layer MCP design for Nexcel, Wordo, and future modules
- Intended audience: system architect, API implementers, agent/runtime developers
- Last updated: 2026-03-29

---

## 2. Purpose

This document defines a complete upgrade plan for turning KASUMI application capabilities into a unified MCP-style server surface so AI agents can interact with KASUMI artifacts as first-class working objects.

The goal is not only to let AI read artifacts, but to let AI operate them with the same practical power as a human user:

- read workbook and document state
- write content
- format content
- create links and references
- create or delete tabs/files
- import and export files
- ask AI-native semantic questions over the artifact

This plan is written so the MCP layer can grow with the KASUMI product without needing a redesign every time a new module appears.

---

## 3. Executive Summary

### Decision 1: one server, multiple namespaces

KASUMI should expose a single logical MCP server, not separate MCP servers for Nexcel and Wordo.

Reason:

- real user workflows span modules
- shared auth, audit, caching, and transport should exist once
- cross-module operations should happen inside the server, not be pushed onto the agent
- future modules can register into the same surface

The external shape should therefore be:

- `kasumi://nexcel/...`
- `kasumi://wordo/...`
- `kasumi://system/...`
- future: `kasumi://calendar/...`, `kasumi://canvas/...`, `kasumi://mail/...`

### Decision 2: semantic parsing is on-demand and cache-first

Artifact semantic parsing should not run during ordinary human editing.

It should run only when an AI task needs semantic context:

1. compute content hash
2. check semantic cache
3. on hit, return cached semantic result
4. on miss, call configured LLM API
5. store result and return it

### Decision 3: extensibility comes from registry, not hardcoding

Tools and resources must be registered by module. New modules should add MCP support by contributing definitions to a central registry, without changing the core server contract.

---

## 4. Why A Unified KASUMI MCP Server

### 4.1 Separate MCP servers would create unnecessary friction

If Nexcel and Wordo each expose separate servers:

- the agent must manage multiple tool catalogs
- cross-module actions become multi-step orchestration outside the platform
- auth, permissions, event broadcast, and audit logic are duplicated
- module growth becomes harder to govern consistently

### 4.2 A single server matches product reality

KASUMI is one user-facing system. The MCP layer should reflect that by exposing one coherent tool plane with clean internal separation by module.

### 4.3 Internal module isolation still matters

A unified server does not mean a monolithic implementation. Internally:

- each module owns its own handlers
- each module owns its own schemas
- each module can version its tools
- only the registry and common middleware are shared

---

## 5. Architecture Overview

```text
AI Agent / HASHI Runtime
        |
        |  JSON-RPC / MCP-style requests
        v
+---------------------------------------------+
|           KASUMI MCP Server                 |
|---------------------------------------------|
| Common Middleware                           |
| - auth                                      |
| - permissions                               |
| - validation                                |
| - audit log                                 |
| - rate limit                                |
| - cache lookup                              |
| - websocket event emission                  |
|---------------------------------------------|
| Registry                                    |
| - resources                                 |
| - tools                                     |
| - prompt templates                          |
|---------------------------------------------|
| Modules                                     |
| - Nexcel                                    |
| - Wordo                                     |
| - future modules                            |
+---------------------------------------------+
        |
        +--> storage layer / artifact store
        +--> semantic cache
        +--> LLM API client
        +--> websocket broadcast
```

---

## 6. Core MCP Model For KASUMI

KASUMI should expose three categories consistent with MCP concepts:

### 6.1 Resources

Read-only or query-only object views that AI can inspect.

Examples:

- workbook list
- sheet raw grid
- semantic map
- document outline
- named ranges
- styles

### 6.2 Tools

Mutating or procedural operations that AI can call.

Examples:

- write cell
- format range
- insert section
- create sheet
- export workbook
- analyze sheet

### 6.3 Prompts

Reusable task scaffolds with domain assumptions built in.

Examples:

- analyze audit workpaper
- normalize spreadsheet formatting
- summarize document structure
- convert extracted table to structured JSON

---

## 7. API Style And Transport

### 7.1 Protocol shape

The server should expose a JSON-RPC 2.0 compatible HTTP surface, with optional SSE or WebSocket support for streaming and events.

Recommended base:

- `/api/kasumi/mcp/v1`

Recommended endpoints:

- `POST /api/kasumi/mcp/v1/resources/read`
- `POST /api/kasumi/mcp/v1/tools/call`
- `GET /api/kasumi/mcp/v1/tools/list`
- `GET /api/kasumi/mcp/v1/resources/list`
- `GET /api/kasumi/mcp/v1/prompts/list`

### 7.2 Why JSON-RPC 2.0

- simple request-response contract
- clean error envelope
- tool invocation maps naturally to method calls
- easy to expose over HTTP, bridge, and internal adapters

### 7.3 Example tool call

```json
{
  "jsonrpc": "2.0",
  "id": "req-001",
  "method": "tools/call",
  "params": {
    "name": "nexcel_write_range",
    "arguments": {
      "sheetId": "sheet_123",
      "range": "A1:C3",
      "data": [
        ["Day", "Task", "Owner"],
        ["1", "Planning", "Barry"],
        ["2", "Testing", "Aqiao"]
      ]
    }
  }
}
```

---

## 8. Semantic Parsing Strategy

### 8.1 Purpose

Raw cells are often insufficient for high-quality AI reasoning. The server should provide semantic interpretation so agents can understand structure rather than only coordinates.

### 8.2 Trigger rule

Semantic parsing should run:

- when an AI task explicitly requests analysis
- when object context middleware needs semantic context
- when a tool depends on cluster-level understanding

It should not run continuously during human editing.

### 8.3 Cache-first flow

```text
Read cell grid
   |
Compute cell hash
   |
Cache hit? ---- yes ---> return SemanticMap
   |
   no
   |
Call configured LLM API
   |
Validate output
   |
Store semantic cache
   |
Return SemanticMap
```

### 8.4 LLM provider rule

The semantic parser must call a configurable LLM API, not a hardcoded single model vendor.

Supported target styles:

- HASHI API
- any OpenAI-compatible endpoint
- future vendor adapters if needed

Configuration should be environment-driven:

- `KASUMI_LLM_BASE_URL`
- `KASUMI_LLM_API_KEY`
- `KASUMI_LLM_MODEL`
- `KASUMI_LLM_TIMEOUT_MS`

### 8.5 Parser output contract

```json
{
  "docType": "audit_workpaper",
  "clusters": [
    {
      "id": "c1",
      "type": "document_header",
      "range": "A1:D3",
      "label": "Hashi Shire Council - Audit Plan header",
      "confidence": 0.97
    },
    {
      "id": "c2",
      "type": "table",
      "range": "A9:C14",
      "label": "Audit plan schedule",
      "inferredHeaders": ["Day", "Task", "Responsible Person"]
    }
  ],
  "relations": [
    {
      "sourceClusterId": "c1",
      "targetClusterId": "c2",
      "type": "introduces"
    }
  ],
  "summary": "Audit planning sheet containing title block and a schedule table.",
  "cellHash": "sha256:abc123",
  "parsedAt": "2026-03-29T06:39:00Z"
}
```

### 8.6 SemanticMap requirements

At minimum, the parser should produce:

- `docType`
- `clusters`
- `relations`
- `summary`
- `cellHash`
- `parsedAt`

Optional future fields:

- `warnings`
- `assumptions`
- `entities`
- `formulaIntent`
- `layoutHints`

---

## 9. Nexcel MCP Surface

Nexcel is a spreadsheet-like artifact domain. The MCP layer should cover read, write, formatting, links, structure, file operations, and AI-native helpers.

### 9.1 Nexcel resources

- `kasumi://nexcel/workbook/list`
- `kasumi://nexcel/workbook/{workbookId}/tabs`
- `kasumi://nexcel/sheet/{sheetId}/raw`
- `kasumi://nexcel/sheet/{sheetId}/semantic-map`
- `kasumi://nexcel/sheet/{sheetId}/range/{range}`
- `kasumi://nexcel/sheet/{sheetId}/named-ranges`
- `kasumi://nexcel/sheet/{sheetId}/formats`

### 9.2 Nexcel read tools

- `nexcel_read_cell(sheetId, ref)`
- `nexcel_read_range(sheetId, range)`
- `nexcel_read_semantic_map(sheetId)`
- `nexcel_find_cells(sheetId, query)`
- `nexcel_get_formula(sheetId, ref)`

### 9.3 Nexcel write tools

- `nexcel_write_cell(sheetId, ref, value)`
- `nexcel_write_range(sheetId, range, data)`
- `nexcel_write_formula(sheetId, ref, formula)`
- `nexcel_clear_range(sheetId, range)`
- `nexcel_delete_range(sheetId, range, shift)`

### 9.4 Nexcel format tools

- `nexcel_set_format(sheetId, range, format)`
- `nexcel_merge_cells(sheetId, range)`
- `nexcel_unmerge_cells(sheetId, range)`
- `nexcel_set_column_width(sheetId, col, width)`
- `nexcel_set_row_height(sheetId, row, height)`
- `nexcel_freeze_panes(sheetId, row, col)`

### 9.5 Nexcel link and reference tools

- `nexcel_create_named_range(sheetId, name, range)`
- `nexcel_delete_named_range(sheetId, name)`
- `nexcel_create_hyperlink(sheetId, ref, url, label)`
- `nexcel_create_cross_sheet_link(sheetId, ref, targetSheetId, targetRef)`

### 9.6 Nexcel structure tools

- `nexcel_insert_rows(sheetId, afterRow, count)`
- `nexcel_insert_cols(sheetId, afterCol, count)`
- `nexcel_delete_rows(sheetId, rows)`
- `nexcel_delete_cols(sheetId, cols)`
- `nexcel_sort_range(sheetId, range, byCol, direction)`

### 9.7 Nexcel file tools

- `nexcel_new_sheet(workbookId, name)`
- `nexcel_rename_sheet(sheetId, name)`
- `nexcel_delete_sheet(sheetId)`
- `nexcel_duplicate_sheet(sheetId, newName)`
- `nexcel_reorder_sheets(workbookId, order)`
- `nexcel_new_workbook(name)`
- `nexcel_import_csv(data, options)`
- `nexcel_import_xlsx(buffer)`
- `nexcel_export_csv(sheetId)`
- `nexcel_export_xlsx(workbookId)`
- `nexcel_export_pdf(sheetId, printArea)`

### 9.8 Nexcel AI-native tools

- `nexcel_analyse_sheet(sheetId)`
- `nexcel_query_cluster(sheetId, clusterId, question)`
- `nexcel_auto_format_table(sheetId, range)`
- `nexcel_extract_table(sheetId, clusterId)`
- `nexcel_fill_series(sheetId, range, pattern)`

---

## 10. Wordo MCP Surface

Wordo is a document-like artifact domain. It should be a first-class module in the same MCP server.

### 10.1 Wordo resources

- `kasumi://wordo/document/list`
- `kasumi://wordo/document/{documentId}/raw`
- `kasumi://wordo/document/{documentId}/outline`
- `kasumi://wordo/document/{documentId}/semantic-map`
- `kasumi://wordo/document/{documentId}/section/{sectionId}`
- `kasumi://wordo/document/{documentId}/styles`

### 10.2 Wordo read tools

- `wordo_read_document(documentId)`
- `wordo_read_section(documentId, sectionId)`
- `wordo_read_outline(documentId)`
- `wordo_find_text(documentId, query)`
- `wordo_extract_tables(documentId)`

### 10.3 Wordo write tools

- `wordo_write_block(documentId, blockId, content)`
- `wordo_insert_block(documentId, afterBlockId, content, type)`
- `wordo_delete_block(documentId, blockId)`
- `wordo_replace_range(documentId, start, end, content)`
- `wordo_insert_table(documentId, afterBlockId, data)`

### 10.4 Wordo format tools

- `wordo_format_block(documentId, blockId, style)`
- `wordo_apply_heading(documentId, blockId, level)`
- `wordo_set_page_layout(documentId, layout)`
- `wordo_insert_page_break(documentId, afterBlockId)`

### 10.5 Wordo link and structure tools

- `wordo_create_bookmark(documentId, name, blockId)`
- `wordo_create_link(documentId, blockId, target)`
- `wordo_move_section(documentId, sectionId, afterSectionId)`
- `wordo_merge_documents(documentIds, outputName)`

### 10.6 Wordo file tools

- `wordo_new_document(name)`
- `wordo_rename_document(documentId, name)`
- `wordo_delete_document(documentId)`
- `wordo_duplicate_document(documentId, newName)`
- `wordo_import_docx(buffer)`
- `wordo_import_markdown(text)`
- `wordo_export_docx(documentId)`
- `wordo_export_pdf(documentId)`
- `wordo_export_markdown(documentId)`

### 10.7 Wordo AI-native tools

- `wordo_analyse_document(documentId)`
- `wordo_query_section(documentId, sectionId, question)`
- `wordo_generate_outline(documentId)`
- `wordo_normalise_styles(documentId)`
- `wordo_extract_action_items(documentId)`

---

## 11. System-Level MCP Surface

Some capabilities should exist above any single module.

### 11.1 System resources

- `kasumi://system/modules`
- `kasumi://system/tools`
- `kasumi://system/resources`
- `kasumi://system/prompts`
- `kasumi://system/capabilities`

### 11.2 System tools

- `system_list_modules()`
- `system_list_tools(module?)`
- `system_list_resources(module?)`
- `system_get_capabilities()`
- `system_ping()`

These are important so agents can discover the current platform shape at runtime instead of relying on stale assumptions.

---

## 12. Registry-Based Extensibility

### 12.1 Principle

The MCP layer must be self-describing. Modules should register what they expose.

### 12.2 Registry model

```text
Module package
   |
   +--> resources[]
   +--> tools[]
   +--> prompts[]
   |
Registry.register(moduleName, definitions)
   |
Server auto-exposes all registered items
```

### 12.3 Example TypeScript shape

```ts
export interface McpToolDefinition {
  name: string
  module: string
  version: string
  description: string
  inputSchema: Record<string, unknown>
  outputSchema: Record<string, unknown>
  deprecated?: boolean
  replacedBy?: string
  handler: (args: unknown, ctx: McpContext) => Promise<unknown>
}

export interface McpResourceDefinition {
  uriPattern: string
  module: string
  version: string
  description: string
  read: (params: unknown, ctx: McpContext) => Promise<unknown>
}

export interface McpPromptDefinition {
  name: string
  module: string
  version: string
  description: string
  build: (args: unknown, ctx: McpContext) => Promise<string>
}
```

### 12.4 Example module registration

```ts
import { registry } from "../mcp/registry"
import { nexcelTools, nexcelResources, nexcelPrompts } from "./nexcel/mcp"

registry.registerModule("nexcel", {
  tools: nexcelTools,
  resources: nexcelResources,
  prompts: nexcelPrompts,
})
```

### 12.5 Upgrade benefit

When a future module appears, the main server does not need a custom rewrite. The module only registers definitions and handlers.

---

## 13. Versioning And Compatibility

### 13.1 API versioning

Expose server versions in the route path:

- `/api/kasumi/mcp/v1/...`
- future: `/api/kasumi/mcp/v2/...`

### 13.2 Tool versioning

Each tool and resource should also carry its own semantic version string.

### 13.3 Compatibility rule

Within one API major version:

- additive changes are allowed
- removals are not allowed
- behavior changes must be explicit and documented

### 13.4 Deprecation rule

Old tools should not be hard removed immediately.

Instead:

- mark as `deprecated: true`
- expose `replacedBy`
- keep it during the supported compatibility window

### 13.5 Why this matters

Agents may cache tool assumptions. Stable evolution avoids silent breakage.

---

## 14. Object Context Injection In HASHI

### 14.1 Purpose

When HASHI agents operate on KASUMI artifacts, the bridge should automatically enrich the prompt with relevant object context.

### 14.2 Rule

If an artifact is recognized as a KASUMI object:

- fetch semantic context when available
- fetch raw summary if needed
- inject concise object context into the system or tool prompt

### 14.3 Nexcel object hook

If `objectType === "nexcel"`:

1. request semantic map
2. attach top-level summary, clusters, and key ranges
3. allow later tool calls to operate against the same artifact id

### 14.4 Wordo object hook

If `objectType === "wordo"`:

1. request outline and semantic map
2. inject section hierarchy and document summary
3. preserve document id for follow-up tool calls

### 14.5 Why middleware is better than ad hoc prompt building

- consistent behavior across routes and agents
- easier observability
- easier cache reuse
- less duplicated prompt assembly logic

---

## 15. Caching Strategy

### 15.1 What should be cached

- semantic maps
- document outlines if expensive to compute
- range snapshots for large repeated reads if justified

### 15.2 Cache key design

Recommended cache key parts:

- module name
- artifact id
- content hash
- parser version
- model identifier

Example:

```text
nexcel:sheet_123:sha256_abcd:v1:gpt-5.4-mini
```

### 15.3 Invalidation

Any artifact mutation that changes effective content should invalidate semantic cache.

Examples:

- write cell
- clear range
- import file
- move document section
- delete block

### 15.4 Format-only invalidation

Formatting-only operations may or may not require semantic invalidation depending on whether semantic understanding depends on layout.

Recommended initial rule:

- formatting changes do invalidate semantic cache

This is conservative and simpler to reason about.

---

## 16. Eventing And Realtime Sync

When the MCP layer mutates artifacts, user-facing interfaces should update immediately.

### 16.1 Broadcast rule

After a successful write or file operation:

- persist change
- emit a websocket event
- return operation result

### 16.2 Example events

- `nexcel.cells.updated`
- `nexcel.sheet.created`
- `nexcel.sheet.deleted`
- `wordo.block.updated`
- `wordo.document.created`
- `wordo.document.deleted`

### 16.3 Why this matters

The user should see the AI editing artifacts in real time, like collaborative editing.

---

## 17. Security, Permissions, And Audit

### 17.1 Minimum enforcement

Every tool call should pass through:

- authentication
- authorization
- schema validation
- artifact existence checks
- operation audit logging

### 17.2 Sensitive operations

Higher-risk operations may require stricter policy:

- deleting sheets or documents
- exporting files outside a workspace
- bulk destructive edits

### 17.3 Audit record shape

At minimum log:

- timestamp
- actor or agent id
- tool name
- artifact id
- summary of arguments
- success or failure

### 17.4 Why this matters

If AI is given human-equivalent editing power, traceability is mandatory.

---

## 18. Error Model

Tool errors should be explicit and typed. Avoid vague generic failures.

Recommended categories:

- `INVALID_ARGUMENT`
- `NOT_FOUND`
- `PERMISSION_DENIED`
- `CONFLICT`
- `RATE_LIMITED`
- `UPSTREAM_LLM_ERROR`
- `INTERNAL_ERROR`

Example JSON-RPC error:

```json
{
  "jsonrpc": "2.0",
  "id": "req-001",
  "error": {
    "code": -32010,
    "message": "INVALID_ARGUMENT",
    "data": {
      "field": "range",
      "reason": "Range must be A1 notation"
    }
  }
}
```

---

## 19. Recommended Internal File Layout

This is a suggested structure for implementation inside an API server package.

```text
api-server/
  src/
    mcp/
      server.ts
      registry.ts
      types.ts
      middleware/
        auth.ts
        audit.ts
        validation.ts
        cache.ts
      routes/
        mcp.ts
    services/
      llm/
        openaiCompatibleClient.ts
      semantic/
        SemanticSheetParser.ts
        SemanticDocumentParser.ts
      cache/
        semanticCache.ts
    modules/
      nexcel/
        mcp/
          resources.ts
          tools.ts
          prompts.ts
        services/
          nexcelStoreAdapter.ts
      wordo/
        mcp/
          resources.ts
          tools.ts
          prompts.ts
        services/
          wordoStoreAdapter.ts
```

If KASUMI lives inside the broader HASHI system, the same structure can be nested under the existing server package rather than requiring a separate deployment.

---

## 20. Implementation Phases

### Phase 0: specification and contracts

Deliverables:

- this architecture plan
- canonical tool naming rules
- resource URI rules
- JSON schemas for first tools

### Phase 1: core server skeleton

Deliverables:

- registry
- JSON-RPC routes
- tool and resource listing
- shared middleware
- basic audit log

### Phase 2: Nexcel read path plus semantic analysis

Deliverables:

- Nexcel resources
- Nexcel read tools
- `SemanticSheetParser`
- cache-first semantic map flow
- OpenAI-compatible LLM client

### Phase 3: Nexcel write and formatting

Deliverables:

- write tools
- format tools
- structure tools
- invalidation rules
- websocket broadcast

### Phase 4: Wordo support

Deliverables:

- Wordo resources
- Wordo read and write tools
- Wordo semantic parser
- document outline support

### Phase 5: cross-module workflows and AI-native tools

Deliverables:

- convert Wordo tables into Nexcel
- cross-reference linking
- advanced AI-native helpers
- prompt templates

### Phase 6: governance and hardening

Deliverables:

- capability discovery
- stable versioning policy
- deprecation policy
- better permission tiers
- performance tuning

---

## 21. First Minimal Viable Slice

If implementation starts immediately, the recommended first slice is:

1. registry and JSON-RPC route skeleton
2. `nexcel_read_range`
3. `nexcel_read_semantic_map`
4. `nexcel_analyse_sheet`
5. OpenAI-compatible LLM client
6. semantic cache with content hash

This gives the system a useful end-to-end path with minimal surface area.

---

## 22. Design Rules For Future Modules

Every new KASUMI module should follow these rules:

### Rule 1: namespace everything

Tool names must be prefixed by module:

- `calendar_create_event`
- `canvas_add_shape`

### Rule 2: register, do not patch core

New modules should plug into the registry instead of editing server internals.

### Rule 3: publish schemas

Every tool and resource needs machine-readable input and output schema.

### Rule 4: support capability discovery

Agents must be able to ask the server what exists now.

### Rule 5: additive-first upgrades

Prefer adding new tools over changing old ones incompatibly.

### Rule 6: event consistently

Mutations should emit events using a predictable naming pattern.

### Rule 7: document every module

Every new module should ship:

- resource list
- tool list
- permissions notes
- examples
- upgrade notes

---

## 23. Documentation Requirements

To keep the MCP layer maintainable, documentation must evolve with the code.

Required docs for each new module:

- module overview
- supported resources
- supported tools
- JSON examples
- permission model
- cache and invalidation notes
- compatibility and deprecation notes

Recommended central docs:

- MCP architecture overview
- current tool catalog
- migration notes per version
- troubleshooting guide for tool failures

---

## 24. Open Questions

These items should be resolved before implementation hardens:

- where KASUMI server code will physically live in this repository
- whether MCP is exposed only internally first, or externally too
- how user identity and workspace permissions map to tool calls
- whether format-only changes should always trigger semantic reparse
- how large binary imports and exports should be streamed
- whether prompt templates should be part of v1 or deferred

---

## 25. Final Recommendation

Build a unified `Kasumi MCP Server` with registry-based extensibility, versioned JSON-RPC routes, and cache-first semantic parsing backed by any OpenAI-compatible LLM endpoint.

Start with Nexcel semantic read support, then expand into write and formatting, then bring Wordo into the same server surface. This gives immediate value while preserving a clean path for future modules.

The most important non-functional rule is this:

the MCP layer must be self-describing and additive, so KASUMI can grow without forcing agents or infrastructure to relearn the platform every time a new feature ships.
