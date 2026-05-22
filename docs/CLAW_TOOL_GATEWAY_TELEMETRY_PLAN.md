# Claw Tool Gateway And Telemetry Plan

Status: proposed long-term architecture
Owner: HASHI
Created: 2026-05-23
Related docs:

- [CLAW_CODE_MODULE_PLAN.md](CLAW_CODE_MODULE_PLAN.md)
- [HASHI_SLIM_CORE_ARCHITECTURE.md](HASHI_SLIM_CORE_ARCHITECTURE.md)
- [HASHI_LAYERED_RUNTIME_BOUNDARIES.md](HASHI_LAYERED_RUNTIME_BOUNDARIES.md)
- [TOKEN_AUDIT_SPEC.md](TOKEN_AUDIT_SPEC.md)
- [tools.md](tools.md)

## Decision

Do not solve Claw internet access with a short-lived adapter-only shim.

The long-term solution is a protocol boundary:

```text
HASHI Tool Gateway -> MCP/tools protocol -> Claw runtime -> HASHI telemetry
```

Claw should not know the internal shape of HASHI's `ToolRegistry`, and HASHI
should not embed Claw-specific tool routing into the core runtime. HASHI should
expose a stable tool gateway that can serve Claw, OpenRouter API agents,
DeepSeek API agents, Ollama API agents, and future model runtimes through
backend-appropriate protocol adapters.

The matching observability solution is a telemetry contract:

```text
Claw stream-json / structured events -> HASHI StreamEvent -> token audit
```

Do not fake reasoning token counts. If a provider/runtime does not expose real
thinking tokens, HASHI must record that explicitly.

## Problem Statement

API backends currently receive HASHI tools directly:

```text
openrouter-api / deepseek-api / ollama-api
  -> FlexibleBackendManager attaches ToolRegistry
  -> model calls OpenAI-format tools
  -> ToolRegistry executes web_search, web_fetch, browser_*, etc.
```

Claw mode is different:

```text
claw-cli
  -> HASHI starts a Claw subprocess
  -> Claw owns its own model/tool loop
  -> HASHI receives final JSON only
```

Therefore, a Claw backend agent can be configured in HASHI, but it does not
automatically inherit the internet tools that are available to API backends.
Passing `allowed_tools` to Claw only permits Claw-native tool names or tools that
Claw discovers through its own extension surfaces.

This caused agents such as `diaochan` to correctly report that the current
Claw backend did not have browser or URL-fetch capability, even though the same
agent has `openrouter-api` and `deepseek-api` configurations with HASHI
`tools.allowed = ["*"]`.

## Evidence From Current Code

HASHI attaches `ToolRegistry` only to API backends today:

```text
orchestrator/flexible_backend_manager.py
  engine in ("openrouter-api", "deepseek-api", "ollama-api")
    -> _attach_tool_registry(...)
```

The Claw adapter currently passes Claw's own tool list into the subprocess:

```text
adapters/claw_cli.py
  --allowedTools read,glob,grep
```

HASHI internet tools already exist in the registry:

```text
tools.registry.TOOL_TIERS["web"]
  web_search, web_fetch, http_request

tools.registry.TOOL_TIERS["browser"]
  browser_session, browser_screenshot, browser_get_text, browser_get_html,
  browser_click, browser_fill, browser_type_text, browser_evaluate,
  browser_scroll, browser_hover, browser_key, browser_select,
  browser_wait_for, browser_get_attribute, browser_drag, browser_upload
```

Claw's external runtime has the right long-term extension shapes:

```text
runtime::ToolExecutor
runtime MCP stdio discovery
plugin tools
ConversationRuntime model/tool iteration
AssistantEvent::Thinking
```

But the current Claw CLI JSON surface is not enough for parity:

```text
--output-format supports text/json, not stream-json
JSON prompt output includes message, model, iterations, tool_uses, tool_results, usage
usage includes input/output/cache tokens, not thinking_tokens
thinking blocks are rendered as hidden summaries, not emitted as machine telemetry
```

## Architecture Goals

1. Keep HASHI core slim and hot-rebootable.
2. Keep Claw optional and replaceable.
3. Make internet tools available to Claw through a standard tool protocol.
4. Reuse one HASHI tool permission/audit implementation instead of duplicating
   web/browser tool behavior for every backend.
5. Make telemetry explicit, structured, and audit-friendly.
6. Preserve backward compatibility for current API backends and current
   `claw-cli` agents.
7. Avoid pretending that estimated or unavailable thinking tokens are real.

## Non-Goals

- Do not move Claw into `main.py`.
- Do not make Cargo, Rust, or Claw source checkout a HASHI runtime dependency.
- Do not add Claw-only internet code paths to HASHI core.
- Do not parse human terminal output as the permanent telemetry protocol.
- Do not expose raw hidden chain-of-thought by default.
- Do not report `thinking_tokens = 0` when the correct value is unknown.

## Target Architecture

```text
┌───────────────────────────────┐
│ HASHI agent runtime            │
│ - backend switching            │
│ - verbose display              │
│ - token audit                  │
└───────────────┬───────────────┘
                │
                ▼
┌───────────────────────────────┐
│ Backend adapter                │
│ - API adapter                  │
│ - Claw adapter                 │
│ - future adapters              │
└───────┬───────────────────────┘
        │
        ├────────────────────────────────────────────┐
        │                                            │
        ▼                                            ▼
┌───────────────────────────────┐        ┌───────────────────────────────┐
│ HASHI Tool Gateway             │        │ Agent Telemetry Contract       │
│ - schema catalog               │        │ - run_started                  │
│ - permission policy            │        │ - thinking_summary             │
│ - audit logging                │        │ - thinking_tokens              │
│ - ToolRegistry execution       │        │ - tool_start/tool_end          │
│ - MCP/OpenAI adapters          │        │ - usage                        │
└───────────────┬───────────────┘        └───────────────┬───────────────┘
                │                                        │
                ▼                                        ▼
      web_search / web_fetch / browser_*        StreamEvent / token_audit
```

For Claw specifically:

```text
claw-cli
  -> configured MCP stdio server: hashi-tool-gateway
  -> Claw model calls MCP tools
  -> Tool Gateway executes existing HASHI tools
  -> Claw emits structured telemetry
  -> HASHI records usage and displays verbose status
```

## HASHI Tool Gateway

The Tool Gateway is a stable facade around the existing `ToolRegistry`.

Responsibilities:

- Load the same tool catalog used by API backends.
- Resolve enabled tools from `tools.allowed`, tiers, and per-agent policy.
- Execute existing `ToolRegistry.execute(...)`.
- Enforce workspace/access-root rules.
- Enforce permissions and tool options.
- Add audit context for agent, backend, request, workspace, tool, duration,
  output size, and error state.
- Expose the catalog through multiple protocol adapters:
  - OpenAI function definitions for API backends.
  - MCP stdio server for Claw and other MCP clients.
  - Internal Python API for existing runtime callers.

The gateway must be hot-reloadable and live outside `main.py`.

Suggested package shape:

```text
tools/gateway/
  __init__.py
  service.py          # ToolGateway facade
  audit.py            # structured tool audit records
  policy.py           # allowed tools, tiers, permission decisions
  openai.py           # OpenAI function schema adapter
  mcp_stdio.py        # MCP stdio server adapter
  types.py            # gateway request/result models
```

The current `tools.registry.ToolRegistry` remains the execution core at first.
The gateway becomes the protocol and policy facade.

## Claw MCP Integration

Claw already has MCP lifecycle and stdio discovery surfaces. The long-term
integration should use them instead of a Claw-only plugin script layer.

Target Claw config per agent:

```json
{
  "mcpServers": {
    "hashi-tools": {
      "command": "python3",
      "args": [
        "-m",
        "tools.gateway.mcp_stdio",
        "--agent",
        "diaochan",
        "--backend",
        "claw-cli"
      ],
      "required": true
    }
  }
}
```

The Claw adapter should generate or select this config under an isolated
agent-specific Claw config home, for example:

```text
workspaces/<agent>/.claw-hashi/config/
```

The adapter should not require modifying a user's global `~/.claw` config.

Required startup behavior:

- If the gateway MCP server starts and lists tools, Claw mode is healthy.
- If a required gateway fails, Claw backend initialization should fail clearly.
- If optional future MCP servers fail, the status should be degraded rather
  than silently losing tools.
- Diagnostics must identify which server failed and why.

## Internet Tool Surface

The first required gateway tier for Claw parity is:

```text
web
browser
```

That expands to:

```text
web_search
web_fetch
http_request
browser_session
browser_screenshot
browser_get_text
browser_get_html
browser_click
browser_fill
browser_type_text
browser_evaluate
browser_scroll
browser_hover
browser_key
browser_select
browser_wait_for
browser_get_attribute
browser_drag
browser_upload
```

Later tiers can expose file, shell, communication, desktop, Windows, Obsidian,
and remote tools, but internet parity should be proven first.

## Agent Telemetry Contract

HASHI needs one backend-neutral event contract.

Canonical events:

```text
run_started
model_delta
thinking_started
thinking_summary
thinking_tokens
tool_start
tool_end
usage
run_finished
error
```

Minimum fields:

```json
{
  "type": "tool_start",
  "request_id": "req-0001",
  "agent": "diaochan",
  "backend": "claw-cli",
  "model": "deepseek/deepseek-v4-pro",
  "tool_name": "web_fetch",
  "tool_call_id": "toolu_...",
  "timestamp": "2026-05-23T07:00:00+10:00"
}
```

Usage event:

```json
{
  "type": "usage",
  "input_tokens": 1000,
  "output_tokens": 300,
  "thinking_tokens": 50,
  "thinking_token_source": "real",
  "cached_tokens": 0,
  "cost_usd": 0.0012
}
```

Allowed `thinking_token_source` values:

```text
real
estimated
unavailable
not_applicable
```

Rules:

- `real`: provider/runtime returned a reasoning token count.
- `estimated`: HASHI estimated from visible or structured thinking text.
- `unavailable`: runtime can prove thinking may exist but does not expose count.
- `not_applicable`: backend/model does not produce a separate thinking channel.

Do not use `0` to mean unknown.

## Claw Telemetry Requirements

Claw should gain a machine-readable streaming output format:

```text
claw --output-format stream-json prompt "..."
```

Each line should be a JSON event matching a stable schema. Required event kinds:

```text
run_started
assistant_delta
thinking_summary
tool_start
tool_end
usage
run_finished
error
```

Claw currently has `AssistantEvent::Thinking`, but the CLI path renders it as a
hidden human summary and does not emit it into prompt JSON. Long-term parity
requires preserving structured thinking telemetry without leaking raw hidden
chain-of-thought by default.

Suggested Claw-side changes:

```text
runtime/src/usage.rs
  add thinking_tokens/reasoning_tokens

api/providers/openai_compat.rs
  parse completion_tokens_details.reasoning_tokens
  preserve DeepSeek reasoning_content as thinking telemetry

rusty-claude-cli/src/main.rs
  add CliOutputFormat::StreamJson
  emit tool/thinking/usage/run events
  include thinking_token_source in final JSON
```

## HASHI Claw Adapter Requirements

The HASHI `claw-cli` adapter should:

- Resolve Claw binary and provider as it does today.
- Prepare an isolated Claw config home for the agent.
- Configure the HASHI Tool Gateway MCP server.
- Validate Claw MCP readiness during `initialize()`.
- Prefer `--output-format stream-json` when available.
- Fall back to `--output-format json` only as a compatibility mode.
- Convert Claw telemetry into HASHI `StreamEvent`.
- Populate `BackendResponse.usage` with real fields when available.
- Set `thinking_token_source` in token audit.
- Log a clear capability warning when Claw lacks `stream-json` support.

The adapter should not execute HASHI tools directly on behalf of Claw. Tool
execution should happen through the gateway protocol.

## Logging And Audit Requirements

Every gateway tool call must write structured audit data:

```json
{
  "ts": "2026-05-23T07:00:00+10:00",
  "request_id": "req-0001",
  "agent": "diaochan",
  "backend": "claw-cli",
  "tool_name": "web_fetch",
  "tool_call_id": "toolu_...",
  "allowed": true,
  "duration_ms": 512,
  "input_hash": "sha256:...",
  "output_chars": 4096,
  "is_error": false,
  "error_kind": null
}
```

Do not log raw secrets, cookies, bearer tokens, or full browser output by
default. Store hashes, sizes, and redacted previews.

## Rollout Plan

### Phase A: Tool Gateway Foundation

Deliverables:

- Add `tools/gateway/service.py`.
- Wrap existing `ToolRegistry` without changing current API backend behavior.
- Add gateway audit records.
- Add tests proving wildcard and tier filtering still match current registry.

Acceptance:

- Existing API backend tool tests still pass.
- Gateway and direct `ToolRegistry` return equivalent results for selected
  `web_fetch`, `http_request`, and permission-denied cases.
- No changes to `main.py`.

### Phase B: MCP Server Adapter

Deliverables:

- Add `tools/gateway/mcp_stdio.py`.
- Expose `tools/list` and `tools/call`.
- Convert HASHI schemas to MCP `inputSchema`.
- Add startup/degraded diagnostics.

Acceptance:

- MCP list returns the expected internet tools.
- MCP call executes `web_fetch` and returns MCP text content.
- Bad tool calls return structured MCP errors.
- Tool audit records are written.

### Phase C: Claw MCP Config Integration

Deliverables:

- Add Claw adapter support for isolated `CLAW_CONFIG_HOME`.
- Generate/select per-agent Claw MCP config.
- Validate required `hashi-tools` MCP server before accepting the backend.
- Add `/status` or backend diagnostics showing Claw gateway readiness.

Acceptance:

- A Claw backend agent can fetch a public URL through the gateway.
- Removing the gateway command causes clear backend initialization failure.
- Existing non-Claw backends are unchanged.
- `/reboot min` adopts adapter/config changes.

### Phase D: Claw Telemetry Contract

Deliverables:

- Add or adopt Claw `stream-json`.
- Add HASHI parser for Claw telemetry events.
- Convert tool and model events to `StreamEvent`.
- Record `thinking_token_source`.

Acceptance:

- Telegram verbose shows Claw tool start/end events.
- Token audit records real, estimated, unavailable, or not_applicable thinking
  source accurately.
- Claw JSON fallback continues to work for older binaries, with a warning.

### Phase E: Real Thinking Token Support

Deliverables:

- Update Claw usage model to carry reasoning tokens.
- Parse OpenAI-compatible `completion_tokens_details.reasoning_tokens`.
- Surface provider reasoning usage in Claw final and streaming telemetry.

Acceptance:

- DeepSeek/OpenRouter through Claw reports real reasoning tokens when the
  provider returns them.
- If a provider does not return them, audit shows `unavailable` rather than `0`.

### Phase F: Agent Migration

Deliverables:

- Configure selected Claw agents to use gateway-backed internet tools.
- Keep API agents on existing direct ToolRegistry path until gateway parity is
  proven.
- Migrate API adapters to gateway facade only after behavior is equivalent.

Acceptance:

- `diaochan` can fetch and summarize a public URL in `claw-cli` mode.
- `openrouter-api`, `deepseek-api`, and `ollama-api` keep existing tool access.
- Tool audit output has consistent shape across backend types.

## Compatibility Strategy

Current Claw JSON mode remains supported:

```text
--output-format json
```

If `stream-json` is unavailable:

- Claw adapter can still return final text.
- Tool use counts may be available from final JSON.
- Real-time verbose is limited.
- Thinking tokens must be marked `unavailable` unless explicit usage exists.

This preserves compatibility with existing Claw binaries while establishing the
new target contract.

## Validation Matrix

Focused Python checks:

```bash
python3 -m py_compile adapters/claw_cli.py tools/registry.py
pytest tests/test_claw_cli_adapter.py tests/test_workzone.py tests/test_deepseek_api.py
```

Gateway checks:

```bash
pytest tests/test_tool_gateway.py tests/test_tool_gateway_mcp.py
```

Live smoke checks:

```text
Claw MCP list: sees web and browser tools
Claw web_fetch: fetches a public URL
Claw browser_get_text: reads a public page
Claw missing gateway: fails clearly
Claw stream-json: emits tool_start/tool_end/usage
Token audit: thinking_token_source is accurate
```

Regression checks:

```text
/reboot min adopts adapter changes
/reboot max keeps all active agents online
API backend tools still work
No secrets in logs
No HASHI core restart required for gateway/adapter updates
```

## Open Questions

1. Should the first gateway tier for Claw be `web,browser` only, or `*` for all
   tools after internet parity is proven?
2. Should Claw gateway MCP be required by default for every `claw-cli` backend,
   or opt-in via backend config?
3. Should browser tools run through the current browser bridge, Playwright
   fallback, or both?
4. Should raw thinking text ever be surfaced to users, or only hidden summaries
   and token counts?
5. Should API backends move to Tool Gateway in the same release or after Claw
   parity is proven?

## Recommended Answers

1. Start with `web,browser`; expand only after audit and safety behavior are
   proven.
2. Make gateway required when `tools` is configured on `claw-cli`; otherwise
   preserve current read-only Claw behavior.
3. Prefer existing browser bridge discovery first, with Playwright fallback.
4. Do not show raw hidden reasoning by default. Show status and token counts.
5. Keep API direct path initially; migrate to gateway facade after parity tests.

## Acceptance Definition

This plan is complete when:

- Claw backend agents can use HASHI internet tools through a standard gateway.
- Tool execution is logged with structured, redacted audit records.
- Thinking/token telemetry is explicit and source-labelled.
- Existing API backend behavior is preserved.
- No Claw-specific behavior is added to HASHI core.
- The feature can be adopted by `/reboot` without restarting the HASHI process.
