# Claw Tool Gateway And Telemetry Plan

Status: proposed long-term architecture, updated for in-package Claw runtime
Owner: HASHI
Created: 2026-05-23
Updated: 2026-05-23
Related docs:

- [CLAW_CODE_MODULE_PLAN.md](CLAW_CODE_MODULE_PLAN.md)
- [HASHI_SLIM_CORE_ARCHITECTURE.md](HASHI_SLIM_CORE_ARCHITECTURE.md)
- [HASHI_LAYERED_RUNTIME_BOUNDARIES.md](HASHI_LAYERED_RUNTIME_BOUNDARIES.md)
- [TOKEN_AUDIT_SPEC.md](TOKEN_AUDIT_SPEC.md)
- [tools.md](tools.md)

## Decision

Do not solve Claw internet access with a short-lived adapter-only shim.

Since the Claw codebase must be modified for durable tool and telemetry
support, HASHI should stop treating Claw as a user-installed external binary.
The long-term solution is to vendor/refactor Claw into the HASHI repository and
ship a HASHI-managed Claw runtime as part of the HASHI package.

Users should not need to download or build a separate Claw checkout to use Claw
mode.

The runtime boundary remains process-level:

```text
HASHI package
  -> packaged hashi-claw executable
  -> HASHI Tool Gateway over MCP/tools protocol
  -> HASHI telemetry
```

Claw should become a HASHI-owned packaged subsystem, not HASHI core. HASHI
should preserve a stable adapter/process boundary so `/reboot` can adopt Python
adapter and gateway changes without restarting the whole process. The packaged
Claw binary can be upgraded through normal HASHI release/package updates.

Claw should not know the internal shape of HASHI's `ToolRegistry`. HASHI should
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
  -> HASHI starts a Claw subprocess, currently resolved from an external binary
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

There is also a deployment problem. If another HASHI instance pulls the latest
adapter code but does not have a compatible Claw binary installed, Claw mode is
not actually portable. That violates the desired user experience for the HASHI
deck: the package should carry what it needs.

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
2. Make Claw mode work from the HASHI package without a separate user download.
3. Make internet tools available to Claw through a standard tool protocol.
4. Reuse one HASHI tool permission/audit implementation instead of duplicating
   web/browser tool behavior for every backend.
5. Make telemetry explicit, structured, and audit-friendly.
6. Preserve backward compatibility for current API backends and current
   `claw-cli` agents.
7. Avoid pretending that estimated or unavailable thinking tokens are real.
8. Keep the packaged Claw runtime replaceable at the adapter boundary.

## Non-Goals

- Do not move Claw into `main.py`.
- Do not require users to install Cargo, Rust, or a separate Claw checkout at
  runtime.
- Do not add Claw-only internet code paths to HASHI core.
- Do not parse human terminal output as the permanent telemetry protocol.
- Do not expose raw hidden chain-of-thought by default.
- Do not report `thinking_tokens = 0` when the correct value is unknown.
- Do not make HASHI process startup depend on compiling Rust.

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
hashi-claw packaged runtime
  -> configured MCP stdio server: hashi-tool-gateway
  -> Claw model calls MCP tools
  -> Tool Gateway executes existing HASHI tools
  -> Claw emits structured telemetry
  -> HASHI records usage and displays verbose status
```

## In-Package Claw Runtime

Claw should be integrated into the HASHI source and release pipeline as a
packaged sidecar runtime.

Recommended source layout:

```text
vendor/claw-code/
  LICENSE
  rust/
    Cargo.toml
    crates/...

packaging/claw/
  build.py                 # build/release helper
  manifest.json            # expected binary names, versions, checksums
  README.md                # maintainer notes

hashi_assets/claw/
  bin/
    linux-x86_64/hashi-claw
    windows-x86_64/hashi-claw.exe
    macos-arm64/hashi-claw
  manifest.json
```

The exact path can be adjusted to match packaging conventions, but the boundary
should stay clear:

```text
vendored source -> release build artifact -> packaged runtime binary
```

Runtime lookup order in `adapters/claw_cli.py` should become:

1. Explicit backend `claw_binary_path` or `claw_cmd`.
2. Global HASHI `claw_binary_path`.
3. Packaged HASHI Claw binary for the current platform.
4. `CLAW_BINARY` / `CLAW_BIN`.
5. `PATH` fallback for developer overrides.

That keeps developer flexibility while making the packaged runtime the normal
user path.

### License And Attribution

The reviewed Claw source is MIT licensed:

```text
Copyright (c) 2026 UltraWorkers and Claw Code contributors
```

HASHI packaging must preserve:

- Claw `LICENSE`.
- A vendored attribution note in HASHI release materials.
- Any dependency license obligations from the Rust `Cargo.lock` dependency set.

No vendoring should occur without keeping the upstream license text in the
distributed package.

### Build-Time Versus Runtime Dependency

Cargo and Rust are allowed in release/build CI.

Cargo and Rust are not allowed as normal runtime prerequisites for HASHI users.

Release artifacts should ship prebuilt Claw binaries for supported platforms.
If a platform has no packaged binary, Claw mode should report a clear
unsupported-platform diagnostic rather than asking the user to manually build
Claw.

### Versioning

The packaged Claw runtime should have explicit version metadata:

```json
{
  "hashi_claw_version": "0.1.0-hashi.1",
  "upstream_claw_commit": "f8e1bb7262b261da1ee6bfcd461bfc5b676f6a6d",
  "build_target": "linux-x86_64",
  "sha256": "..."
}
```

`claw-cli` backend status should include:

```text
binary_source = packaged | explicit | env | path
binary_path
hashi_claw_version
upstream_claw_commit
hashi_platform_key
rust_target_triple
tool_gateway = healthy | degraded | unavailable
telemetry = stream-json | json-fallback
```

### Binary Naming

The canonical packaged executable name is `hashi-claw`.

The vendored Rust crate may still build an upstream binary named `claw`. Release
packaging must copy or rename that build output into the HASHI package as:

```text
hashi-claw
hashi-claw.exe
```

External overrides can still point at any executable name, including an
upstream `claw` binary.

### Platform Keys

Do not pass HASHI package platform keys directly to Cargo. The package manifest
must distinguish HASHI's stable platform key from Rust's target triple:

```json
{
  "hashi_platform_key": "linux-x86_64",
  "rust_target_triple": "x86_64-unknown-linux-gnu",
  "binary_name": "hashi-claw",
  "sha256": "..."
}
```

Initial release gates:

```text
linux-x86_64      -> x86_64-unknown-linux-gnu
windows-x86_64    -> x86_64-pc-windows-msvc
```

Planned but not first-gate targets:

```text
macos-arm64
linux-aarch64
windows-arm64
```

Unsupported platforms must fail with a clear diagnostic and keep explicit
binary override available.

### Checksum Policy

Packaged binaries should be checked against `hashi_assets/claw/manifest.json`.

If the packaged binary checksum fails:

1. Mark `binary_integrity = failed` in diagnostics.
2. Do not execute that packaged binary.
3. Continue the lookup chain only for explicit/env/PATH overrides.
4. If no safe override exists, fail Claw backend initialization clearly.

This is stricter than a warning-only policy because executing a corrupted
packaged runtime is riskier than requiring an explicit override.

### Packaging Inclusion

`hashi_assets/` does not exist in the current repository and must be added with
explicit packaging rules. Phase 0 must update the active packaging mechanism:

```text
MANIFEST.in
setup.py package_data or pyproject.toml setuptools package-data
release/deploy scripts
```

Acceptance requires a built HASHI artifact to contain:

```text
hashi_assets/claw/manifest.json
hashi_assets/claw/bin/<hashi_platform_key>/hashi-claw
vendor/claw-code/LICENSE
```

The current `setup.py` only packages selected Python packages and does not yet
include arbitrary binary assets. This must be fixed before the packaged runtime
can be considered shipped.

## Review Feedback Evaluation

Akane's technical review is accepted with one important modification.

Accepted feedback:

- Add a design-freeze phase before implementation.
- Define how MCP stdio gets runtime state.
- Define API backend parity gates before changing gateway execution.
- Delay `browser_*` until stateless web tools prove the gateway path.
- Define binary naming, platform key mapping, checksums, package-data rules,
  input validation, error kinds, and timeout tiers.
- Preserve the long-term direction: in-package Claw runtime plus Tool Gateway
  and telemetry.

Modified feedback:

- A raw "MCP server reloads all HASHI config and secrets" design is too broad.
  The safer Phase B design is a per-agent `GatewayContext` snapshot generated by
  HASHI with only the minimal state needed by the MCP server. Future IPC can
  replace or supplement this after the web tier is stable.

Rejected feedback:

- None of the review findings require changing the long-term architecture.
  They are design-freeze requirements, not direction blockers.

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

## Gateway Runtime State

Claw launches the Tool Gateway MCP adapter as a separate stdio subprocess. That
process cannot rely on in-memory `FlexibleBackendManager` state. It needs a
documented state handoff.

The Phase B design is a per-agent GatewayContext snapshot.

```text
HASHI runtime / Claw adapter
  -> resolves backend, workspace, access root, tool policy, and minimal secrets
  -> writes GatewayContext JSON with 0600 permissions
  -> launches Claw with MCP server args pointing at that context
  -> MCP server reads context and constructs ToolGateway/ToolRegistry
```

Suggested context path:

```text
workspaces/<agent>/backend_state/claw_gateway_context.json
```

Suggested context shape:

```json
{
  "schema_version": 1,
  "agent": "diaochan",
  "backend": "claw-cli",
  "workspace_dir": "workspaces/diaochan",
  "access_root": "/home/lily/projects/hashi",
  "workzone_dir": null,
  "allowed_tools": ["web_search", "web_fetch", "http_request"],
  "max_loops": 25,
  "tool_options": {},
  "secrets": {
    "brave_api_key": {"source": "secrets.json", "required": false}
  },
  "audit": {
    "agent_name": "diaochan",
    "backend": "claw-cli",
    "safety_mode": "read_only_web"
  }
}
```

Secrets policy:

- Prefer references to secret names over copying secret values.
- If a value must be copied into the context, it must be scoped to the minimum
  required tool and the context file must be owner-readable only.
- The MCP process must never log secret values.
- Browser cookies, bearer tokens, and full page outputs are never written to the
  context.

Why not use direct config reload as the default?

- It is simple, but broad. A subprocess that reloads global config and all
  secrets has more authority than the requested tool tier needs.
- It can drift from the already-running agent's resolved workzone/access-root
  state.
- It makes audit reasoning harder because the MCP process reconstructs policy
  rather than receiving the policy snapshot selected by HASHI.

Future optimization:

- Replace or supplement the snapshot with an authenticated local IPC gateway
  once the stateless web tier is stable.
- TCP/WebSocket transports must require authentication. Stdio transport can rely
  on parent-child pipe isolation plus context-file permissions.

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
        "--context",
        "workspaces/diaochan/backend_state/claw_gateway_context.json",
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

The first required gateway tier for Claw parity is the stateless web tier:

```text
web
```

That expands to:

```text
web_search
web_fetch
http_request
```

The browser tier is intentionally deferred until the web tier proves the
cross-process gateway path:

```text
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

Reason:

- `web_search`, `web_fetch`, and `http_request` are mostly stateless HTTP tools.
- `browser_*` tools need browser bridge discovery, session coordination, CDP
  access, page state, and stronger audit controls.

Later tiers can expose file, shell, communication, desktop, Windows, Obsidian,
and remote tools, but internet parity should be proven first.

## Gateway Policy And Validation

The Tool Gateway must validate tool input before dispatch.

Validation requirements:

- Validate MCP tool arguments against the existing `TOOL_SCHEMA_MAP` schemas.
- Reject unknown tool names before dispatch.
- Reject malformed arguments with `error_kind = parse_error`.
- Reject disallowed tools with `error_kind = permission_denied`.
- Apply per-tool URL and protocol restrictions. For example, `web_fetch` and
  `http_request` must not silently allow local file reads through unsupported
  URL schemes.

Initial `error_kind` enum:

```text
permission_denied
parse_error
schema_validation_error
timeout
network_error
tool_error
binary_integrity_failed
unsupported_platform
unknown
```

Timeout tiers:

```text
diagnostic command       30s
single web tool call     120s
short prompt             600s
long coding task         3600s
```

The existing `DEFAULT_CLAW_TASK_TIMEOUT_SEC = 1800` is acceptable only as a
temporary default. The packaged runtime plan must move toward tier-specific
timeouts.

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

The packaged HASHI Claw runtime should gain a machine-readable streaming output
format:

```text
hashi-claw --output-format stream-json prompt "..."
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

Required Claw-side changes in the vendored/refactored runtime:

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
  "error_kind": null,
  "transport": "mcp_stdio"
}
```

Do not log raw secrets, cookies, bearer tokens, or full browser output by
default. Store hashes, sizes, and redacted previews.

When `is_error = true`, `error_kind` must use the enum defined in
[Gateway Policy And Validation](#gateway-policy-and-validation).

## Rollout Plan

### Phase 0: Vendor And Package Claw Runtime

Deliverables:

- Import the reviewed MIT Claw source into a dedicated vendor directory.
- Preserve Claw `LICENSE` and attribution.
- Add packaging metadata for supported binary targets.
- Add release/build scripts that compile upstream `claw` and package it as
  `hashi-claw` during CI or release
  preparation, not during normal HASHI startup.
- Add packaged-binary lookup to `adapters/claw_cli.py`.
- Add status diagnostics for binary source, version, build target, and checksum.
- Add `MANIFEST.in`, `setup.py`, `pyproject.toml`, or release-script changes
  so `hashi_assets/claw` is actually included in distributed artifacts.

Acceptance:

- A clean HASHI package install can resolve a Claw binary without a separate
  Claw checkout.
- Runtime startup does not require Cargo or Rust.
- Explicit `claw_binary_path` still overrides the packaged binary for developer
  testing.
- `hashi_platform_key` and `rust_target_triple` are both present in the
  packaged manifest.
- Packaged binary checksum failure prevents that packaged binary from executing
  and produces a clear diagnostic.
- Unsupported platforms fail with a clear diagnostic.
- License text is present in the distributed package.

### Phase 0.5: Design Freeze

This phase must complete before Phase A implementation begins.

Decisions to freeze:

1. Gateway state handoff uses per-agent `GatewayContext` snapshots for Phase B.
2. Direct full-config reload is not the default MCP state-sharing strategy.
3. Future IPC is allowed only after the stateless web tier is stable.
4. The canonical packaged executable name is `hashi-claw`.
5. Platform manifest distinguishes `hashi_platform_key` from
   `rust_target_triple`.
6. First Tool Gateway parity tier is `web` only:
   `web_search`, `web_fetch`, `http_request`.
7. Browser tier is delayed until cross-process web tools and audit are stable.
8. API backend parity gate is mandatory before any API adapter migrates from
   direct `ToolRegistry` execution to gateway-wrapped execution.
9. `hashi_assets/` packaging inclusion strategy is selected and documented.

Acceptance:

- This document records the final Phase A/B tool scope.
- Tests to compare direct `ToolRegistry` and gateway execution are specified.
- GatewayContext file schema is stable enough for implementation.
- Security review covers context-file permissions and secret handling.
- No code is required in Phase 0.5 except documentation/test-plan updates.

### Phase A: Tool Gateway Foundation

Deliverables:

- Add `tools/gateway/service.py`.
- Wrap existing `ToolRegistry` without changing current API backend behavior.
- Add gateway audit records.
- Add tests proving wildcard and tier filtering still match current registry.
- Add direct-vs-gateway parity tests for `web_search`, `web_fetch`,
  `http_request`, and representative permission-denied / parse-error cases.

Acceptance:

- Existing API backend tool tests still pass.
- Gateway and direct `ToolRegistry` return equivalent results for selected
  web-tier tools and permission-denied cases.
- Any output difference between direct and gateway execution is documented and
  justified.
- No changes to `main.py`.

### Phase B: MCP Server Adapter

Deliverables:

- Add `tools/gateway/mcp_stdio.py`.
- Expose `tools/list` and `tools/call`.
- Convert HASHI schemas to MCP `inputSchema`.
- Add startup/degraded diagnostics.
- Read per-agent GatewayContext snapshots.
- Add per-tool schema validation before dispatch.

Acceptance:

- MCP list returns the expected web-tier tools.
- MCP call executes `web_fetch` and returns MCP text content.
- Bad tool calls return structured MCP errors.
- Tool audit records are written.
- GatewayContext files are required to be owner-readable only.

### Phase B2: Browser Gateway Design

Deliverables:

- Specify browser bridge discovery and CDP endpoint handling for subprocess
  gateway execution.
- Define browser session audit fields and redaction policy.
- Define whether browser tools use the existing browser bridge, Playwright
  fallback, or both.
- Add direct-vs-gateway parity tests for `browser_get_text` and one interactive
  browser action before broad browser rollout.

Acceptance:

- Browser tools are not enabled for Claw MCP until session and audit behavior is
  specified.
- Browser failures are categorized as `network_error`, `timeout`, `tool_error`,
  or `permission_denied` instead of generic unknown errors.

### Phase C: Claw MCP Config Integration

Deliverables:

- Add Claw adapter support for isolated `CLAW_CONFIG_HOME`.
- Generate/select per-agent Claw MCP config.
- Validate required `hashi-tools` MCP server before accepting the backend.
- Add `/status` or backend diagnostics showing Claw gateway readiness.
- Ensure generated Claw config targets the packaged `hashi-claw` runtime by
  default.
- Define mapping between HASHI `tools.allowed` and Claw `--allowedTools`:
  HASHI tools are exposed through MCP; Claw-native tools remain controlled by
  Claw's own allowed tool list.

Acceptance:

- The packaged Claw runtime starts from a clean HASHI install.
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
- Detect when the packaged binary changed since the last version check and
  re-run a lightweight version/integrity check.

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

Current explicit external binary configuration also remains supported. The
packaged binary is the default user path, but developers can still override it:

```json
{
  "engine": "claw-cli",
  "claw_binary_path": "/path/to/custom/claw"
}
```

This prevents the integrated package from blocking upstream Claw testing or
hotfix validation.

## Validation Matrix

Focused Python checks:

```bash
python3 -m py_compile adapters/claw_cli.py tools/registry.py
pytest tests/test_claw_cli_adapter.py tests/test_workzone.py tests/test_deepseek_api.py
```

Packaged runtime checks:

```bash
pytest tests/test_claw_packaging.py
python3 scripts/claw_package_probe.py --json
```

Gateway checks:

```bash
pytest tests/test_tool_gateway.py tests/test_tool_gateway_mcp.py
pytest tests/test_tool_gateway_parity.py
```

Live smoke checks:

```text
Packaged Claw binary resolves without external checkout
Packaged Claw version/sha appears in backend status
Claw MCP list: sees web-tier tools
Claw web_fetch: fetches a public URL
Claw missing gateway: fails clearly
Claw stream-json: emits tool_start/tool_end/usage
Token audit: thinking_token_source is accurate
```

Browser smoke checks are added only after Phase B2:

```text
Claw browser_get_text: reads a public page
Claw browser_session: preserves expected session behavior
Browser audit: redacted and categorized
```

Regression checks:

```text
/reboot min adopts adapter changes
/reboot max keeps all active agents online
API backend tools still work
No secrets in logs
No Cargo/Rust needed at runtime
No HASHI core restart required for gateway/adapter updates
```

## Open Questions

1. Should the first gateway tier for Claw be `web` only, `web,browser`, or `*`?
2. Should Claw gateway MCP be required by default for every `claw-cli` backend,
   or opt-in via backend config?
3. Should browser tools run through the current browser bridge, Playwright
   fallback, or both?
4. Should raw thinking text ever be surfaced to users, or only hidden summaries
   and token counts?
5. Should API backends move to Tool Gateway in the same release or after Claw
   parity is proven?
6. Should HASHI ship one full package with Claw included, or split a slim core
   package and a full deck package?
7. Which platform binaries are release-blocking for the first in-package Claw
   rollout?
8. Should MCP state sharing use full config reload, context snapshots, or IPC?

## Recommended Answers

1. Start with `web` only. Add `browser` after cross-process session and audit
   behavior are proven.
2. Make gateway required when `tools` is configured on `claw-cli`; otherwise
   preserve current read-only Claw behavior.
3. Prefer existing browser bridge discovery first, with Playwright fallback.
4. Do not show raw hidden reasoning by default. Show status and token counts.
5. Keep API direct path initially; migrate to gateway facade after parity tests.
6. Ship Claw in the normal full HASHI deck. A separate slim package can exist
   later, but it must not be the default user path for Claw mode.
7. Treat Linux/WSL x86_64 and Windows x86_64 as first rollout gates; add macOS
   after the build pipeline is stable.
8. Use per-agent GatewayContext snapshots first; reserve IPC for a later
   optimization.

## Acceptance Definition

This plan is complete when:

- Claw backend agents can use HASHI internet tools through a standard gateway.
- Tool execution is logged with structured, redacted audit records.
- Thinking/token telemetry is explicit and source-labelled.
- Existing API backend behavior is preserved.
- No Claw-specific behavior is added to HASHI core.
- A normal HASHI package contains the Claw runtime needed by `claw-cli` mode.
- MCP subprocess state is passed through minimal, permissioned GatewayContext
  snapshots rather than full broad config reconstruction.
- The feature can be adopted by `/reboot` without restarting the HASHI process.
