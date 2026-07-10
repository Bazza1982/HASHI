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
/api model grok-4.3   # example: make Grok 4.3 the default chat model
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
    model="grok-4.3",
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

# Grok 4.3 chat completion
curl http://127.0.0.1:18801/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4.3",
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
"id": "gpt-5.4"
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
```

### `api` Field

Use `"openai-completions"` — despite the name, this maps to `/v1/chat/completions` in OpenClaw (not the legacy completions endpoint).

---

## Notes

- **No authentication** — the gateway binds to `127.0.0.1` (localhost only). Use a reverse proxy or firewall if exposing externally.
- **Session cache** is in-memory only; it resets when HASHI restarts.
- **Request timeout** is 300 seconds per request.
- Each backend adapter is lazily initialized on first request.
