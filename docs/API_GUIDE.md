# HASHI API Gateway — OpenAI Compatible API Guide

HASHI includes a built-in OpenAI-compatible API gateway. Any tool or library that works with the OpenAI API can connect to HASHI directly.

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
/api model grok-4.5   # example: make Grok 4.5 the default chat model
```

`/api` only controls the OpenAI-compatible API Gateway. It does not change an
agent's active `/backend` or `/model`; callers can still override the gateway
default by supplying a request-level `model`.

Common local ports:

| Instance | HASHI Backend API | API Gateway |
|---|---:|---:|
| HASHI1 | `18800` | `18801` |
| HASHI2 | `18802` | `18803` |
| HASHI9 | `18819` | `18820` |

### 2. Connection Parameters

| Parameter | Value |
|-----------|-------|
| **Base URL** | `http://<api_host>:<api_gateway_port>/v1` |
| **Port** | `global.api_gateway_port`, defaulting to `global.workbench_port + 1` |
| **API Key** | Any non-empty string (no auth enforced, e.g. `"EMPTY"`) |

By default, HASHI binds the Backend API and API Gateway to the configured
`global.api_host`. If that value is `127.0.0.1` or `localhost` and the WSL host
alias `10.255.255.254` is available, HASHI uses `10.255.255.254` instead. This
avoids WSL loopback environments where `127.0.0.1` accepts a socket but does not
serve aiohttp traffic reliably. Confirm the live address with Backend API
`GET /api/health` or the startup log line.

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check — live listener state, persisted restart choice, engine preflight, available models |
| GET | `/v1/models` | List models whose backends passed preflight |
| POST | `/v1/chat/completions` | Chat completion (sync & streaming) |
| POST | `/v1/images/generations` | xAI Imagine image generation |
| POST | `/v1/videos/generations` | xAI Imagine video generation request |

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
- `/v1/images/generations`
- `/v1/videos/generations`
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

The gateway exposes models from all configured backends:

| Backend | Example Models |
|---------|---------------|
| Gemini CLI | `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-3.1-pro-preview`, `gemini-3-flash-preview` |
| Claude CLI | `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5` |
| Codex CLI | `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`, `gpt-5.3-codex-spark`, `gpt-5.4`, `gpt-5.3-codex`, `gpt-5.2-codex`, `gpt-5.2`, `gpt-5.1-codex-max`, `gpt-5.1-codex-mini` |
| Grok CLI | `grok-4.5`, `grok-composer-2.5-fast` |
| xAI API (`xai-api`) | `grok-4.5`, `grok-4.3`, `grok-build-0.1`, `grok-4.20-0309-reasoning`, `grok-4.20-0309-non-reasoning`, `grok-4.20-multi-agent-0309`, `grok-imagine-image`, `grok-imagine-image-quality`, `grok-imagine-video`, `grok-imagine-video-1.5-preview` |

Run `GET /v1/models` to see the current list. Models whose backend failed
preflight (missing CLI binary, no Hermes OAuth, etc.) are omitted until the
backend becomes available.

Grok CLI is maintained separately from the `xai-api` backend. HASHI's Grok
CLI catalog follows the logged-in CLI's advertised model list; at Grok CLI
`0.2.93`, `grok-4.5` is the default and `grok-composer-2.5-fast` remains
available. Existing agents with an explicit Composer selection keep it until a
user changes their model.

`grok-4.5` is also available through the API Gateway's `xai-api` backend. It
uses xAI's Responses API route with the credential source configured for that
backend. The three Codex API Gateway variants `gpt-5.6-sol`,
`gpt-5.6-terra`, and `gpt-5.6-luna` remain the tested GPT-5.6 choices.

The separate HASHI-native device-login utility is documented in
[HASHI_XAI_OAUTH.md](HASHI_XAI_OAUTH.md). Its token store is not implicitly
injected into an active backend.

### GPT-5.6 through Codex CLI

HASHI supports the smoke-tested Codex CLI variants below. The bare `gpt-5.6`
alias is deliberately not advertised because it was rejected by the configured
ChatGPT-account Codex access path.

| Model | HASHI use | `/effort` choices |
|---|---|---|
| `gpt-5.6-sol` | Highest-capability tier for difficult, long-horizon work | `low`, `medium`, `high`, `xhigh`, `max` |
| `gpt-5.6-terra` | Balanced daily-use tier | `low`, `medium`, `high`, `xhigh` |
| `gpt-5.6-luna` | Fast, cost-efficient tier | `low`, `medium`, `high`, `xhigh` |

The Telegram `/effort` command follows the currently selected model rather
than exposing one unsafe backend-wide list. If an agent switches from Sol with
`max` selected to Terra or Luna, HASHI automatically normalizes effort to
`medium` before the next Codex invocation. See OpenAI's
[GPT-5.6 preview announcement](https://openai.com/index/previewing-gpt-5-6-sol/)
for the model-family positioning.

### Claw execution effort

Claw providers currently do not expose a model reasoning-effort control. HASHI
therefore maps `/effort` to the maximum agentic model/tool-loop iterations:
`low=12`, `medium=32`, `high=96` (default), `xhigh=192`, and `max=384`.
Reaching the selected budget returns a successful but machine-readable
`completion_status: incomplete` result with `stop_reason: max_iterations`;
the final iteration is tool-free and reports verified progress and a recommended
next step. A natural model stop returns `completed` with `end_turn`.

### xAI OAuth setup

`xai-api` uses Hermes-managed SuperGrok OAuth with automatic token refresh.
Configure in `agents.json`:

```json
{
  "global": {
    "hermes_home": "/mnt/c/Users/<you>/AppData/Local/hermes/profiles/<profile>",
    "xai_api_base_url": "https://api.x.ai/v1"
  }
}
```

On native Windows, `hermes_home` is typically
`C:\\Users\\<you>\\AppData\\Local\\hermes\\profiles\\<profile>` when the
working xAI OAuth credential belongs to a Hermes profile. Use the global Hermes
root only when that root owns the valid `xai-oauth` credential.

Fallback options in `secrets.json`:

- `xai_oauth_refresh_token` — standalone OAuth refresh (no Hermes install)
- `xai_api_key` — static console API key

HASHI prefers Hermes' own xAI OAuth resolver when the local `hermes-agent`
package is available, so Hermes keeps ownership of credential-pool refresh and
rotated refresh-token persistence. HASHI only falls back to direct token reading
when the resolver cannot be imported.

`grok-build-0.1` routes to xAI `/v1/responses`. Set global
`xai_use_responses_api: true` to force all `xai-api` models through responses.

Imagine image models (`grok-imagine-image*`) are exposed through both
`/v1/chat/completions` and `/v1/images/generations`. Imagine video models
(`grok-imagine-video*`) are exposed through `/v1/videos/generations`. Agents
with tools enabled can also use the `xai_imagine` tool from the `web` tier.

Example:

```python
client = OpenAI(
    base_url="http://10.255.255.254:18801/v1",
    api_key="EMPTY",
)
response = client.chat.completions.create(
    model="grok-4.5",
    messages=[{"role": "user", "content": "Hello"}],
)
```

Standard media routes use the same gateway host, without the `/v1` suffix in
the configured base URL when using raw HTTP:

```bash
curl http://10.255.255.254:18801/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-imagine-image",
    "prompt": "small red cube on a white background",
    "n": 1
  }'

curl http://10.255.255.254:18801/v1/videos/generations \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-imagine-video",
    "prompt": "small red cube rotating on a white background"
  }'
```

### Health preflight

`GET /health` returns:

- `status`: `ok` when at least one engine is available, otherwise `degraded`
- `engine_status`: per-engine `{available, reason}` from startup preflight
- `available_engines` / `available_models`: callable backends right now
- `default_model_available`: whether the configured default can be used

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

The gateway preserves caller-owned OpenAI function tools for Codex CLI models
and xAI models that use the native `/chat/completions` route. For Codex, HASHI
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
another's effort. Current live-probed Luna and Sol values are `none`, `low`,
`medium`, `high`, `xhigh`, and `max`. Invalid or unverified model/value pairs
return `invalid_reasoning_effort` instead of silently falling back.

For both synchronous and streaming Codex responses, HASHI returns the
backend-reported token usage. Streaming places it on the terminal completion
chunk; clients without provider usage retain the legacy text estimate.

The gateway never executes these caller-owned tools. The client is responsible
for executing each function and sending the next request with the assistant
`tool_calls` message and matching `role: "tool"` / `tool_call_id` result.

Current boundaries:

- Supported by every Codex model advertised by `GET /v1/models`, including the
  smoke-tested `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` variants.
- Also supported by `xai-api` models using `/chat/completions`, such as
  `grok-4.3`.
- Gemini CLI, Claude CLI, and Grok CLI models are rejected instead of silently
  dropping tools.
- xAI Responses API models, including `grok-4.5` and `grok-build-*`, are rejected
  until their separate function-call protocol is implemented.
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

#### Verified Claw Code tool loop

Claw Code identifies Grok as an xAI provider. Point its xAI-compatible client at
the HASHI Gateway, keep the `/v1` suffix, and use the provider-qualified model
selector:

```bash
export XAI_BASE_URL="http://<gateway-host>:18803/v1"
export XAI_API_KEY="EMPTY"

claw \
  --model xai/grok-4.3 \
  --permission-mode read-only \
  --allowedTools glob \
  --output-format json \
  prompt "Use the Glob tool once, then report the result."
```

Claw requires a non-empty `XAI_API_KEY` value even when the Gateway is on a
trusted local network and does not validate that placeholder. Claw removes the
`xai/` provider prefix and sends `model: "grok-4.3"` to HASHI.

This path was live-validated on HASHI2 with both Claw Code 0.1.0 and HASHI Engine Runtime (HER)
0.1.3. The model returned one `tool_call`, Claw executed `glob_search` locally,
Claw sent the matching tool result back through the Gateway, and the model
produced the requested final answer on iteration two. No Claw tool was executed
by HASHI's Gateway.

`10.255.255.254` may be a host-virtual address that works only from the HASHI
host. A Claw instance on another machine must use a Gateway address reachable
from that machine, with an appropriate firewall, reverse proxy, or tunnel.

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

# Grok 4.5 Responses-backed chat completion
curl http://127.0.0.1:18801/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4.5",
    "messages": [{"role": "user", "content": "Hello from HASHI"}],
    "stream": false
  }'

# xAI Imagine image generation
curl http://127.0.0.1:18801/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-imagine-image",
    "prompt": "small red cube on a white background",
    "n": 1
  }'

# xAI Imagine video generation request
curl http://127.0.0.1:18801/v1/videos/generations \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-imagine-video",
    "prompt": "small red cube rotating on a white background"
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
        { "id": "claude-opus-4-6", "name": "Claude Opus 4.6", ... },
        { "id": "grok-4.3", "name": "Grok 4.3", ... }
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
"id": "gpt-5.4"
"id": "grok-4.3"
```

**Wrong (will cause "unknown model" errors):**
```
"id": "gemini/gemini-2.5-flash"   ← prefix breaks routing
"id": "claude/claude-opus-4-6"    ← prefix breaks routing
"id": "codex/gpt-5.4"             ← prefix breaks routing
```

OpenClaw sends the model `id` as-is to the API — it does not strip any prefix. The `vllm/` part in the full model selector (e.g. `vllm/gemini-2.5-flash`) is the OpenClaw provider prefix and is stripped by OpenClaw itself; everything after `vllm/` is what gets sent to HASHI.

### Model Selector in Agent Config

```json
"model": "vllm/gemini-2.5-flash"
"model": "vllm/claude-opus-4-6"
"model": "vllm/gpt-5.4"
"model": "vllm/grok-4.3"
```

### `api` Field

Use `"openai-completions"` — despite the name, this maps to `/v1/chat/completions` in OpenClaw (not the legacy completions endpoint).

For caller-owned tool use, select a supported Codex model such as
`vllm/gpt-5.6-luna`, or an xAI Chat Completions model such as
`vllm/grok-4.3`. The Gateway rejects unsupported CLI and xAI Responses API
models instead of silently discarding tools. OpenClaw must execute each returned
tool locally and include the assistant `tool_calls` plus matching
`role: "tool"` result in its next request.

---

## Notes

- **No request authentication** — depending on the host configuration, the
  gateway may bind to `127.0.0.1`, a configured address, or the WSL
  host-virtual address `10.255.255.254`. Use a firewall, authenticated reverse
  proxy, or private tunnel before exposing it beyond a trusted local boundary.
- **Session cache** is in-memory only; it resets when HASHI restarts.
- **Request timeout** is 300 seconds per request.
- Each backend adapter is lazily initialized on first request.
