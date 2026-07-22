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

| Instance | HASHI API / Workbench | API Gateway |
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

By default, HASHI binds the Workbench API and API Gateway to the configured
`global.api_host`. If that value is `127.0.0.1` or `localhost` and the WSL host
alias `10.255.255.254` is available, HASHI uses `10.255.255.254` instead. This
avoids WSL loopback environments where `127.0.0.1` accepts a socket but does not
serve aiohttp traffic reliably. Confirm the live address with Workbench
`GET /api/health` or the startup log line.

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check — gateway status, engine preflight, available models |
| GET | `/v1/models` | List models whose backends passed preflight |
| POST | `/v1/chat/completions` | Chat completion (sync & streaming) |
| POST | `/v1/images/generations` | xAI Imagine image generation |
| POST | `/v1/videos/generations` | xAI Imagine video generation request |

The HASHI API itself is separate from the API Gateway and listens on
`global.workbench_port`. Use `GET /api/health` on the HASHI API port to confirm
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
uses xAI's Responses API route and was smoke-tested through HASHI's xAI OAuth
adapter. The three Codex API Gateway variants `gpt-5.6-sol`,
`gpt-5.6-terra`, and `gpt-5.6-luna` remain the tested GPT-5.6 choices.

**Coming soon:** direct Grok OAuth for agent runtimes via Claw (`claw-cli` +
HASHI-owned device login, no Hermes and no `grok-cli`). Code is in tree;
production login waits on HASHI's own xAI OAuth `client_id`. See
[HASHI_XAI_CLAW_OAUTH.md](HASHI_XAI_CLAW_OAUTH.md).

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

The gateway preserves caller-owned OpenAI function tools for xAI models that use
the native `/chat/completions` route. The gateway forwards `messages`, `tools`,
`tool_choice`, and `parallel_tool_calls`, then returns `message.tool_calls` with
`finish_reason: "tool_calls"`. In streaming mode it emits a complete
`delta.tool_calls` before the terminal `tool_calls` finish reason.

The gateway never executes these caller-owned tools. The client is responsible
for executing each function and sending the next request with the assistant
`tool_calls` message and matching `role: "tool"` / `tool_call_id` result.

Current boundaries:

- Supported only by `xai-api` models using `/chat/completions`, such as
  `grok-4.3`.
- CLI-backed models are rejected instead of silently dropping tools.
- xAI Responses API models, including `grok-4.5` and `grok-build-*`, are rejected
  until their separate function-call protocol is implemented.
- Gateway `session_id` caching is disabled for external tool turns; clients must
  send the complete structured conversation.
- Empty `tools: []` does not change the legacy text-only route.
- A request may declare at most 128 tools, with a combined serialized tool
  payload of at most 1 MiB.

Hot deployment requires only `/reboot`. An enabled in-process API Gateway is
stopped and recreated from the reloaded modules as part of the normal reboot
service refresh.

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

This path was live-validated on HASHI2 with both Claw Code 0.1.0 and HASHI Claw
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

For caller-owned tool use, select `vllm/grok-4.3`. The current Gateway rejects
tools for CLI-backed models and xAI Responses API models instead of silently
discarding them. OpenClaw must execute each returned tool locally and include
the assistant `tool_calls` plus matching `role: "tool"` result in its next
request.

---

## Notes

- **No request authentication** — depending on the host configuration, the
  gateway may bind to `127.0.0.1`, a configured address, or the WSL
  host-virtual address `10.255.255.254`. Use a firewall, authenticated reverse
  proxy, or private tunnel before exposing it beyond a trusted local boundary.
- **Session cache** is in-memory only; it resets when HASHI restarts.
- **Request timeout** is 300 seconds per request.
- Each backend adapter is lazily initialized on first request.
