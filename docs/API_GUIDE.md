# HASHI API Gateway — OpenAI Compatible API Guide

HASHI includes a built-in OpenAI-compatible API gateway. Any tool or library that works with the OpenAI API can connect to HASHI directly.

## Quick Start

### 1. Start HASHI with API Gateway

```bash
python main.py --api-gateway
```

The gateway listens on `global.api_gateway_port`. If that value is not set in
`agents.json`, HASHI derives it as `global.workbench_port + 1`.

Common local ports:

| Instance | HASHI API / Workbench | API Gateway |
|---|---:|---:|
| HASHI1 | `18800` | `18801` |
| HASHI2 | `18802` | `18803` |
| HASHI9 | `18819` | `18820` |

### 2. Connection Parameters

| Parameter | Value |
|-----------|-------|
| **Base URL** | `http://127.0.0.1:<api_gateway_port>/v1` |
| **Port** | `global.api_gateway_port`, defaulting to `global.workbench_port + 1` |
| **API Key** | Any non-empty string (no auth enforced, e.g. `"EMPTY"`) |

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check — returns `{"status": "ok", "engines": [...]}` |
| GET | `/v1/models` | List all available models |
| POST | `/v1/chat/completions` | Chat completion (sync & streaming) |

The HASHI API itself is separate from the API Gateway and listens on
`global.workbench_port`. Use `GET /api/health` on the HASHI API port to confirm
instance ownership, online agents, and the configured API Gateway port.

---

## Available Models

The gateway exposes models from all configured backends:

| Backend | Example Models |
|---------|---------------|
| Gemini CLI | `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-3.1-pro-preview`, `gemini-3-flash-preview` |
| Claude CLI | `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5` |
| Codex CLI | `gpt-5.4`, `gpt-5.3-codex`, `gpt-5.2-codex`, `gpt-5.2`, `gpt-5.1-codex-max`, `gpt-5.1-codex-mini` |

Run `GET /v1/models` to see the current list.

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
