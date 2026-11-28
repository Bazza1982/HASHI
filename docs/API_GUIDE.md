# HASHI API Gateway — OpenAI Compatible API Guide

HASHI includes an optional gateway for supported OpenAI-compatible endpoints.
Clients must use the endpoint, model, media, and tool features this gateway
implements; compatibility does not imply complete OpenAI API parity.
This is separate from HASHI's authenticated Backend API. The Gateway itself
does not enforce caller authentication and should remain private.

## Quick Start

### 1. Start Or Enable HASHI API Gateway

```bash
python main.py --api-gateway
```

The gateway listens on `global.api_gateway_port`. If that value is not set in
`agents.json`, HASHI derives it as `global.workbench_port + 1`.

You can also control the gateway at runtime from Telegram:

```text
/api                  # show status, address, endpoints, and buttons
/api on               # start the gateway and persist enabled-on-restart
/api off              # stop the gateway and persist disabled-on-restart
/api model            # open default-model buttons
/api model <model>    # set the default model for requests without model
/api model gpt-6-astra  # example: make GPT-6 Astra the default chat model
```

`/api` only controls the OpenAI-compatible API Gateway. It does not change an
agent's active `/backend` or `/model`; callers can still override the gateway
default by supplying a request-level `model`.

Ports are assigned per instance. Discover the selected instance's actual
address through `/api`, configuration, and service health rather than using
another machine's historical example ports.

### 2. Connection Parameters

| Parameter | Value |
|-----------|-------|
| **Base URL** | `http://<api_host>:<api_gateway_port>/v1` |
| **Port** | `global.api_gateway_port`, defaulting to `global.workbench_port + 1` |
| **API Key** | Any non-empty string (no auth enforced, e.g. `"EMPTY"`) |

HASHI derives service binding from `global.api_host` and the shared endpoint
resolver. For loopback configuration on WSL, that resolver may use a bindable
address from live interface discovery. There is no universal WSL host address
to copy into configuration. Confirm the actual address with `/api`, Backend
API `GET /api/health`, or the startup log.

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check — live listener state, persisted restart choice, engine preflight, available models |
| GET | `/v1/models` | List models whose backends passed preflight |
| POST | `/v1/chat/completions` | Chat completion (sync & streaming) |

### Request and original-image limits

- The API Gateway accepts a serialized request body up to `256 MiB`.
- Each inline Base64 image may decode to at most `50 MiB`. A `50 MiB` image
  occupies about `66.7 MiB` after Base64 encoding, leaving about `189 MiB` for
  JSON, conversation history, tool definitions, and earlier screenshots.
- HASHI validates the declared MIME type and file signature, then forwards the
  original image bytes as a data URL. This path does not compress, resize, or
  replace the image with OCR text. Any caller-supplied image `detail` value is
  preserved.
- The `256 MiB` serialized-body boundary is the aggregate limit for HASHI API
  Codex image requests; there is no lower decoded-total cap that would reject a
  valid current image merely because earlier screenshots are also present.

The HASHI Backend API is separate from the API Gateway and listens on
`global.workbench_port` (a compatibility field name). Use `GET /api/health` on the Backend API port to confirm
instance ownership, online agents, and the configured API Gateway port.

The Telegram `/api` status view shows the live gateway address every time,
including:

- `Address`
- `/v1/chat/completions`
- `/v1/models`
- runtime state
- enabled-on-restart state
- default API model

The Gateway's own `/health` payload keeps runtime and persisted state separate:

| Field | Meaning |
| --- | --- |
| `enabled` | Live server flag; set after the listener starts and cleared when it stops |
| `running` | Whether the Gateway currently owns an active aiohttp site |
| `accepting_requests` | Whether the Gateway currently admits new HTTP requests |
| `draining` | Whether shutdown is waiting for active handlers to finish |
| `active_requests` | Number of currently tracked HTTP request handlers |
| `configured_enabled` | Persisted choice controlling whether the Gateway should return after restart |

This distinction makes a temporary command-line start visible without silently
changing the saved `/api on|off` choice.

---

## Available Models

Run `GET /v1/models` to see the current available model list, derived from
eligible engines, instance opt-ins, and preflight. Models whose backend failed
preflight (missing CLI binary, no Hermes OAuth, etc.) are omitted until the
backend becomes available.

### Instance-configured models

An active Agent's `allowed_backends` can opt this instance into additional
Gateway models beyond the shared catalogue. The backend must support
Gateway requests. Its `model`, `default_model`, `fast_model`, `pro_model` and
`models` entries supply the model names; `model_efforts` supplies permitted
request-level reasoning choices, including `max` when explicitly configured.
Conflicting Engine assignments or effort sets across Agents are rejected.

The Function-layer configuration reader uses the live instance's `agents.json`,
including UTF-8 BOM files and installations with separate source and instance
directories. It does not load Agent personas or mutate the shared model catalog.
The `/api` model menu, persisted default and Gateway request routes use these
instance opt-ins. Other instances retain their own model lists.
The model menu includes configured names even when they do not use a built-in
model-family prefix.

The Gateway snapshots model and effort opt-ins when it starts. Restart the
Gateway after changing these entries. Selecting a newly configured model before
the running Gateway has loaded it leaves the previous default unchanged and
returns a restart instruction. The API remains owned by the shared Function
process; no Core or dedicated legacy sidecar change is required. Engine preflight
still controls which models appear in `/v1/models`.

Grok CLI is the only supported Grok path. HASHI's Grok CLI catalog follows the
logged-in CLI's advertised model list; at Grok CLI `0.2.93`, `grok-4.5` is the
default and `grok-composer-2.5-fast` remains available. Existing agents with an
explicit Composer selection keep it until a user changes their model.

Grok models are not served through the API Gateway. The former `xai-api`
backend and the HASHI-native xAI device-login utility were retired, so no xAI
model appears in `GET /v1/models` and older `xai/`-prefixed Gateway calls are no
longer accepted. If `/api` reports a saved default as retired, select a
supported default or send an explicit model from the current catalogue. See
[migration and retirements](MIGRATION_AND_RETIREMENTS.md).

Call `GET /v1/models` on the target API Gateway to find the model IDs that
its running configuration and backend preflight make available.

For GPT-6 Astra, the Codex CLI adapter also declares its
context capacity to the compaction resolver: a 1,050,000-token window with
128,000 tokens of response headroom. Explicit instance/profile capacity settings
retain precedence. This capacity declaration neither enables the model nor
changes the compaction trigger policy.

### Codex CLI models

Use `/model` on the Agent to see its available Codex CLI model IDs. After you
select a model, `/effort` shows the choices for that model. HASHI normalizes a
saved effort that the selected model does not support before invoking Codex.

### HERV3 provider models

HASHI1 uses OpenRouter as a HERV3 Model Provider. Its shared catalogue
contains only `deepseek/deepseek-v3.2-exp`,
`deepseek/deepseek-v4-flash`, `deepseek/deepseek-v4-pro`, and
`google/gemini-3.8-flash`.

The official DeepSeek provider advertises `deepseek-flash` and
`deepseek-v4-pro`. `deepseek-flash` is V4.1 Flash and accepts image input, so a
separate current vision model ID is unnecessary. The adapter still recognizes
the temporary `deepseek-v4-flash` and `deepseek-v4-flash-vision-exp` aliases for
legacy configuration, but menus do not present retired aliases as current
models.

### Claw execution effort

Claw providers currently do not expose a model reasoning-effort control. HASHI
therefore maps `/effort` to the maximum agentic model/tool-loop iterations:
`low=12`, `medium=32`, `high=96` (default), `xhigh=192`, and `max=384`.
Reaching the selected budget returns a successful but machine-readable
`completion_status: incomplete` result with `stop_reason: max_iterations`;
the final iteration is tool-free and reports verified progress and a recommended
next step. A natural model stop returns `completed` with `end_turn`.

### Health preflight

`GET /health` returns:

- `status`: `ok` when at least one engine is available, otherwise `degraded`
- `engine_status`: per-engine `{available, reason}` from startup preflight
- `available_engines` / `available_models`: callable backends right now
- `default_model_available`: whether the configured default can be used
- `default_model_unavailable`: `true` when the stored default is no longer in the
  catalogue. The stored value is kept and requests that omit `model` fail with an
  explicit error until an operator selects a new default with `/api model`

---

## Usage Examples

### Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:18801/v1",
    api_key="EMPTY",
)

# Basic chat completion
response = client.chat.completions.create(
    model="gemini-2.5-flash",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.choices[0].message.content)
```

### Python — Streaming

```python
stream = client.chat.completions.create(
    model="claude-sonnet-4-6",
    messages=[{"role": "user", "content": "Tell me a joke"}],
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)
```

### External Tool-Call Passthrough

The gateway preserves caller-owned OpenAI function tools for Codex CLI models.
For Codex, HASHI
maps each OpenAI function schema to an app-server `dynamicTool`, captures the
model's `item/tool/call`, and maps it back to `message.tool_calls` with
`finish_reason: "tool_calls"`.

HASHI accepts and preserves `messages`, `tools`, `tool_choice`, and
`parallel_tool_calls`. Supported `tool_choice` values are `auto`, `none`,
`required`, and a named OpenAI function choice. When
`parallel_tool_calls: false`, HASHI fails closed if a backend nevertheless
returns more than one call. In streaming mode, the gateway emits each complete
tool call in `delta.tool_calls` before the terminal `tool_calls` finish reason;
it does not currently stream partial JSON argument fragments.

Codex requests may also include a top-level `reasoning_effort`. HASHI validates
the value against the selected model before acquiring a pooled adapter and
applies it only to that request, so concurrent clients cannot overwrite one
another's effort. The exact model-specific choices are listed above; `ultra`
is unavailable on Luna models and GPT-5.5 stops at `xhigh`. Invalid or
unverified model/value pairs return `invalid_reasoning_effort` instead of
silently falling back.

For both synchronous and streaming Codex responses, HASHI returns the
backend-reported token usage. Streaming places it on the terminal completion
chunk; clients without provider usage retain the legacy text estimate.

The gateway never executes these caller-owned tools. The client is responsible
for executing each function and sending the next request with the assistant
`tool_calls` message and matching `role: "tool"` / `tool_call_id` result.

Current boundaries:

- Supported by every qualified Codex model advertised by `GET /v1/models`,
  including `gpt-6-astra`, `gpt-6-sol`, and `gpt-6-luna`.
- Antigravity CLI, Claude CLI, and Grok CLI models are rejected instead of silently
  dropping tools.
- Gateway `session_id` caching is disabled for external tool turns; clients must
  send the complete structured conversation.
- Empty `tools: []` does not change the legacy text-only route.
- A request may declare at most 128 tools, with a combined serialized tool
  payload of at most 1 MiB.
- Function names must match `[A-Za-z0-9_-]{1,64}` and names must be unique.
- External tool passthrough currently supports `n: 1` only.

Each Codex request runs in an ephemeral app-server thread and temporary working
directory. HASHI disables Codex shell, filesystem mutation, Web, app, plugin,
image, computer-use, multi-agent, and configured MCP access for that thread.
Configured MCP servers are inventoried again for every request and replaced by
disabled inert transports, so a server added after adapter startup cannot leak
into this path. Native/local tool lifecycle events do not abort the API turn;
Codex can observe its tool failure and continue directly. Unsupported host
callbacks receive a recoverable method-not-found response. If isolation cannot
be proven, an undeclared caller-owned dynamic tool appears, or the experimental
app-server protocol is unavailable, the request still fails with an
`external_tool_backend_error`.

The installed Codex CLI must support the experimental app-server
`dynamicTools`, `item/tool/call`, and `thread/inject_items` protocol. HASHI does
not persist Codex threads for API tool loops: structured assistant calls and
tool results are re-injected from the caller's complete `messages` array on each
request. This keeps concurrent clients isolated and avoids hidden session state.

#### Complete Python tool loop

```python
import json
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:18801/v1",
    api_key="EMPTY",
)

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Return the current weather for one city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
                "additionalProperties": False,
            },
        },
    }
]


def get_weather(city: str) -> dict:
    # Replace this example with the caller's real implementation.
    return {"city": city, "temperature_c": 23, "condition": "sunny"}


messages = [{"role": "user", "content": "What is Sydney's weather?"}]

while True:
    response = client.chat.completions.create(
        model="gpt-5.6-luna",
        messages=messages,
        tools=tools,
        tool_choice="auto",
        parallel_tool_calls=True,
    )
    assistant = response.choices[0].message
    messages.append(assistant.model_dump(exclude_none=True))

    if not assistant.tool_calls:
        print(assistant.content)
        break

    for call in assistant.tool_calls:
        if call.function.name != "get_weather":
            raise RuntimeError(f"unapproved tool: {call.function.name}")
        arguments = json.loads(call.function.arguments)
        result = get_weather(**arguments)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result),
            }
        )
```

Do not execute an unrecognized function name, and validate arguments against
the caller's own authorization rules before invoking a side-effecting tool.
The detailed design and Agent contract are in
[CODEX_API_TOOL_CALL_BRIDGE.md](CODEX_API_TOOL_CALL_BRIDGE.md).

Hot deployment requires only `/reboot`. An enabled in-process API Gateway first
stops accepting requests, drains active handlers, then shuts down its adapters
and is recreated from the reloaded modules. A request reaching an accepted
connection during the drain receives a retriable `503` with
`code: gateway_draining` and `Retry-After: 1`; HASHI itself remains online. If
an active handler cannot be cancelled safely, Adapter shutdown and Gateway
replacement are aborted instead of racing that handler.

### Python — Multi-turn with Session Cache

```python
# Pass session_id to maintain conversation context (TTL: 30 minutes)
response = client.chat.completions.create(
    model="gemini-2.5-pro",
    messages=[{"role": "user", "content": "My name is Barry"}],
    extra_body={"session_id": "my-session-1"},
)

# Follow-up in same session — the model remembers previous messages
response = client.chat.completions.create(
    model="gemini-2.5-pro",
    messages=[{"role": "user", "content": "What is my name?"}],
    extra_body={"session_id": "my-session-1"},
)
```

### cURL

```bash
# Health check
curl http://127.0.0.1:18801/health

# List models
curl http://127.0.0.1:18801/v1/models

# Chat completion
curl http://127.0.0.1:18801/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-2.5-flash",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": false
  }'
```

### JavaScript / TypeScript

```typescript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://127.0.0.1:18801/v1",
  apiKey: "EMPTY",
});

const response = await client.chat.completions.create({
  model: "gemini-2.5-flash",
  messages: [{ role: "user", content: "Hello!" }],
});
console.log(response.choices[0].message.content);
```

---

## Configuration

In `agents.json`, the port is set under the `global` section:

```json
{
  "global": {
    "workbench_port": 18800,
    "api_gateway_port": 18801
  }
}
```

If `api_gateway_port` is omitted, HASHI uses `workbench_port + 1`. For example,
HASHI2 with `"workbench_port": 18802` will use API Gateway port `18803`.

Runtime `/api on|off|model` choices are persisted separately from `agents.json`
so they survive a cold restart. This allows an operator to keep the core config
stable while changing whether the gateway comes back on restart and which
default model it uses for requests that omit `model`.

The canonical file is:

```text
<bridge_home>/state/api_gateway_config.json
```

Older installations may have `<bridge_home>/api_gateway_state.json`. When the
canonical file does not yet exist, HASHI imports that legacy state once,
atomically writes the canonical file, and retains the old file as a rollback
artifact. After migration, only the canonical file is read and updated, so the
Telegram controls, startup, hot reboot, and Gateway health view share one state
owner.

---

---

## OpenClaw Integration

OpenClaw uses the `vllm` provider type to connect to HASHI.

### Provider Config (`openclaw.json`)

```json
"models": {
  "providers": {
    "vllm": {
      "baseUrl": "http://127.0.0.1:18801/v1",
      "apiKey": "EMPTY",
      "api": "openai-completions",
      "models": [
        { "id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash", ... },
        { "id": "claude-opus-4-6", "name": "Claude Opus 4.6", ... }
      ]
    }
  }
}
```

### Critical: Model ID Format

The model `id` in `openclaw.json` must match **exactly** what HASHI serves in `GET /v1/models`.

**Correct:**
```
"id": "gemini-2.5-flash"
"id": "claude-opus-4-6"
"id": "gpt-5.6-sol"
```

**Wrong (will cause "unknown model" errors):**
```
"id": "gemini/gemini-2.5-flash"   ← prefix breaks routing
"id": "claude/claude-opus-4-6"    ← prefix breaks routing
"id": "codex/gpt-5.6-sol"         ← prefix breaks routing
```

OpenClaw sends the model `id` as-is to the API — it does not strip any prefix. The `vllm/` part in the full model selector (e.g. `vllm/gemini-2.5-flash`) is the OpenClaw provider prefix and is stripped by OpenClaw itself; everything after `vllm/` is what gets sent to HASHI.

### Model Selector in Agent Config

```json
"model": "vllm/gemini-2.5-flash"
"model": "vllm/claude-opus-4-6"
"model": "vllm/gpt-5.6-sol"
```

### `api` Field

Use `"openai-completions"` — despite the name, this maps to `/v1/chat/completions` in OpenClaw (not the legacy completions endpoint).

For caller-owned tool use, select a supported Codex model such as
`vllm/gpt-5.6-luna`. The Gateway rejects unsupported CLI models instead of
silently discarding tools. OpenClaw must execute each returned
tool locally and include the assistant `tool_calls` plus matching
`role: "tool"` result in its next request.

---

## Notes

- **No request authentication** — depending on the host configuration, the
  gateway may bind to `127.0.0.1`, a configured address, or the WSL
  host-virtual address. Use a firewall, authenticated reverse
  proxy, or private tunnel before exposing it beyond a trusted local boundary.
- **Session cache** is in-memory only; it resets when HASHI restarts.
- **Request timeout** is 300 seconds per request.
- Each backend adapter is lazily initialized on first request.


### Same-model execution capacity (HASHI3 nightly candidate)

`api_gateway_config.json` supports `max_parallel_per_model` (default 2, range
1..8), published through its existing revision-aware writer. Each engine/model
pair leases independent adapter/process slots; each slot is exclusively held
through streaming completion. Slot workspaces are isolated. API Session cache
leases still cover history read/append/write, so one cached Session is FIFO.
Native CLI thread context is cleared between API calls; assembled messages own
API history and cannot inherit a previous pooled client's thread.
