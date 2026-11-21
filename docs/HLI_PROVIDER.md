# HLI (Hashi Local Inference) — HERV3 provider

HLI connects **HASHI HERV3** to Hashi Compute's existing OpenAI-style
`/v1/chat/completions` endpoint. The other end is an optional native
`llama-server` host running on the Compute machine. HASHI does not know GPU
counts, devices, batching, sharding, installation paths or model weights.

Provider ID: `hli-api` (public selector `hli`; display name
**Hashi Local Inference**). **No API key is required.** Models are user-named
GGUF IDs; no hardware/parameter-size whitelist is maintained in HASHI.

## Agent configuration

On a **non-production agent configuration**, add a provider row among
`allowed_backends` and include HLI in the HER v3 provider metadata (if this
agent uses an explicit provider list). Example snippets (merge with your
existing configuration; do not replace it):

```json
{
  "engine": "hli-api",
  "models": ["my-qwen-gguf", "my-gemma-gguf"],
  "base_url": "http://127.0.0.1:19546/v1",
  "model_capabilities": {
    "my-qwen-gguf": {"context_window_tokens": 8192,
                      "response_headroom_tokens": 2048}
  }
}
```

`19546` illustrates a staging Compute port; normal installed Compute may use
`19446`. Without an explicit URL, HLI defaults to
`http://127.0.0.1:19446/v1/chat/completions`. Keep the actual HLI host
port private to Compute; HASHI points only to Compute.

For HER provider definitions, use the existing `her_providers.providers` form:

```json
{"hli": {"engine": "hli-api", "status": "stable"}}
```

Configure HERV3 `main` / model selector to `provider: "hli"` and the exact
GGUF model ID advertised by Compute `/v1/models`. HLI exposes existing HERV3
reasoning choices: off, low, medium, high, xhigh and max. The chosen value is
passed to llama.cpp as `reasoning_effort` (off -> `none`). Each model's chat
template determines which values are effective; unsupported settings do not
create a separate HASHI refusal policy.

The core/small-model toolkit is presented to HERV3 HLI by default. The
existing `tool_find` on-demand tool view keeps tool descriptions compact and
reveals already-available tools when the model requests them. When authorised,
the shared starter includes both `file_read` and `file_write`, so normal file
work does not depend on a discovery call. This does not
alter tool execution authority, invent new permission systems or impose tool
call limits. Existing HASHI tool policies, `/stop`, and model switching remain
as they are for other providers.

## Context and streaming

- HLI observes the exact model's served context from Compute's existing
  `/v1/model-capabilities?model=...` when an adapter starts; the probe is
  best-effort and is never a prerequisite for inference.
- An explicit per-model `model_capabilities` entry can establish accurate
  capacity immediately, even while Compute is starting. This is important
  when switching an existing long session to a much smaller GGUF.
- Once context is known, the HERV3 HLI route triggers automatic Compact at
  approximately 70% of usable context and aims for 40% after Compact; existing
  non-HLI provider policies remain unchanged.
- Do not conflate `context_tokens` with total llama.cpp KV allocation. Compute
  advertises the **per-slot** context, accounting for `--parallel`.
- Token streaming, tool call JSON and native `reasoning_content` pass through
  existing OpenAI-compatible handling. HASHI always retains control of tool
  execution and `/stop`; the provider owns only inference.

## Limits of this cloud implementation

Python adapter and model-selection logic have unit checks. The GitHub branch
does **not** prove compatibility with your installed llama.cpp binary, model
templates, actual CUDA runtime, RAM/VRAM limits or live cancellation. These
require local integration testing before production changes. No GitHub Actions
or production deployments are part of this change.
