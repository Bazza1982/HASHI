# Codex API Tool-Call Bridge

Status: implemented. Runtime adoption requires a normal HASHI `/reboot` after
the code is deployed.

## Purpose

The HASHI OpenAI-compatible API Gateway supports caller-owned function tools on
Codex CLI models without granting Codex access to the caller's implementation.
HASHI translates between two protocols:

1. The API client sends OpenAI Chat Completions `tools` and structured
   `messages`.
2. HASHI starts one isolated Codex `app-server` process and maps those schemas
   to experimental `dynamicTools` under a deterministic, reserved-name-safe
   internal namespace.
3. Codex emits an `item/tool/call` request.
4. HASHI stops that model turn and returns an OpenAI `assistant.tool_calls`
   message. HASHI does not execute the function.
5. The client executes its own function and sends the complete conversation,
   including the assistant call and matching `role: "tool"` result.
6. HASHI injects the structured history into a new ephemeral Codex thread. The
   model either returns the next tool batch or the final assistant answer.

This is a stateless API boundary. It deliberately does not reuse the Telegram
agent's Codex thread or HASHI's optional Gateway `session_id` cache.

## Public Contract

Use `POST /v1/chat/completions` with a Codex model returned by
`GET /v1/models`.

Supported OpenAI fields for this path:

- `messages` roles: `system`, `developer`, `user`, `assistant`, and `tool`
- function `tools` with `name`, optional `description`, and JSON Schema
  `parameters`
- `tool_choice`: `auto`, `none`, `required`, or a named function
- `parallel_tool_calls`: `true` or `false`
- `reasoning_effort`: a model-supported request-scoped Codex effort
- `stream`: `true` or `false`
- `n`: `1`

Response behavior:

- A tool batch returns `finish_reason: "tool_calls"`, `content: null`, and one
  or more standard OpenAI function calls.
- A completed answer returns `finish_reason: "stop"` and assistant text.
- Streaming sends complete `delta.tool_calls` entries; partial argument-token
  streaming is not promised.
- Backend/protocol/isolation failures are explicit errors. HASHI never converts
  a lost tool schema into a plain text completion.
- The Gateway validates `reasoning_effort` before adapter acquisition and uses
  it for the ephemeral app-server turn without mutating the pooled default.

Limits:

- 128 declared tools per request
- 1 MiB serialized tool-schema payload
- unique function names matching `[A-Za-z0-9_-]{1,64}`
- no Gateway `session_id` during a structured tool conversation

## Structured History Mapping

Codex `thread/inject_items` receives raw Responses API history items:

| OpenAI Chat Completions input | Codex history item |
|---|---|
| user message | `message` / `user` / `input_text` or `input_image` |
| assistant text | `message` / `assistant` / `output_text` |
| assistant function call | `function_call` with the original `call_id` |
| tool result | `function_call_output` with the matching `call_id` |

The final user message becomes the new `turn/start` input. If the request ends
with tool results, HASHI supplies a neutral continuation input after injecting
the complete history. Call IDs and JSON argument strings remain stable across
the OpenAI → Codex → OpenAI boundary.

Public function names also remain stable. Internally, HASHI deterministically
aliases every caller name (including names such as `web_search`, `bash`, and
`apply_patch`) before sending it to Codex, because Codex dynamic tools must not
collide with built-in tool names or namespaces. The same mapping is applied to
structured call history and named `tool_choice`, then reversed before the
OpenAI response leaves the Gateway. HASHI preserves the caller's exact name in
all returned API tool-call objects.

## Isolation And Ownership

Caller tools are capabilities owned entirely by the API client. The bridge
enforces the following invariants for every Codex tool request:

- one new app-server process, ephemeral thread, and temporary empty working
  directory per API request;
- `approvalPolicy: never` and a read-only sandbox;
- shell, app, plugin, multi-agent, browser, computer-use, image-generation,
  hook, and Web-search features disabled;
- project instructions, persistent history, and Codex memories disabled;
- configured MCP servers re-inventoried for every request and replaced on the
  command line with complete disabled inert transports;
- only internal aliases derived from names declared in the current request are
  accepted from `item/tool/call`, then mapped back to the caller's exact name;
- duplicate/missing call IDs, undeclared tools, local tool items, unknown host
  callbacks, and policy violations interrupt the turn and fail closed;
- active app-server and MCP-inventory subprocesses tracked and killed during
  cancellation or adapter shutdown.

The inert MCP replacement is intentional. Codex CLI config overrides replace an
MCP table rather than deep-merging it, so an `enabled=false` leaf alone produces
an invalid transport. HASHI supplies a complete disabled loopback transport and
does not copy the configured endpoint, command, headers, environment, or
credentials into process arguments.

## Capturing A Tool Batch

Codex expects a response to each `item/tool/call` before it can finish the
dynamic tool item. HASHI returns an internal `HASHI_EXTERNAL_TOOL_DEFERRED`
sentinel to app-server only. That sentinel is never exposed as the actual tool
result and never authorizes execution. It lets Codex surface every independent
call in a batch, including Code Mode-wrapped calls. Once the captured dynamic
calls complete and Codex begins another model item, HASHI interrupts the turn
and returns the batch to the API client.

`parallel_tool_calls: false` is enforced twice: the model receives an explicit
single-call instruction, and HASHI rejects a response containing multiple
calls. `required` and named choices are likewise instructed and verified after
the turn.

## Compatibility And Failure Semantics

The bridge depends on Codex app-server's experimental `dynamicTools`,
`item/tool/call`, and `thread/inject_items` interfaces. It initializes with
`capabilities.experimentalApi: true` on every request. This intentionally
avoids version-number guessing: an incompatible installed CLI fails its real
protocol handshake and the Gateway returns a backend error.

Protocol reference: [OpenAI Codex app-server documentation](https://learn.chatgpt.com/docs/app-server).

Normal text-only Codex API requests continue through the established `codex
exec` adapter. A failure in MCP inventory or the app-server bridge does not
disable ordinary Codex chat, but it does fail that external-tool request.

The xAI Chat Completions passthrough remains separate and provider-native.
xAI Responses API models and non-Codex CLI adapters remain unsupported for
caller-owned functions until their structured protocols are implemented.

## Agent Checklist

An Agent or application using this bridge must:

1. Send its full structured `messages` list and tool schemas on each round.
2. Preserve the assistant tool-call object, especially its `id` and raw
   `function.arguments` string.
3. Allow-list the returned function name and validate decoded arguments.
4. Execute the function in the caller's own authorization boundary.
5. Append exactly one `role: "tool"` message per result with the matching
   `tool_call_id`.
6. Repeat until the response has no tool calls.
7. Never assume HASHI executed a function, and never use `session_id` for this
   loop.

See [API_GUIDE.md](API_GUIDE.md#external-tool-call-passthrough) for a complete
OpenAI SDK example.

## Verification

The focused regression suite covers schema conversion, structured history,
single and parallel calls, named choice filtering, streaming output, local-tool
failure closure, Gateway Codex routing, unsupported-engine rejection, and the
existing Codex adapter behavior.

A live protocol smoke test must prove both halves after a Codex CLI upgrade:

1. required function schema → standard OpenAI tool call, with no function
   execution inside HASHI;
2. assistant call plus matching tool result → final answer through
   `thread/inject_items`.

Run the repository's normal Gateway/core tests before deployment. Activate the
new modules only through HASHI's supported `/reboot` flow; do not use a cold or
hard restart for this change.
